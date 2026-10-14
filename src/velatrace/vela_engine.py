"""Native Vela-routing engine (research): multilayer grid A* with negotiated congestion.

Independent implementation from published ideas only: A* maze search on a uniform
grid with 45-degree moves, through vias, PathFinder-style negotiated congestion
(McMurchie & Ebeling) and a final hard pass that never leaves two nets in conflict.
Inner layers that are mostly copper zone are kept as solid reference planes: no
signal is routed on them, and pads of the plane's net drop a via into it.

Needs numpy (the ``routing-research`` extra). Not wired into the plugin; every
result must still pass KiCad DRC, which stays the only judge.

ponytail: pure-Python search loop, roughly 1e5 expansions/s. Move the inner loop
to a compiled kernel if boards beyond a few hundred nets matter.
"""
from collections import deque
from dataclasses import dataclass, field, replace
import heapq
import math
import time
import uuid

import numpy as np

from .candidate import CopperItem
from .stackup import homogeneous_delay_ps
from .vela_board import Board
from .vela_electrical import differential_pairs, layer_models

_SQRT2 = math.sqrt(2)


@dataclass
class Options:
    pitch: float = 0.1            # target grid pitch in mm; snapped to the finest pad pitch
    via_mm: float = 4.0           # track length a via is worth
    iterations: int = 40          # negotiated-congestion rounds before the hard pass
    edge_clearance: float = 0.25  # floor under the board's own copper-to-edge rule, mm
    budget_s: float = 900.0
    heuristic: float = 2.0        # A* weight while negotiating; >1 trades length for speed
    tidy_heuristic: float = 1.2   # A* weight of the tidy-up pass, which decides the final shape
    pres0: float = 1.0
    pres_mult: float = 1.5
    pres_cap: float = 64.0
    hist_step: float = 0.3
    plane_fraction: float = 0.3   # inner layer is a reference plane above this zone coverage
    plane_via_mm: float = 1.2     # a plane drop is cheap: every pad should get its own via
    plane_cost: float = 3.0       # last-resort cost factor for a signal on a solid plane; 0 = never
    mixed_cost: float = 1.5       # cost factor for signals on the other plane layers; 0 = keep all solid
    via_in_pad_min: float = 2.0   # SMD pads narrower than this never get a via in them, mm
    rescue_turns: int = 12        # rip-up turns per dropped connection before giving up
    partial_rounds: bool = True   # negotiation re-routes only the paths in conflict, not whole nets
    rip_cost: float = 8.0         # price per grid step of pushing through another net in the rescue
    stall_rounds: int = 5         # stop negotiating after this many rounds without progress
    probe: int = 1500             # expansions of the cheap backwards probe before the real search
    optimize_passes: int = 2
    neck_width: float = 0.15      # escape width for pads the class width cannot reach, mm
    seed: int | None = 1          # shuffles the re-route order each round to break ties; None = fixed
    tries: int = 3                # route_board: re-route orders tried until one routes everything
    hist_spread: float = 0.4      # mm around a conflict that gets the history penalty
    log: object = None            # callable(str) for progress


@dataclass
class _Net:
    name: str
    cls: object
    t: int                        # track map index
    v: int                        # via map index
    pads: list
    plane_layers: tuple = ()
    neck: int | None = None       # track map index of the narrow escape width, if narrower
    paths: list = field(default_factory=list)   # each: list of global flat cells
    necks: set = field(default_factory=set)     # indexes of paths drawn at the escape width
    stamps: list | None = None
    failed: int = 0
    connections: int = 0


def _pad_sdf(pad, X, Y):
    a = math.radians(pad.angle)
    ca, sa = math.cos(a), math.sin(a)
    dx, dy = X - pad.x, Y - pad.y
    lx, ly = dx * ca - dy * sa, dx * sa + dy * ca
    best = None
    for shape in pad.shapes:
        if shape[0] == "rrect":
            _, cx, cy, w, h, r = shape
            qx, qy = np.abs(lx - cx) - (w / 2 - r), np.abs(ly - cy) - (h / 2 - r)
            d = np.hypot(np.maximum(qx, 0), np.maximum(qy, 0)) + np.minimum(np.maximum(qx, qy), 0) - r
        elif shape[0] == "line":
            d = _segment_sdf(*shape[1:5], lx, ly) - shape[5] / 2
        else:
            d = _poly_sdf(shape[1], lx, ly)
        best = d if best is None else np.minimum(best, d)
    return best


def _pad_reach(pad):
    reach = 0.0
    for shape in pad.shapes:
        if shape[0] == "rrect":
            reach = max(reach, math.hypot(abs(shape[1]) + shape[3] / 2, abs(shape[2]) + shape[4] / 2))
        elif shape[0] == "line":
            reach = max(reach, math.hypot(*shape[1:3]) + shape[5] / 2, math.hypot(*shape[3:5]) + shape[5] / 2)
        else:
            reach = max(reach, max(math.hypot(x, y) for x, y in shape[1]))
    return reach


def _segment_sdf(x1, y1, x2, y2, X, Y):
    ex, ey = x2 - x1, y2 - y1
    t = np.clip(((X - x1) * ex + (Y - y1) * ey) / (ex * ex + ey * ey or 1e-18), 0, 1)
    return np.hypot(X - x1 - t * ex, Y - y1 - t * ey)


def _poly_sdf(points, X, Y):
    d = np.full(np.broadcast(X, Y).shape, np.inf)
    inside = np.zeros(d.shape, bool)
    for (ax, ay), (bx, by) in zip(points, points[1:] + points[:1]):
        d = np.minimum(d, _segment_sdf(ax, ay, bx, by, X, Y))
        if ay != by:
            inside ^= ((ay > Y) != (by > Y)) & (X < ax + (Y - ay) * (bx - ax) / (by - ay))
    return np.where(inside, -d, d)


def _inside(points, xs, ys):
    """Even-odd polygon mask on the grid, touching only the rows each edge spans."""
    mask = np.zeros((ys.size, xs.size), bool)
    for (ax, ay), (bx, by) in zip(points, points[1:] + points[:1]):
        if ay == by:
            continue
        lo, hi = np.searchsorted(ys, [min(ay, by), max(ay, by)])
        if hi > lo:
            cross = ax + (ys[lo:hi, None] - ay) * (bx - ax) / (by - ay)
            mask[lo:hi] ^= xs[None, :] < cross
    return mask


def _erode(mask, cells):
    for _ in range(cells):
        inner = mask.copy()
        inner[1:] &= mask[:-1]
        inner[:-1] &= mask[1:]
        inner[:, 1:] &= mask[:, :-1]
        inner[:, :-1] &= mask[:, 1:]
        mask = inner
    return mask


def _pieces(region, seeds):
    """Which connected piece of ``region`` each seed cell sits in (0 = none), and the label image.

    ponytail: labels stop at 255, so past 253 pieces the rest share one label.
    """
    h, w = region.shape
    state = bytearray(region.astype(np.uint8).tobytes())   # 1 = in the region, not yet labelled
    labels, label = [], 1
    for seed in seeds:
        # A seed on the rim counts for the piece beside it.
        near = (seed, seed - 1, seed + 1, seed - w, seed + w, seed - w - 1, seed - w + 1, seed + w - 1, seed + w + 1,
                seed - 2, seed + 2, seed - 2 * w, seed + 2 * w)
        start = next((cell for cell in near if 0 <= cell < h * w and state[cell]), None)
        if start is None:
            labels.append(0)
            continue
        if state[start] == 1:
            label = min(label + 1, 255)
            state[start] = label
            stack = [start]
            while stack:
                i = stack.pop()
                x = i % w
                for j in (i - w, i + w, i - 1 if x else -1, i + 1 if x < w - 1 else -1):
                    if 0 <= j < h * w and state[j] == 1:
                        state[j] = label
                        stack.append(j)
        labels.append(state[start])
    return labels, np.frombuffer(bytes(state), np.uint8).reshape(h, w)


def _flood_outside(blocked):
    """Cells reachable from the grid corner without crossing ``blocked``."""
    h, w = blocked.shape
    state = bytearray(blocked.astype(np.uint8).tobytes())   # 0 free, 1 blocked, 2 outside
    stack = [0]
    state[0] = 2
    while stack:
        i = stack.pop()
        x = i % w
        for j in (i - w, i + w, i - 1 if x else -1, i + 1 if x < w - 1 else -1):
            if 0 <= j < h * w and not state[j]:
                state[j] = 2
                stack.append(j)
    return np.frombuffer(bytes(state), np.uint8).reshape(h, w) == 2


def _search(cost, vcost, w, hw, layers, starts, goal, box, weight, limit=0):
    """A* over the window. cost/vcost < 0 is blocked. Returns cells start..goal or None.

    With ``limit``, gives up after that many expansions and returns False (not None:
    None means the reachable area was exhausted, i.e. the starts are walled in).
    """
    inf = float("inf")
    g = [inf] * (layers * hw)
    came = [-1] * (layers * hw)
    gx0, gy0, gx1, gy1 = box
    heap = []
    for s in starts:
        if cost[s] < 0 and not goal[s]:
            continue
        g[s] = 0.0
        r = s % hw
        x, y = r % w, r // w
        dx = gx0 - x if x < gx0 else (x - gx1 if x > gx1 else 0)
        dy = gy0 - y if y < gy0 else (y - gy1 if y > gy1 else 0)
        heap.append(((dx + dy - 0.5857864 * (dx if dx < dy else dy)) * weight, 0.0, s))
    heapq.heapify(heap)
    moves = ((1, 1, 0, 1.0), (-1, -1, 0, 1.0), (w, 0, 1, 1.0), (-w, 0, -1, 1.0),
             (w + 1, 1, 1, _SQRT2), (w - 1, -1, 1, _SQRT2), (-w + 1, 1, -1, _SQRT2), (-w - 1, -1, -1, _SQRT2))
    bases = tuple(range(0, layers * hw, hw))
    push, pop = heapq.heappush, heapq.heappop
    while heap:
        _, gi, i = pop(heap)
        if gi > g[i]:
            continue
        if limit:
            limit -= 1
            if not limit:
                return False
        if goal[i]:
            path = [i]
            while came[i] >= 0:
                i = came[i]
                path.append(i)
            path.reverse()
            return path
        r = i % hw
        x, y = r % w, r // w
        for off, mx, my, mult in moves:
            j = i + off
            c = cost[j]
            if c < 0:
                continue
            ng = gi + c * mult
            if ng < g[j]:
                g[j] = ng
                came[j] = i
                nx, ny = x + mx, y + my
                dx = gx0 - nx if nx < gx0 else (nx - gx1 if nx > gx1 else 0)
                dy = gy0 - ny if ny < gy0 else (ny - gy1 if ny > gy1 else 0)
                push(heap, (ng + (dx + dy - 0.5857864 * (dx if dx < dy else dy)) * weight, ng, j))
        vc = vcost[r]
        if vc >= 0:
            here = i - r
            dx = gx0 - x if x < gx0 else (x - gx1 if x > gx1 else 0)
            dy = gy0 - y if y < gy0 else (y - gy1 if y > gy1 else 0)
            h = (dx + dy - 0.5857864 * (dx if dx < dy else dy)) * weight
            for base in bases:
                if base == here:
                    continue
                j = base + r
                c = cost[j]
                if c < 0:
                    continue
                ng = gi + vc + c
                if ng < g[j]:
                    g[j] = ng
                    came[j] = i
                    push(heap, (ng + h, ng, j))
    return None


class Router:
    def __init__(self, board: Board, options: Options | None = None, obstacles=()):
        """``obstacles``: extra copper boxes (copper_obstacles.CopperObstacle), e.g. copper text."""
        self.board, self.opt = board, options or Options()
        self.started = time.monotonic()
        self._classes()
        self._grid()
        self._statics()
        for box in obstacles:
            if box.layer in board.layers:
                iy0, iy1, ix0, ix1 = self._window_of(box.xmin_mm, box.ymin_mm, box.xmax_mm, box.ymax_mm, self.reach)
                X, Y = self.xs[None, ix0:ix1], self.ys[iy0:iy1, None]
                qx = np.maximum(box.xmin_mm - X, X - box.xmax_mm)
                qy = np.maximum(box.ymin_mm - Y, Y - box.ymax_mm)
                d = np.hypot(np.maximum(qx, 0), np.maximum(qy, 0)) + np.minimum(np.maximum(qx, qy), 0)
                self._block(d, iy0, ix0, (board.layers.index(box.layer),), 0.0, None)
        self._planes()
        self._electrical()

    def _say(self, text):
        if self.opt.log:
            self.opt.log(f"[{time.monotonic() - self.started:6.1f}s] {text}")

    # ---- setup -------------------------------------------------------------
    def _classes(self):
        board = self.board
        by_net = {}
        for index, pad in enumerate(board.pads):
            if pad.net and pad.layers:
                by_net.setdefault(pad.net, []).append(index)
        classes = {name: board.netclass(name) for name in by_net}
        # A class width is a default, not a limit: the board minimum may be used to leave tight pads.
        neck = self.neck_width = max(self.opt.neck_width, board.rules.get("min_track_width", 0))
        tracks = sorted({(c.width, c.clearance) for c in classes.values()}
                        | {(neck, c.clearance) for c in classes.values() if c.width > neck + 1e-9} or {(0.2, 0.2)})
        self.default_width = board.netclass("").width
        vias = sorted({(c.via_diameter, c.clearance) for c in classes.values()} or {(0.6, 0.2)})
        # One occupancy map per probe: (half extent, clearance). Tracks first, then vias.
        self.maps = [(w / 2, c) for w, c in tracks] + [(d / 2, c) for d, c in vias]
        self.track_maps = len(tracks)
        self.nets = {}
        for name, pads in by_net.items():
            c = classes[name]
            self.nets[name] = _Net(name, c, tracks.index((c.width, c.clearance)),
                                   len(tracks) + vias.index((c.via_diameter, c.clearance)), pads,
                                   neck=tracks.index((neck, c.clearance)) if c.width > neck + 1e-9 else None)
        self.max_clearance = max(c for _, c in self.maps)
        self.max_extent = max(e for e, _ in self.maps)

    def _grid(self):
        board, opt = self.board, self.opt
        # Snap the pitch to the finest pad pitch so fine-pitch pads sit on grid lines.
        finest, anchor = None, []
        by_ref = {}
        for pad in board.pads:
            if pad.net and pad.ref:
                by_ref.setdefault(pad.ref, []).append(pad)
        for pads in by_ref.values():
            if len(pads) < 6:
                continue
            near = min((math.hypot(a.x - b.x, a.y - b.y) for i, a in enumerate(pads[:64]) for b in pads[i + 1:64]),
                       default=0)
            if near >= 0.3 and (finest is None or near < finest - 1e-6):
                finest, anchor = near, pads
        steps = max(1, round(finest / opt.pitch)) if finest else 1
        p = finest / steps if finest else opt.pitch
        self.p = p
        self.via_cost, self.plane_via_cost = opt.via_mm / p, opt.plane_via_mm / p
        self.weight = opt.heuristic
        self.margin = 0.13 * p   # chord sag of 45-degree steps against round obstacles

        def phase(values):
            residues = [v % p for v in values]
            return max(residues, key=lambda r: sum(abs((q - r + p / 2) % p - p / 2) < 0.01 for q in residues))
        ox = phase([pad.x for pad in anchor]) if anchor else 0.0
        oy = phase([pad.y for pad in anchor]) if anchor else 0.0
        xs = [v for e in board.edges for v in (e[0], e[2])] or [pad.x for pad in board.pads]
        ys = [v for e in board.edges for v in (e[1], e[3])] or [pad.y for pad in board.pads]
        self.edge_clearance = max(opt.edge_clearance, board.rules.get("min_copper_edge_clearance", 0))
        self.reach = self.max_extent + max(self.max_clearance, self.edge_clearance,
                                           board.rules.get("min_hole_clearance", 0)) + self.margin
        border = int((2 * self.max_extent + self.max_clearance + self.margin) / p) + 3
        self.x0 = ox + (math.floor((min(xs) - ox) / p) - border) * p
        self.y0 = oy + (math.floor((min(ys) - oy) / p) - border) * p
        self.W = int(math.ceil((max(xs) - self.x0) / p)) + border + 1
        self.H = int(math.ceil((max(ys) - self.y0) / p)) + border + 1
        self.L = len(board.layers)
        self.HW = self.H * self.W
        if self.L * self.HW > 40_000_000:
            raise ValueError("Board is too large for this grid pitch; raise Options.pitch.")
        self.xs = self.x0 + p * np.arange(self.W)
        self.ys = self.y0 + p * np.arange(self.H)
        shape = (self.L, self.H, self.W)
        self.hard = [np.zeros(shape, np.uint16) for _ in self.maps]
        self.soft = [np.zeros(shape, np.uint16) for _ in self.maps]
        self.hardf = [a.reshape(-1) for a in self.hard]
        self.softf = [a.reshape(-1) for a in self.soft]
        self.hist = np.zeros(shape, np.float32)
        self.vhist = np.zeros((self.H, self.W), np.float32)
        self.base = np.ones(self.L)
        self.smd = np.zeros((self.H, self.W), bool)
        self.vown = np.zeros((self.H, self.W), bool)   # via keep-out of the net being routed
        self.hole_to_hole = board.rules.get("min_hole_to_hole", 0) or 0.25
        self._disks = {}

    def _window_of(self, x1, y1, x2, y2, reach):
        ix0 = max(0, int((min(x1, x2) - reach - self.x0) / self.p))
        iy0 = max(0, int((min(y1, y2) - reach - self.y0) / self.p))
        ix1 = min(self.W, int((max(x1, x2) + reach - self.x0) / self.p) + 2)
        iy1 = min(self.H, int((max(y1, y2) + reach - self.y0) / self.p) + 2)
        return iy0, iy1, ix0, ix1

    def _block(self, distance, iy0, ix0, layers, clearance, own, *, copper=True, maps=None):
        """Mark cells nearer than probe extent + clearance to a fixed shape."""
        for m, (extent, map_clearance) in enumerate(self.maps):
            if maps is not None and m not in maps:
                continue
            limit = extent + (max(clearance, map_clearance) if copper else clearance) + self.margin
            iy, ix = np.nonzero(distance < limit)
            if not iy.size:
                continue
            flat = (iy + iy0) * self.W + ix + ix0
            cells = np.concatenate([layer * self.HW + flat for layer in layers])
            if own is None:
                self.hardf[m][cells] += 1
            else:
                own[m].append(cells)

    def _statics(self):
        board, L = self.board, self.L
        every = tuple(range(L))
        own = {name: [[] for _ in self.maps] for name in self.nets}
        self.pad_cells = {}
        self.tied = {}   # net -> [(map, cells)] of net-tie copper it may touch, each counted on its own
        self.pour, self.pour_gap = {}, {}   # copper pour mask, and how far its fill stays from other nets
        for zone in board.zones:
            if zone.net in self.nets:
                mask = _inside(zone.points, self.xs, self.ys)
                for layer in zone.layers:
                    key = (zone.net, layer)
                    self.pour[key] = self.pour.get(key, False) | mask
                    self.pour_gap[key] = max(self.pour_gap.get(key, 0), zone.clearance + zone.min_thickness / 2)
        hole_clearance = board.rules.get("min_hole_clearance", 0) or 0.25
        for index, pad in enumerate(board.pads):
            reach = _pad_reach(pad)
            iy0, iy1, ix0, ix1 = self._window_of(pad.x, pad.y, pad.x, pad.y, reach + self.reach)
            X, Y = self.xs[None, ix0:ix1], self.ys[iy0:iy1, None]
            d = _pad_sdf(pad, X, Y)
            net = self.nets.get(pad.net)
            layers = pad.layers if pad.plated and pad.layers else every
            clearance = max(net.cls.clearance if net else 0.0, pad.clearance)
            sharers = [name for name in (pad.net, *pad.tie) if name in self.nets] if pad.tie else []
            owner = [[] for _ in self.maps] if sharers else own[pad.net] if net else None
            if pad.plated and pad.layers:
                self._block(d, iy0, ix0, layers, clearance, owner)
            if pad.drill:
                hole = np.hypot(X - pad.x, Y - pad.y) - pad.drill / 2
                self._block(hole, iy0, ix0, every, max(hole_clearance, pad.clearance),
                            owner if net and pad.plated else None, copper=False)
            for m, parts in enumerate(owner if sharers else ()):
                if parts:
                    cells = np.unique(np.concatenate(parts))
                    self.hardf[m][cells] += 1
                    for name in sharers:
                        self.tied.setdefault(name, []).append((m, cells))
            if net:
                # Route ends must sit well inside the pad, not on its rim.
                depth = min(net.cls.width / 2, -0.5 * float(d.min())) if d.min() < 0 else None
                iy, ix = np.nonzero(d < -depth) if depth is not None else (np.array([], int),) * 2
                if not iy.size:
                    iy, ix = np.unravel_index(np.argmin(d), d.shape)
                    iy, ix = np.array([iy]), np.array([ix])
                self.pad_cells[index] = (iy + iy0) * self.W + ix + ix0
            if not pad.drill and pad.layers and float(d.min()) > -self.opt.via_in_pad_min / 2:
                self.smd[iy0:iy1, ix0:ix1] |= d < 0   # too small for a via in the pad
        for net, layer, x1, y1, x2, y2, width in board.tracks:
            iy0, iy1, ix0, ix1 = self._window_of(x1, y1, x2, y2, width / 2 + self.reach)
            d = _segment_sdf(x1, y1, x2, y2, self.xs[None, ix0:ix1], self.ys[iy0:iy1, None]) - width / 2
            known = self.nets.get(net)
            self._block(d, iy0, ix0, (layer,), known.cls.clearance if known else 0.0, own[net] if known else None)
        self.own = {name: [np.unique(np.concatenate(parts)) if parts else np.zeros(0, np.int64) for parts in lists]
                    for name, lists in own.items()}
        for lists in self.own.values():
            for m, cells in enumerate(lists):
                self.hardf[m][cells] += 1
        # Rule areas.
        for zone in board.zones:
            if zone.net or not (zone.no_tracks or zone.no_vias):
                continue
            px, py = [x for x, _ in zone.points], [y for _, y in zone.points]
            iy0, iy1, ix0, ix1 = self._window_of(min(px), min(py), max(px), max(py), self.reach)
            d = _poly_sdf(zone.points, self.xs[None, ix0:ix1], self.ys[iy0:iy1, None])
            if zone.no_tracks:
                self._block(d, iy0, ix0, zone.layers, 0.0, None, copper=False, maps=range(self.track_maps))
            if zone.no_vias:
                self._block(d, iy0, ix0, every, 0.0, None, copper=False,
                            maps=range(self.track_maps, len(self.maps)))
        # Board outline: keep copper off the edge, and everything outside it off limits.
        thin = self.maps[0][0] + self.edge_clearance
        wall = np.zeros((self.H, self.W), bool)
        for x1, y1, x2, y2 in board.edges:
            iy0, iy1, ix0, ix1 = self._window_of(x1, y1, x2, y2, self.reach)
            d = _segment_sdf(x1, y1, x2, y2, self.xs[None, ix0:ix1], self.ys[iy0:iy1, None])
            self._block(d, iy0, ix0, every, self.edge_clearance, None, copper=False)
            wall[iy0:iy1, ix0:ix1] |= d < thin
        self.inside = np.ones((self.H, self.W), bool)
        if board.edges:
            outside = _flood_outside(wall)
            centres = [(int(round((pad.y - self.y0) / self.p)), int(round((pad.x - self.x0) / self.p)))
                       for pad in board.pads if pad.net]
            lost = sum(bool(outside[min(max(iy, 0), self.H - 1), min(max(ix, 0), self.W - 1)]) for iy, ix in centres)
            if lost * 5 > len(centres):   # outline is open: the flood leaked in
                self._say("board outline is not closed; edge keep-out only")
            else:
                self.inside = ~outside
                for m in range(len(self.maps)):
                    self.hard[m][:, outside] += 1

    def _planes(self):
        """Inner layers that are mostly zone become reference planes for their nets."""
        board = self.board
        self.zone, self.zone_full = {}, {}
        self.plane = [False] * self.L
        area = max(1, int(self.inside.sum()))
        shrink = int(max(e for e, _ in self.maps[self.track_maps:]) / self.p) + 1
        for layer in range(1, self.L - 1):
            masks = {}
            for (name, on), mask in self.pour.items():
                if on == layer:
                    masks[name] = mask & self.inside
            if masks and sum(int(m.sum()) for m in masks.values()) >= self.opt.plane_fraction * area:
                self.plane[layer] = True
                for name, mask in masks.items():
                    self.zone[(name, layer)] = _erode(mask, shrink)
                    self.zone_full[(name, layer)] = mask
                    self.nets[name].plane_layers += (layer,)
        # Hand-routed 4-layer boards put signals on a plane layer too. Keep the plane next to
        # the busier component side unbroken as the reference; the others also carry signals.
        planes = [i for i in range(self.L) if self.plane[i]]
        self.mixed, self.guard, self.mended = [False] * self.L, [], 0
        if len(planes) > 1 and self.opt.mixed_cost:
            sides = [sum(1 for pad in board.pads if pad.net and pad.layers == (side,)) for side in (0, self.L - 1)]
            solid = min(planes) if sides[0] >= sides[1] else max(planes)
            self.mixed = [i in planes and i != solid for i in range(self.L)]
            # A plane whose net has no solid plane as well can be cut apart by those signals:
            # these are checked after routing and mended (_mend_planes).
            self.guard = [(name, layer) for name, layer in self.zone
                          if self.mixed[layer] and solid not in self.nets[name].plane_layers]
        named = [f"{board.layers[i]}={'/'.join(n for n, l in self.zone if l == i)}{' (also signals)' * self.mixed[i]}"
                 for i in planes]
        self._say(f"grid {self.W}x{self.H}x{self.L} at {self.p:.4f} mm; nets {len(self.nets)}; "
                  f"reference planes: {', '.join(named) or 'none'}")

    def _electrical(self):
        """Stackup-derived layer costs: a step costs its propagation delay, so slow layers are dearer."""
        board = self.board
        self.models = layer_models(board.layers, board.thickness, board.text,
                                   {i for i in range(self.L) if self.plane[i]})
        width = self.default_width
        delay = [m.delay_ps_per_mm(width) for m in self.models]
        fastest = min(d for i, d in enumerate(delay) if not self.plane[i])
        self.base = np.array([d / fastest for d in delay])
        self.via_delay_ps = homogeneous_delay_ps(board.thickness, sum(m.epsilon_r for m in self.models) / self.L)
        self.pairs = differential_pairs(self.nets)

    # ---- occupancy ---------------------------------------------------------
    def _disk(self, radius):
        key = round(radius, 6)
        if key not in self._disks:
            n = int(radius / self.p) + 1
            dy, dx = np.mgrid[-n:n + 1, -n:n + 1]
            keep = np.hypot(dx, dy) * self.p < radius
            self._disks[key] = (dy[keep] * self.W + dx[keep]).astype(np.int64)
        return self._disks[key]

    def _split(self, net):
        """Full-width track cells, escape-width track cells and via cells (2D) of a net."""
        tracks, narrow, vias = set(), set(), set()
        for index, path in enumerate(net.paths):
            (narrow if index in net.necks else tracks).update(path)
            for a, b in zip(path, path[1:]):
                if a % self.HW == b % self.HW:
                    vias.add(a % self.HW)
        return (np.array(sorted(tracks), np.int64), np.array(sorted(narrow), np.int64),
                np.array(sorted(vias), np.int64))

    def _stamp(self, net):
        tracks, narrow, vias = self._split(net)
        net.stamps = []
        c = net.cls
        for m, (extent, clearance) in enumerate(self.maps):
            gap = max(c.clearance, clearance) + extent + self.margin
            parts = []
            if tracks.size:
                parts.append((tracks[:, None] + self._disk(c.width / 2 + gap)[None, :]).ravel())
            if narrow.size:
                parts.append((narrow[:, None] + self._disk(self.neck_width / 2 + gap)[None, :]).ravel())
            if vias.size:
                disk = self._disk(c.via_diameter / 2 + gap)
                for layer in range(self.L):
                    parts.append((layer * self.HW + vias[:, None] + disk[None, :]).ravel())
            cells = np.unique(np.concatenate(parts)) if parts else np.zeros(0, np.int64)
            self.softf[m][cells] += 1
            net.stamps.append(cells)

    def _ripup(self, net):
        if net.stamps is not None:
            for m, cells in enumerate(net.stamps):
                self.softf[m][cells] -= 1
        net.stamps, net.paths, net.necks, net.failed, net.connections = None, [], set(), 0, 0

    def _conflicts(self, net):
        """Cells of this net's centre line that sit inside another net's keep-off band."""
        if net.stamps is None:
            return np.zeros(0, np.int64), np.zeros(0, np.int64)
        tracks, narrow, vias = self._split(net)
        bad_tracks = np.concatenate([tracks[self.softf[net.t][tracks] > 1],
                                     narrow[self.softf[net.neck or 0][narrow] > 1]])
        bad_vias = vias
        if vias.size:
            worst = np.max([self.softf[net.v][layer * self.HW + vias] for layer in range(self.L)], axis=0)
            bad_vias = vias[worst > 1]
        return bad_tracks, bad_vias

    # ---- routing -----------------------------------------------------------
    def _costs(self, net, t, y0, y1, x0, x1, pres, strict, via_cost, planes_ok):
        sl = np.s_[:, y0:y1, x0:x1]
        soft = self.soft[t][sl]
        blocked = self.hard[t][sl] > 0
        if strict:
            blocked |= soft > 0
        cost = self.base[:, None, None] + pres * soft
        if not strict:   # history only steers the negotiation; the hard pass wants the shortest path
            cost = cost + self.hist[sl]
        cost[blocked] = -1
        goal = np.zeros(cost.shape, bool)
        for layer in range(self.L):
            if self.plane[layer]:
                if layer in net.plane_layers:
                    goal[layer] = self.zone[(net.name, layer)][y0:y1, x0:x1] & ~blocked[layer]
                    cost[layer] = np.where(goal[layer], 1.0, -1.0)
                elif self.mixed[layer] or planes_ok:
                    factor = self.opt.mixed_cost if self.mixed[layer] else self.opt.plane_cost
                    cost[layer] = np.where(blocked[layer], -1.0, cost[layer] * factor)
                else:
                    cost[layer] = -1
        via_soft = self.soft[net.v][sl].sum(0)
        via_blocked = (self.hard[net.v][sl] > 0).any(0) | self.smd[y0:y1, x0:x1] | self.vown[y0:y1, x0:x1]
        if strict:
            via_blocked |= via_soft > 0
        vcost = via_cost + pres * via_soft + (0 if strict else self.vhist[y0:y1, x0:x1])
        vcost[via_blocked] = -1
        h, w = y1 - y0 + 2, x1 - x0 + 2
        padded = np.full((self.L, h, w), -1.0)
        padded[:, 1:-1, 1:-1] = cost
        vpadded = np.full((h, w), -1.0)
        vpadded[1:-1, 1:-1] = vcost
        gpadded = np.zeros((self.L, h, w), np.uint8)
        gpadded[:, 1:-1, 1:-1] = goal
        return padded.ravel().tolist(), vpadded.ravel().tolist(), gpadded, h, w

    def _local(self, cells, y0, y1, x0, x1, w, hw):
        """Global flat cells -> window flat cells (dropping any outside the window)."""
        cells = np.asarray(cells, np.int64)
        layer, rest = np.divmod(cells, self.HW)
        iy, ix = np.divmod(rest, self.W)
        keep = (iy >= y0) & (iy < y1) & (ix >= x0) & (ix < x1)
        return (layer[keep] * hw + (iy[keep] - y0 + 1) * w + ix[keep] - x0 + 1)

    def _pad_global(self, index):
        flat = self.pad_cells[index]
        return np.concatenate([layer * self.HW + flat for layer in self.board.pads[index].layers])

    def _connect(self, net, starts, target, pres, strict, *, extra=None, planes_ok=False, t=None, escape=False,
                 focus=None, plane_goal=True):
        """A* from starts to target cells, or (target None) into the net's plane or ``extra``.

        ``t`` is the track map (width) to route at. ``escape`` instead looks for the
        nearest open ground where the net's full width fits. The window grows on
        failure. ``focus`` (cells) centres the first windows on where the join is expected,
        so a big half-routed net is not searched end to end. Returns global cells or None.
        """
        t = net.t if t is None else t
        if time.monotonic() - self.started > 3 * self.opt.budget_s:
            return None   # hard deadline: fail fast, what is routed stays valid
        to_plane = target is None
        ends = starts if to_plane else np.concatenate([starts, target])
        # A pad far from its plane's outline may need the whole board to reach it.
        for grow in ((1,) if escape else (1, 4, 1000) if to_plane else (1, 4) if focus is None else (1, 4, 0)):
            iy, ix = np.divmod((ends if focus is None or not grow else focus) % self.HW, self.W)
            pad = int(max(3.0 / self.p, 0.3 * max(np.ptp(iy), np.ptp(ix))) * (grow or 1))
            y0, y1 = max(0, int(iy.min()) - pad), min(self.H, int(iy.max()) + pad + 1)
            x0, x1 = max(0, int(ix.min()) - pad), min(self.W, int(ix.max()) + pad + 1)
            cost, vcost, goal, h, w = self._costs(net, t, y0, y1, x0, x1, pres, strict,
                                                  self.plane_via_cost if to_plane else self.via_cost,
                                                  planes_ok)
            hw = h * w
            flat = goal.reshape(-1)
            if escape:
                sl = np.s_[:, y0:y1, x0:x1]
                wide = self.hard[net.t][sl] == 0
                if strict:
                    wide &= self.soft[net.t][sl] == 0
                wide[[plane and not mixed for plane, mixed in zip(self.plane, self.mixed)]] = False
                # Open ground: the cell and its four neighbours all take the full width.
                wide[:, 1:-1, 1:-1] &= wide[:, :-2, 1:-1] & wide[:, 2:, 1:-1] & wide[:, 1:-1, :-2] & wide[:, 1:-1, 2:]
                goal[:] = 0
                goal[:, 1:-1, 1:-1] = wide
                flat[self._local(starts, y0, y1, x0, x1, w, hw)] = 0
                box, weight = (0, 0, w, h), 0.0
            elif to_plane:
                if not plane_goal:   # only copper that already reaches the plane will do
                    flat[:] = 0
                if extra is not None:
                    flat[self._local(extra, y0, y1, x0, x1, w, hw)] = 1
                box, weight = (0, 0, w, h), 0.0     # nearest of many goals: plain Dijkstra
            else:
                local = self._local(target, y0, y1, x0, x1, w, hw)
                flat[local] = 1
                gy, gx = np.divmod(local % hw, w)
                box, weight = (int(gx.min()), int(gy.min()), int(gx.max()), int(gy.max())), self.weight
            begin = self._local(starts, y0, y1, x0, x1, w, hw)
            if not flat.any() or not begin.size:
                continue
            path = False
            if not to_plane:
                # Probe backwards from the target first. A walled-in pad is found out in a few
                # thousand steps instead of a whole-board search, and short hops finish here.
                back = np.zeros(self.L * hw, np.uint8)
                back[begin] = 1
                by, bx = np.divmod(begin % hw, w)
                path = _search(cost, vcost, w, hw, self.L, local.tolist(), bytearray(back.tobytes()),
                               (int(bx.min()), int(by.min()), int(bx.max()), int(by.max())) if begin.size
                               else (0, 0, w, h), self.weight, limit=self.opt.probe)
                if path is None:
                    continue
                path = path and path[::-1]
            if path is False:
                path = _search(cost, vcost, w, hw, self.L, begin.tolist(), bytearray(flat.tobytes()), box, weight)
            if path:
                return [(cell // hw) * self.HW + ((cell % hw) // w - 1 + y0) * self.W + cell % w - 1 + x0
                        for cell in path]
            if y0 == 0 and x0 == 0 and y1 == self.H and x1 == self.W:
                break
        return None

    def _elbow(self, a, b, hard, soft):
        """One diagonal leg plus one straight leg from cell a to cell b, if all of it is free."""
        base, W = a - a % self.HW, self.W
        (y0, x0), (y1, x1) = divmod(a % self.HW, W), divmod(b % self.HW, W)
        dx, dy = x1 - x0, y1 - y0
        sx, sy = (dx > 0) - (dx < 0), (dy > 0) - (dy < 0)
        diagonal = min(abs(dx), abs(dy))
        straight = max(abs(dx), abs(dy)) - diagonal
        tx, ty = (sx, 0) if abs(dx) > abs(dy) else (0, sy)
        k = np.arange(1, diagonal + straight + 1)
        for diagonal_first in (True, False):
            d = np.minimum(k, diagonal) if diagonal_first else k - np.minimum(k, straight)
            cells = base + (y0 + d * sy + (k - d) * ty) * W + x0 + d * sx + (k - d) * tx
            if not (hard[cells].any() or soft[cells].any()):
                return cells.tolist()
        return None

    def _straighten(self, net, path, t=None):
        """Swap staircase runs for single elbows wherever the cells are free of other copper.

        The search has no bend cost, so equal-length paths come out jagged; an elbow is
        never longer than the run it replaces.
        """
        t = net.t if t is None else t
        hard, soft, HW = self.hardf[t], self.softf[t], self.HW
        out, start = [], 0
        for end in range(1, len(path) + 1):
            if end < len(path) and path[end] // HW == path[start] // HW:
                continue
            run = path[start:end]
            out.append(run[0])
            i, last = 0, len(run) - 1
            while i < last:
                j, leg = last, None
                while j > i + 1:
                    leg = self._elbow(run[i], run[j], hard, soft)
                    if leg:
                        break
                    j = (i + j) // 2
                out.extend(leg if leg and j > i + 1 else [run[j]])
                i = j
            start = end
        return out

    def _square_up(self, net, path, target):
        """End a branch straight on the net's own copper instead of with a short jog.

        A jog shorter than the track is wide ends inside the copper it joins; KiCad then
        reports the stub as a dangling track.
        """
        last = len(path) - 1
        if last < 2:
            return path
        step, corner = path[last] - path[last - 1], last - 1
        while corner > 0 and path[corner] - path[corner - 1] == step:
            corner -= 1
        before = path[corner] - path[corner - 1] if corner else 0
        reach = int(1.5 * net.cls.width / self.p) + 1
        if not before or abs(before) >= self.HW or abs(step) >= self.HW or last - corner > reach:
            return path
        ahead = path[corner] + before * np.arange(1, reach + 1)
        free = (self.hardf[net.t][ahead] == 0) & (self.softf[net.t][ahead] == 0)
        joins = np.isin(ahead, target)
        for count in range(reach):
            if not free[count]:
                break
            if joins[count]:
                return path[:corner + 1] + ahead[:count + 1].tolist()
        return path

    def _joinable(self, net, path):
        """Cells of a path that a later branch of the same net may start from or end on.

        Own vias get a no-via ring (hole-to-hole) and only their centre stays joinable,
        so no track ends off-centre inside a via.
        """
        cells = np.array(path, np.int64)
        vias = np.array(sorted({a % self.HW for a, b in zip(path, path[1:]) if a % self.HW == b % self.HW}), np.int64)
        # A later via must not swallow this path's ends off-centre either.
        ends = np.array([path[0] % self.HW, path[-1] % self.HW], np.int64)
        near = (ends[:, None] + self._disk(net.cls.via_diameter / 2 + self.p)[None, :]).ravel()
        self.vown.reshape(-1)[near[~np.isin(near, ends)]] = True
        if not vias.size:
            return cells
        hole = net.cls.via_drill + self.hole_to_hole + self.margin
        ring = (vias[:, None] + self._disk(hole)[None, :]).ravel()
        self.vown.reshape(-1)[ring[~np.isin(ring, vias)]] = True
        pad = np.unique((vias[:, None] + self._disk(net.cls.via_diameter / 2 + self.p)[None, :]).ravel())
        cells = cells[~np.isin(cells % self.HW, pad)]
        centres = np.concatenate([layer * self.HW + vias for layer in range(self.L)
                                  if not self.plane[layer] or self.mixed[layer]])
        return np.concatenate([cells, centres])

    def _reroute(self, net, pres, strict=False, thin=False):
        """Re-route only what is in conflict or missing; the rest of the net stays where it is.

        Work per round then follows the number of conflicts, not the size of the nets.
        """
        keep = None
        if net.stamps is not None and net.paths:
            tracks, vias = self._conflicts(net)
            bad, bad_vias = set(tracks.tolist()), set(vias.tolist())
            keep = [(path, index in net.necks) for index, path in enumerate(net.paths)
                    if bad.isdisjoint(path) and not any(a % self.HW == b % self.HW and a % self.HW in bad_vias
                                                        for a, b in zip(path, path[1:]))]
        self._ripup(net)
        self._route(net, pres, strict, keep=keep, thin=thin)
        self._stamp(net)

    def _route(self, net, pres, strict=False, planes_ok=False, keep=None, thin=False):
        """Route a net against the current maps; own pads are free. ``keep``: paths to build on.

        ``thin`` draws the new connections at the escape width from end to end.
        """
        for m, cells in (*enumerate(self.own[net.name]), *self.tied.get(net.name, ())):
            self.hardf[m][cells] -= 1
        self.vown = np.zeros((self.H, self.W), bool)
        for index in net.pads:   # no via too close to the net's own drilled pads
            pad = self.board.pads[index]
            if pad.drill:
                centre = int(round((pad.y - self.y0) / self.p)) * self.W + int(round((pad.x - self.x0) / self.p))
                reach = pad.drill / 2 + net.cls.via_drill / 2 + self.hole_to_hole + self.margin
                self.vown.reshape(-1)[centre + self._disk(reach)] = True
        try:
            if net.plane_layers:
                self._route_to_plane(net, pres, strict, keep or ())
            elif len(net.pads) > 1:
                self._route_tree(net, pres, strict, planes_ok, keep or (), thin)
        finally:
            for m, cells in (*enumerate(self.own[net.name]), *self.tied.get(net.name, ())):
                self.hardf[m][cells] += 1

    def _route_to_plane(self, net, pres, strict, keep=()):
        """Each pad reaches its plane by its own via, or by a short track to copper that already does."""
        anchors, waiting, loose = [], [], []
        for index in net.pads:
            pad = self.board.pads[index]
            centre = (int(round((pad.y - self.y0) / self.p)), int(round((pad.x - self.x0) / self.p)))
            if any(layer in pad.layers and self.zone[(net.name, layer)][centre] for layer in net.plane_layers):
                cells = self._pad_global(index)   # through-hole pad already sits in its plane
                cells = cells[~np.isin(cells // self.HW, net.plane_layers)]
                if pad.number == "via" and not pad.ref:
                    loose.append(cells)   # a via left on the board: KiCad wants copper on a second layer
                else:
                    anchors.append(cells)
            else:
                waiting.append(index)
        pad_of = {cell: index for index in waiting for cell in self._pad_global(index).tolist()} if keep else {}
        for path, narrow in keep:   # kept drops: only those that reach the plane on their own
            index = pad_of.get(path[0])
            layer = path[-1] // self.HW
            if (index in waiting and layer in net.plane_layers
                    and self.zone[(net.name, layer)].reshape(-1)[path[-1] % self.HW]):
                waiting.remove(index)
                net.connections += 1
                if narrow:
                    net.necks.add(len(net.paths))
                net.paths.append(path)
                joinable = self._joinable(net, path)
                anchors += [joinable[~np.isin(joinable // self.HW, net.plane_layers)], self._pad_global(index)]
        net.connections += len(waiting)
        for _ in range(2):   # second pass: a pad may only reach copper that another pad's drop laid down
            left = []
            for index in waiting:
                starts = self._pad_global(index)
                extra = np.concatenate(anchors) if anchors else None
                path = self._connect(net, starts, None, pres, strict, extra=extra)
                t = net.t
                if not path and net.neck is not None:   # a short plane stub may be drawn narrow
                    path, t = self._connect(net, starts, None, pres, strict, extra=extra, t=net.neck), net.neck
                    if path:
                        net.necks.add(len(net.paths))
                if path:
                    path = self._straighten(net, path, t)
                    net.paths.append(path)
                    joinable = self._joinable(net, path)
                    anchors += [joinable[~np.isin(joinable // self.HW, net.plane_layers)], starts]
                else:
                    left.append(index)
            if len(left) in (0, len(waiting)):
                break
            waiting = left
        net.failed += len(left)
        for cells in loose if anchors else ():
            net.connections += 1
            path = self._connect(net, cells, None, pres, strict, extra=np.concatenate(anchors), plane_goal=False)
            if path:
                net.paths.append(self._straighten(net, path))
                anchors.append(cells)
            else:
                net.failed += 1

    def _route_tree(self, net, pres, strict, planes_ok, keep=(), thin=False):
        """Kruskal over the pads: join the closest components first, skip what will not join."""
        pads = net.pads
        root = {i: i for i in pads}
        cells = {i: self._pad_global(i) for i in pads}

        def find(i):
            while root[i] != i:
                i = root[i]
            return i
        where = {i: (self.board.pads[i].x, self.board.pads[i].y) for i in pads}
        pairs = sorted((math.dist(where[a], where[b]), a, b) for n, a in enumerate(pads) for b in pads[n + 1:])
        tried, groups = set(), len(pads)
        if keep:
            owner = {cell: index for index in pads for cell in cells[index].tolist()}
            for path, narrow in keep:   # in the order they were made, so a path hung on a dropped one drops too
                a, b = owner.get(path[0]), owner.get(path[-1])
                if a is None or (b is None and not narrow):
                    continue
                a = find(a)
                if narrow:
                    net.necks.add(len(net.paths))
                net.paths.append(path)
                joinable = self._joinable(net, path)
                for cell in joinable.tolist():
                    owner.setdefault(cell, a)
                merged = [cells[a], joinable]
                if b is not None and find(b) != a:
                    b = find(b)
                    merged.append(cells.pop(b))
                    root[b] = a
                    groups -= 1
                cells[a] = np.unique(np.concatenate(merged))

        def join():
            nonlocal groups
            for _, pad_a, pad_b in pairs:
                a, b = find(pad_a), find(pad_b)
                if a == b or (a, b) in tried:
                    continue
                if len(cells[a]) < len(cells[b]):
                    a, b = b, a
                path = self._connect(net, cells[a], cells[b], pres, strict, planes_ok=planes_ok,
                                     focus=np.concatenate([self._pad_global(pad_a), self._pad_global(pad_b)]),
                                     t=net.neck if thin else None)
                if path and thin:
                    path = self._straighten(net, path, net.neck)
                    net.necks.add(len(net.paths))
                elif path:
                    path = self._square_up(net, self._straighten(net, path), cells[b])
                    path = self._square_up(net, path[::-1], cells[a])[::-1]
                if path:
                    net.paths.append(path)
                    cells[a] = np.unique(np.concatenate([cells[a], cells.pop(b), self._joinable(net, path)]))
                    root[b] = a
                    groups -= 1
                    if groups == 1:
                        return
                else:
                    tried.update(((a, b), (b, a)))
        if groups > 1:
            join()
        if groups > 1 and net.neck is not None:
            # Neck-down: lone pads the full width cannot leave get a short narrow escape
            # to open ground, then the full-width joining is tried once more.
            escaped = False
            for index in [i for i in cells if i in pads and len(cells[i]) == len(self._pad_global(i))]:
                path = self._connect(net, cells[index], None, pres, strict, t=net.neck, escape=True)
                if path:
                    path = self._straighten(net, path, net.neck)
                    net.necks.add(len(net.paths))
                    net.paths.append(path)
                    cells[index] = np.unique(np.concatenate([cells[index], self._joinable(net, path)]))
                    escaped = True
            if escaped:
                tried.clear()
                join()
        if net.necks:
            # An escape nothing joined would dangle: cut it back to where another path meets it.
            ends = [(path[0], path[-1]) for path in net.paths]
            landed = set(np.concatenate([self._pad_global(index) for index in pads]).tolist())
            kept, necks = [], set()
            for index, path in enumerate(net.paths):
                if index in net.necks:
                    met = {cell for other, pair in enumerate(ends) if other != index for cell in pair}
                    if path[-1] not in landed and path[-1] not in met:   # an escape, not a thin connection
                        last = max((k for k, cell in enumerate(path) if cell in met), default=0)
                        path = path[:last + 1]
                    if len(path) < 2:
                        continue
                    necks.add(len(kept))
                kept.append(path)
            net.paths, net.necks = kept, necks
        net.connections, net.failed = len(pads) - 1, groups - 1

    def run(self):
        self.negotiate()
        self.legalize()
        for _ in range(self.opt.optimize_passes):
            self.optimize()
            self._mend_planes()
            if not any(n.failed for n in self.order) or self._late():
                break
            self.legalize()   # the tidy-up freed room: try the dropped connections again
            self.rescue()
            self._mend_planes()
        self._say(f"done: {sum(n.connections - n.failed for n in self.order)}/"
                  f"{sum(n.connections for n in self.order)} connections routed")
        return self

    def _mend_planes(self):
        """Signals on a shared plane layer may have cut a plane apart: find the pieces and re-join.

        The fill is estimated on the grid: inside the zone outline and far enough from other
        nets' copper. Every via and through-hole pad that relies on the plane must sit in one
        piece. Those that do not are re-routed to the main piece (or by track to copper that
        reaches it); the plane shrinks to that piece for the rest of the run.
        """
        extent, clearance = self.maps[0]
        for name, layer in self.guard:
            net = self.nets[name]
            if net.stamps is None:
                continue
            mine = ((self.own[name][0], self.hardf[0]), (net.stamps[0], self.softf[0]))
            for cells, flat in mine:
                flat[cells] -= 1
            fill = self.zone_full[(name, layer)] & (self.hard[0][layer] == 0) & (self.soft[0][layer] == 0)
            for cells, flat in mine:
                flat[cells] += 1
            fill = _erode(fill, int(math.ceil(max(0.0, self.pour_gap[(name, layer)] - extent - clearance) / self.p)))
            drops = [index for index, path in enumerate(net.paths) if path[-1] // self.HW == layer]
            seeds = [net.paths[index][-1] % self.HW for index in drops]
            for index in net.pads:
                pad = self.board.pads[index]
                centre = int(round((pad.y - self.y0) / self.p)) * self.W + int(round((pad.x - self.x0) / self.p))
                if layer in pad.layers and self.zone[(name, layer)].reshape(-1)[centre]:
                    seeds.append(centre)
            if len(seeds) < 2:
                continue
            labels, image = _pieces(fill, seeds)
            found = [label for label in labels if label]
            main = max(set(found), key=found.count) if found else 0
            if all(label == main for label in labels):
                continue
            self.zone[(name, layer)] = self.zone[(name, layer)] & (image == main)
            cut = {index for index, label in zip(drops, labels) if label != main}
            keep = [(path, index in net.necks) for index, path in enumerate(net.paths) if index not in cut]
            self._ripup(net)
            self._route(net, 0.0, strict=True, keep=keep)
            self._stamp(net)
            self.mended += sum(label != main for label in labels)
        if self.mended:
            self._say(f"planes: {self.mended} plane connections re-joined after signals cut their plane")

    def _score(self, net):
        segments, vias = self.geometry(net)
        return net.failed, sum(math.dist(a, b) for _, a, b, _ in segments) / self.p + self.via_cost * len(vias)

    def optimize(self):
        """Re-route each net by pure shortest path around the others; keep it only if no worse."""
        better, self.weight = 0, self.opt.tidy_heuristic
        for net in self.order:
            if not net.paths or self._late():
                continue
            saved, before = self._state(net), self._score(net)
            self._ripup(net)
            self._route(net, 0.0, strict=True)
            self._stamp(net)
            after = self._score(net)
            if after > before:
                self._restore(net, saved)
            elif after < before:
                better += 1
        self.weight = self.opt.heuristic
        self._say(f"tidy-up: {better} nets shortened")

    def negotiate(self):
        opt = self.opt
        size = lambda n: max(np.ptp([self.board.pads[i].x for i in n.pads]),  # noqa: E731
                             np.ptp([self.board.pads[i].y for i in n.pads]))
        # Plane drops first (they need the via sites next to their pads), then short nets.
        paired = {name for pair in self.pairs for name in pair}
        order = self.order = sorted((n for n in self.nets.values() if len(n.pads) > 1 or n.plane_layers),
                                    key=lambda n: (not n.plane_layers, n.name not in paired, size(n)))
        pres, todo, rounds, best, stale = opt.pres0, order, 0, None, 0
        spread = self._disk(opt.hist_spread)
        rng = np.random.default_rng(opt.seed)
        for rounds in range(1, opt.iterations + 1):
            if opt.seed is not None and rounds > 1:
                todo = [todo[i] for i in rng.permutation(len(todo))]
            for net in todo:
                if opt.partial_rounds:
                    self._reroute(net, pres)
                else:
                    self._ripup(net)
                    self._route(net, pres)
                    self._stamp(net)
            todo = []
            for net in order:
                bad_tracks, bad_vias = self._conflicts(net)
                if bad_tracks.size or bad_vias.size:
                    # The whole neighbourhood gets dearer, so nets with a choice drift away.
                    hot = np.unique((bad_tracks[:, None] + spread[None, :]).ravel())
                    self.hist.reshape(-1)[hot] += opt.hist_step
                    self.vhist.reshape(-1)[np.unique((bad_vias[:, None] + spread[None, :]).ravel())] += opt.hist_step
                    todo.append(net)
            self._say(f"round {rounds}: {len(todo)} nets in conflict, "
                      f"{sum(n.failed for n in order)} connections blocked")
            stale = 0 if best is None or len(todo) < best else stale + 1
            best = len(todo) if best is None else min(best, len(todo))
            if not todo or self._late() or stale >= opt.stall_rounds:
                break
            pres = min(pres * opt.pres_mult, opt.pres_cap)
        self.rounds = rounds

    def _late(self):
        return time.monotonic() - self.started > self.opt.budget_s

    def _in_conflict(self, net):
        return any(cells.size for cells in self._conflicts(net))

    def legalize(self):
        """Hard pass: whatever still overlaps is re-routed around everything else, or dropped."""
        for _ in range(3):
            pending = [n for n in self.order if self._in_conflict(n) or n.failed]
            if not pending:
                break
            pending.sort(key=lambda n: -sum(c.size for c in self._conflicts(n)))
            for net in pending:
                # Past the budget only real conflicts are still fixed; dropped connections stay dropped.
                if self._in_conflict(net) or (net.failed and not self._late()):
                    self._reroute(net, 0.0, strict=True)
        self._say(f"hard pass: {sum(n.failed for n in self.order)} connections unrouted")

    def _state(self, net):
        return net.paths, net.necks, net.failed, net.connections

    def _restore(self, net, saved):
        self._ripup(net)
        net.paths, net.necks, net.failed, net.connections = saved
        self._stamp(net)

    def rescue(self):
        """Finish what the hard pass dropped, keeping the board free of conflicts throughout.

        Rip-up and re-route with a queue: a dropped net is routed straight through whatever
        is in its way, the nets it crossed are ripped and re-routed around it, and any of
        those that no longer fit take their turn in the queue. The best state seen is the
        one that stays. Last resort for what is still open: a short run on a reference
        plane layer, reported as such.
        """
        opt = self.opt

        def dropped():
            return sum(n.failed for n in self.order)
        best, kept = dropped(), {n.name: self._state(n) for n in self.order}
        queue = deque(sorted((n for n in self.order if n.failed), key=lambda n: n.failed))
        turns = opt.rescue_turns * len(queue)
        rng = np.random.default_rng(opt.seed)
        while queue and turns > 0 and not self._late():
            turns -= 1
            net = queue.popleft()
            if not net.failed:
                continue
            # Jitter keeps two nets from trading places forever. A signal that was dropped at
            # its class width comes back at the escape width.
            self._reroute(net, opt.rip_cost * (0.5 + rng.random()), thin=(
                net.neck is not None and not net.plane_layers and net.cls.width <= 2 * self.neck_width + 1e-9))
            tracks, vias = self._conflicts(net)
            through = np.concatenate([layer * self.HW + vias for layer in range(self.L)])
            blockers = [other for other in self.order if other is not net and other.stamps is not None
                        and (np.isin(tracks, other.stamps[net.t]).any()
                             or (net.neck is not None and np.isin(tracks, other.stamps[net.neck]).any())
                             or np.isin(through, other.stamps[net.v]).any())]
            for other in blockers:   # they give way: only their crossed paths are re-routed
                self._reroute(other, 0.0, strict=True)
                if other.failed:
                    queue.append(other)
            if dropped() < best:
                best, kept = dropped(), {n.name: self._state(n) for n in self.order}
        for net in self.order:   # back to the best state seen
            if self._state(net)[0] is not kept[net.name][0]:
                self._restore(net, kept[net.name])
        for net in [n for n in self.order if n.failed and n.neck is not None and not n.plane_layers
                    and n.cls.width <= 2 * self.neck_width + 1e-9]:
            # A signal still open: draw the missing connection at the escape width end to end.
            saved, before = self._state(net), net.failed
            keep = [(path, index in net.necks) for index, path in enumerate(net.paths)]
            self._ripup(net)
            self._route(net, 0.0, strict=True, keep=keep, thin=True)
            self._stamp(net)
            if net.failed >= before:
                self._restore(net, saved)
        if opt.plane_cost and any(self.plane):
            for net in [n for n in self.order if n.failed]:
                saved, before = self._state(net), net.failed
                self._ripup(net)
                self._route(net, 0.0, strict=True, planes_ok=True)
                self._stamp(net)
                if net.failed >= before:
                    self._restore(net, saved)
        self._say(f"rescue: {dropped()} connections unrouted")

    # ---- output ------------------------------------------------------------
    def _xy(self, cell):
        rest = cell % self.HW
        return (round(self.x0 + (rest % self.W) * self.p, 6), round(self.y0 + (rest // self.W) * self.p, 6))

    def geometry(self, net):
        """(segments, vias): segments are (layer, (x1, y1), (x2, y2), width); vias are (x, y)."""
        segments, vias = set(), set()
        for index, path in enumerate(net.paths):
            width = self.neck_width if index in net.necks else net.cls.width
            run_start, previous, direction = path[0], path[0], None
            for cell in path[1:] + [None]:
                step = None if cell is None else cell - previous
                via = cell is not None and cell % self.HW == previous % self.HW
                if step != direction or via:
                    if direction is not None and run_start != previous:
                        a, b = self._xy(run_start), self._xy(previous)
                        segments.add((run_start // self.HW, min(a, b), max(a, b), width))
                    run_start, direction = previous, step
                if via:
                    vias.add(self._xy(cell))
                    run_start, direction = cell, None
                previous = cell
        return sorted(segments), sorted(vias)

    def copper(self):
        items = []
        for net in self.nets.values():
            segments, vias = self.geometry(net)
            for layer, a, b, width in segments:
                items.append(CopperItem(str(uuid.uuid4()), "segment", net.name, self.board.layers[layer],
                                        a, b, width))
            for position in vias:
                items.append(CopperItem(str(uuid.uuid4()), "via", net.name, "F.Cu", position, None,
                                        net.cls.via_diameter, net.cls.via_drill))
        return tuple(items)

    def report(self):
        nets = {}
        for net in self.nets.values():
            if not net.connections:
                continue
            segments, vias = self.geometry(net)
            length, necked = {}, 0.0
            for layer, a, b, wide in segments:
                name = self.board.layers[layer]
                length[name] = length.get(name, 0.0) + math.dist(a, b)
                necked += math.dist(a, b) * (wide < net.cls.width)
            by_name = {m.name: m for m in self.models}
            width = net.cls.width
            delay = sum(v * by_name[k].delay_ps_per_mm(width) for k, v in length.items()) + len(vias) * self.via_delay_ps
            impedance = {k: by_name[k].impedance_ohm(width) for k in length}
            nets[net.name] = {"connections": net.connections, "unrouted": net.failed, "vias": len(vias),
                              "length_mm": {k: round(v, 3) for k, v in length.items()},
                              "to_plane": [self.board.layers[i] for i in net.plane_layers],
                              "width_mm": width, "necked_mm": round(necked, 2), "delay_ps": round(delay, 1),
                              "impedance_ohm": {k: v and round(v, 1) for k, v in impedance.items()}}
        total = sum(n["connections"] for n in nets.values())
        failed = sum(n["unrouted"] for n in nets.values())
        planes = [self.board.layers[i] for i in range(self.L) if self.plane[i]]
        width = self.default_width
        stackup = [{"layer": m.name, "kind": m.kind, "epsilon_r": round(m.epsilon_r, 3),
                    "height_mm": round(m.height_mm, 4), "assumed": m.assumed,
                    "delay_ps_per_mm": round(m.delay_ps_per_mm(width), 3),
                    "impedance_ohm": (z := m.impedance_ohm(width)) and round(z, 1),
                    "width_for_50_ohm_mm": m.width_for(50)} for m in self.models]
        pairs = [{"nets": pair, "skew_ps": round(abs(nets[pair[0]]["delay_ps"] - nets[pair[1]]["delay_ps"]), 1),
                  "coupled": False} for pair in self.pairs if pair[0] in nets and pair[1] in nets]
        return {"engine": "vela-native", "pitch_mm": round(self.p, 6), "grid": [self.W, self.H, self.L],
                "reference_planes": planes,
                "mixed_planes": [self.board.layers[i] for i in range(self.L) if self.mixed[i]],
                "plane_connections_mended": self.mended,
                "electrical": {"model": "quasi-static estimate, not sign-off",
                               "stackup_assumed": any(m.assumed for m in self.models),
                               "track_width_mm": width, "layers": stackup, "pairs": pairs,
                               "necked_mm": round(sum(n["necked_mm"] for n in nets.values()), 2),
                               "signal_on_plane_mm": round(sum(v for n in nets.values() for k, v in
                                                               n["length_mm"].items() if k in planes and not n["to_plane"]), 2)},
                "rounds": self.rounds, "seconds": round(time.monotonic() - self.started, 1),
                "connections": total, "unrouted": failed,
                "completion_pct": round(100 * (total - failed) / total, 1) if total else 100.0,
                "vias": sum(n["vias"] for n in nets.values()),
                "length_mm": round(sum(sum(n["length_mm"].values()) for n in nets.values()), 1),
                "nets": nets}


def route_board(board: Board, options: Options | None = None, obstacles=()) -> Router:
    """Best of up to ``options.tries`` runs that differ only in re-route order.

    ponytail: restarts on top of the negotiation; the order still matters on dense boards.
    A finer grid was tried instead (tracks pack 14 % tighter): 7x slower, no gain.
    """
    options = options or Options()
    started, best = time.monotonic(), None
    tries = max(1, options.tries)
    for attempt in range(tries):
        left = options.budget_s - (time.monotonic() - started)
        if best and left < 10:
            break
        seed = None if options.seed is None else options.seed + attempt
        router = Router(board, replace(options, seed=seed, budget_s=max(left, 10)), obstacles).run()
        score = (sum(n.failed for n in router.order), sum(router._score(n)[1] for n in router.order))
        if best is None or score < best[0]:
            best = (score, router)
        if not score[0] or options.seed is None:
            break
    best[1].started = started
    return best[1]


def main(argv=None):
    """Route a copy of a saved board and, given kicad-cli, let KiCad DRC judge it."""
    import argparse
    import json
    from pathlib import Path
    import shutil

    from .candidate import blocking_reasons, candidate_text, route_issues
    from .copper_text import extract_copper_text
    from .errors import CapabilityError, ValidationError
    from .kicad_cli import KiCadCli
    from .vela_board import load_board

    parser = argparse.ArgumentParser(prog="python -m velatrace.vela_engine", description=main.__doc__)
    parser.add_argument("board", type=Path)
    parser.add_argument("--out", type=Path, required=True, help="new folder for the routed copy and report")
    parser.add_argument("--kicad-cli", type=Path, help="kicad-cli for the independent DRC verdict")
    parser.add_argument("--pitch", type=float, default=Options.pitch)
    parser.add_argument("--budget", type=float, default=Options.budget_s, help="seconds")
    args = parser.parse_args(argv)
    board = load_board(args.board)
    cli = KiCadCli(args.kicad_cli) if args.kicad_cli else None
    obstacles, notes = (), []
    if cli:
        try:   # copper text is only known through KiCad's own rendering
            obstacles = extract_copper_text(args.board, cli).obstacles
        except (CapabilityError, ValidationError) as exc:
            notes.append(f"copper text not modelled: {exc}")
    router = route_board(board, Options(pitch=args.pitch, budget_s=args.budget, log=lambda text: print(text, flush=True)), obstacles)
    report = router.report()
    report["notes"] = notes
    folders = {name: args.out / name for name in ("routed", "baseline")}
    for name, folder in folders.items():
        folder.mkdir(parents=True, exist_ok=False)
        for suffix in (".kicad_pro", ".kicad_dru"):
            if args.board.with_suffix(suffix).is_file():
                shutil.copy(args.board.with_suffix(suffix), folder / (args.board.stem + suffix))
        text = candidate_text(board.text, router.copper()) if name == "routed" else board.text
        (folder / args.board.name).write_text(text, encoding="utf-8")
    if cli:
        baseline, routed = (cli.drc(folders[name] / args.board.name) for name in ("baseline", "routed"))
        blocking, _ = route_issues(baseline, routed)
        report["drc"] = {"unconnected_before": baseline.unconnected, "unconnected": routed.unconnected,
                         "blocking": blocking, "reasons": list(blocking_reasons(baseline, routed)),
                         "accepted": not routed.unconnected and not blocking}
    (args.out / "report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k not in ("nets", "electrical")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
