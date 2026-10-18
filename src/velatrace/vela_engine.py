"""Native Vela-routing engine (research): multilayer grid A* with negotiated congestion.

Independent implementation from published ideas only: A* maze search on a uniform
grid with 45-degree moves, through vias, PathFinder-style negotiated congestion
(McMurchie & Ebeling) and a final hard pass that never leaves two nets in conflict.
Inner layers that are mostly copper zone are kept as solid reference planes: no
signal is routed on them, and pads of the plane's net drop a via into it.

Needs numpy. The plugin's Route board uses it through ``vela_router.VelaRouter``;
every result must still pass KiCad DRC, which stays the only judge.

ponytail: pure-Python search loop, roughly 1e5 expansions/s. Move the inner loop
to a compiled kernel if boards beyond a few hundred nets matter.
"""
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
import heapq
import math
import re
import time
import uuid

import numpy as np

from .candidate import CopperItem
from .errors import RoutingCancelled
from .stackup import homogeneous_delay_ps
from .vela_board import Board, Pad
from .vela_electrical import differential_pairs, layer_models

_SQRT2 = math.sqrt(2)


@dataclass
class Options:
    pitch: float = 0.1            # target grid pitch in mm; snapped to the finest pad pitch
    via_mm: float = 4.0           # track length a via is worth
    iterations: int = 40          # negotiated-congestion rounds before the hard pass
    edge_clearance: float = 0.25  # floor under the board's own copper-to-edge rule, mm
    budget_s: float = 900.0
    heuristic: float = 1.2        # A* weight while negotiating; >1 trades length for speed
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
    defer_after: int = 3          # a net ripped this often in the rescue waits for its final round
    stall_rounds: int = 5         # stop negotiating after this many rounds without progress
    probe: int = 1500             # expansions of the cheap backwards probe before the real search
    optimize_passes: int = 2
    neck_width: float = 0.15      # escape width for pads the class width cannot reach, mm
    seed: int | None = 1          # shuffles the re-route order each round to break ties; None = fixed
    tries: int = 3                # route_board: re-route orders tried until one routes everything
    hist_spread: float = 0.4      # mm around a conflict that gets the history penalty
    relieve: bool = True          # re-route tracks that starve a pad's thermal relief
    couple: bool = True           # route name-detected differential pairs as one coupled trunk
    skip_pairs: tuple = ()        # pairs routed as two nets anyway (route_board: their trunk gave way)
    skew_ps: float = 1.0          # delay matching target within each pair, ps; 0 = no tuning
    meander_mm: float = 1.0       # tallest trombone bump of the delay matching, mm
    log: object = None            # callable(str) for progress
    cancel: object = None         # threading.Event; once set, routing stops with RoutingCancelled


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
    links: list = field(default_factory=list)   # off-grid straight tracks (layer, a, b, width), mm; kept


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


def _offset(points, distance):
    """Polyline (numpy points, 45/90-degree corners) shifted sideways by ``distance`` mm, mitred; None if it folds."""
    dirs = [(b - a) / np.linalg.norm(b - a) for a, b in zip(points, points[1:])]
    normals = [np.array((-d[1], d[0])) for d in dirs]
    out = []
    for k, point in enumerate(points):
        n1, n2 = normals[max(k - 1, 0)], normals[min(k, len(normals) - 1)]
        if n1 @ n2 < -1e-9:
            return None   # a turn sharper than 90 degrees
        shift = (n1 + n2) / (1 + n1 @ n2)
        out.append(tuple(np.round(point + distance * shift, 6).tolist()))
    if any((np.subtract(q, p) @ d) <= 1e-6 for p, q, d in zip(out, out[1:], dirs)):
        return None   # a leg shorter than the corner it turns
    return out


def _along(line, point, reach):
    """Arc length along a polyline to the foot of ``point``, if the point lies within ``reach`` of it."""
    best, walked = None, 0.0
    for p, q in zip(line, line[1:]):
        (px, py), (qx, qy), span = p, q, math.dist(p, q)
        t = max(0.0, min(1.0, ((point[0] - px) * (qx - px) + (point[1] - py) * (qy - py)) / (span * span or 1)))
        d = math.dist(point, (px + t * (qx - px), py + t * (qy - py)))
        if d <= reach and (best is None or d < best[0]):
            best = (d, walked + t * span)
        walked += span
    return best and best[1]


def _cut(line, start, end):
    """The part of a polyline between arc lengths ``start`` and ``end``."""
    out, walked = [], 0.0

    def add(point):
        if not out or math.dist(out[-1], point) > 1e-6:
            out.append(point)
    for p, q in zip(line, line[1:]):
        span = math.dist(p, q)
        for at in (start, end):
            if walked - 1e-9 <= at <= walked + span + 1e-9 and (at == end or not out):
                t = (at - walked) / (span or 1)
                add((round(p[0] + t * (q[0] - p[0]), 6), round(p[1] + t * (q[1] - p[1]), 6)))
                if at == end:
                    return out
        if out:
            add(tuple(q))
        walked += span
    return out


def _gap(a, b, width):
    """Smallest copper gap between two polylines of tracks ``width`` wide (exact for disjoint legs)."""
    def nearest(points, line):
        X, Y = np.array([x for x, _ in points]), np.array([y for _, y in points])
        return min(float(_segment_sdf(*p, *q, X, Y).min()) for p, q in zip(line, line[1:]))
    return min(nearest(a, b), nearest(b, a)) - width


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
    # Octile distance to the goal box, weighted; per column and per row.
    hx = [(gx0 - x if x < gx0 else (x - gx1 if x > gx1 else 0)) * weight for x in range(w)]
    hy = [(gy0 - y if y < gy0 else (y - gy1 if y > gy1 else 0)) * weight for y in range(hw // w)]
    heap = []
    for s in starts:
        if cost[s] < 0 and not goal[s]:
            continue
        g[s] = 0.0
        r = s % hw
        a, b = hx[r % w], hy[r // w]
        heap.append((a + b - 0.5857864 * (a if a < b else b), 0.0, s))
    heapq.heapify(heap)
    straight = ((1, 1, 0), (-1, -1, 0), (w, 0, 1), (-w, 0, -1))
    diagonal = ((w + 1, 1, 1), (w - 1, -1, 1), (-w + 1, 1, -1), (-w - 1, -1, -1))
    bases = tuple(range(0, layers * hw, hw))
    push, pop = heapq.heappush, heapq.heappop
    while heap:
        _, gi, i = pop(heap)
        if g[i] < 0:
            continue
        # Expanded once, never again (g < 0 also fails every ``ng < g[j]`` below). Re-opening
        # under a weighted heuristic re-expanded congested windows ~50 times per cell; without
        # it the path stays within ``weight`` of the cheapest.
        g[i] = -1.0
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
        for off, mx, my in straight:
            j = i + off
            c = cost[j]
            if c < 0:
                continue
            ng = gi + c
            if ng < g[j]:
                g[j] = ng
                came[j] = i
                a, b = hx[x + mx], hy[y + my]
                push(heap, (ng + a + b - 0.5857864 * (a if a < b else b), ng, j))
        for off, mx, my in diagonal:
            j = i + off
            c = cost[j]
            if c < 0:
                continue
            ng = gi + c * _SQRT2
            if ng < g[j]:
                g[j] = ng
                came[j] = i
                a, b = hx[x + mx], hy[y + my]
                push(heap, (ng + a + b - 0.5857864 * (a if a < b else b), ng, j))
        vc = vcost[r]
        if vc >= 0:
            here = i - r
            a, b = hx[x], hy[y]
            h = a + b - 0.5857864 * (a if a < b else b)
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
        # Own copy of the pad list: coupled trunks of differential pairs are added as pads.
        self.board, self.opt = replace(board, pads=list(board.pads)), options or Options()
        self.started = time.monotonic()
        self.obstacles, self.kicad_s = tuple(obstacles), 0.0   # kicad_s: time spent in KiCad's checks
        self.rips = {}        # rescue: how often each net lost a connection as a rip victim
        self.surcharge = {}   # net name -> extra price per step of pushing through its copper
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
        self._couple()

    def _stop_if_cancelled(self):
        if self.opt.cancel is not None and self.opt.cancel.is_set():
            raise RoutingCancelled("Cancelled; nothing was written to the board.")

    def _say(self, text):
        self._stop_if_cancelled()
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
        self.pairs, self.couples = differential_pairs(by_net), {}
        for pair in self.pairs if self.opt.couple else ():
            a, b = (classes[name] for name in pair)
            clearance = max(a.clearance, b.clearance)
            width = max(a.diff_pair_width or a.width, b.diff_pair_width or b.width, board.rules.get("min_track_width", 0))
            # The gap never goes below the clearance plus the grid's keep-off margin (0.02 mm): a
            # breakout must still find its own track inside the partner's keep-off band.
            gap = max(a.diff_pair_gap, b.diff_pair_gap, clearance + 0.02 + max(0.0, (max(a.width, b.width) - width) / 2))
            self.couples[pair] = (width, gap, clearance)
        tracks = sorted({(c.width, c.clearance) for c in classes.values()}
                        | {(neck, c.clearance) for c in classes.values() if c.width > neck + 1e-9}
                        | {(2 * w + g, c) for w, g, c in self.couples.values()} or {(0.2, 0.2)})
        # A pair's centreline is searched on a virtual class as wide as both tracks and their gap.
        self.couples = {pair: (w, g, c, tracks.index((2 * w + g, c))) for pair, (w, g, c) in self.couples.items()}
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
        # Reach and grid border as without pairs; a trunk is checked exactly (_link_clear) anyway.
        plain = [(e, c) for m, (e, c) in enumerate(self.maps) if m not in {k[3] for k in self.couples.values()}
                 or (2 * e, c) in {(n.cls.width, n.cls.clearance) for n in self.nets.values()}]
        self.max_clearance = max(c for _, c in plain)
        self.max_extent = max(e for e, _ in plain)

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
        for zone in board.zones:   # KiCad fills nothing inside a no-pour rule area
            if not zone.net and zone.no_pour:
                mask = _inside(zone.points, self.xs, self.ys)
                for key in self.pour:
                    if key[1] in zone.layers:
                        self.pour[key] = self.pour[key] & ~mask
        for index, pad in enumerate(board.pads):
            self._static_pad(index, pad, own)
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
        self._spokes()

    def _static_pad(self, index, pad, own):
        """Fixed copper of one pad: blocked for other nets (into ``own``), its route-end cells, no-via area."""
        every = tuple(range(self.L))
        hole_clearance = self.board.rules.get("min_hole_clearance", 0) or 0.25
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
        if not pad.drill and pad.layers and pad.number != "pair" and float(d.min()) > -self.opt.via_in_pad_min / 2:
            self.smd[iy0:iy1, ix0:ix1] |= d < 0   # too small for a via in the pad

    def _spokes(self):
        """Thermal-spoke corridors of pads in their own pour: ``self.corridors`` [(pad, layer, gap, [(bands, axis)])].

        KiCad ties such a pad to the pour with up to four spokes across the thermal gap and
        reports a starved thermal when only one reaches the fill. A corridor is a spoke's axis
        from the pad's rim across the gap and half the fill's minimum width beyond; its band per
        map holds the cells where a probe's centre line would cut it. Corridors that fixed
        copper of another net (or the outline) already cuts are left out: no routing helps them.
        """
        self.corridors = []
        relief = {}   # (net, layer) -> [mode, thermal gap, spoke width, fill clearance, min thickness]
        for zone in self.board.zones:
            if zone.net in self.nets:
                for layer in zone.layers:
                    old = relief.setdefault((zone.net, layer), [zone.connect, 0.0, 0.0, 0.0, 0.0])
                    for i, v in enumerate((zone.thermal_gap, zone.spoke_width, zone.clearance, zone.min_thickness)):
                        old[i + 1] = max(old[i + 1], v)
        mine, fills = {}, {}   # net -> its own fixed copper (map 0); (net, layer) -> fill around fixed copper
        for pad in self.board.pads if relief and self.opt.relieve else ():
            if pad.net not in self.nets or not pad.plated:
                continue
            cy = min(max(int(round((pad.y - self.y0) / self.p)), 0), self.H - 1)
            cx = min(max(int(round((pad.x - self.x0) / self.p)), 0), self.W - 1)
            for layer in pad.layers:
                mode, gap, spoke, clearance, thin = relief.get((pad.net, layer), ("no", 0, 0, 0, 0))
                thermal = (pad.zone_connect == 1 or (pad.zone_connect == 3 and pad.drill)
                           or (pad.zone_connect == -1 and (mode == "thermal" or (mode == "thru_hole_only" and pad.drill))))
                pour = self.pour[(pad.net, layer)].reshape(-1) if thermal else None
                if pour is None or not pour[cy * self.W + cx]:
                    continue
                if pad.net not in mine:
                    mine[pad.net] = np.zeros(self.L * self.HW, bool)
                    mine[pad.net][self.own[pad.net][0]] = True
                if (pad.net, layer) not in fills:
                    free = self.pour[(pad.net, layer)] & (self.hard[0][layer] <= mine[pad.net].reshape(self.hard[0].shape)[layer])
                    fills[(pad.net, layer)] = _erode(free, int(math.ceil(
                        max(0.0, clearance + thin / 2 - self.maps[0][0] - self.maps[0][1]) / self.p)))
                first, ts = pad.shapes[0], np.linspace(0, _pad_reach(pad), 64)
                round_pad = first[0] == "rrect" and first[3] == first[4] and first[5] >= first[3] / 2 - 1e-9
                turn = pad.spoke_angle if pad.spoke_angle is not None else 45.0 if round_pad else 90.0
                group = []
                for quarter in range(4):
                    a = math.radians(pad.angle + turn + 90 * quarter)
                    dx, dy = math.cos(a), -math.sin(a)   # Y is down
                    # Pad extent along the spoke and across it (KiCad narrows a spoke to the pad).
                    edge, side = (float(ts[_pad_sdf(pad, pad.x + ts * ex, pad.y + ts * ey) <= 0].max(initial=0))
                                  for ex, ey in ((dx, dy), (-dy, dx)))
                    width = min(spoke, 2 * side) if side > 0 else spoke
                    x1, y1 = pad.x + edge * dx, pad.y + edge * dy
                    x2, y2 = x1 + (gap + thin / 2) * dx, y1 + (gap + thin / 2) * dy
                    iy0, iy1, ix0, ix1 = self._window_of(x1, y1, x2, y2, thin / 2 + self.reach)
                    # The axis across the gap and into the fill: copper of another net within a clearance
                    # plus a quarter of the fill minimum width cuts the spoke. ponytail: fitted on the bench
                    # boards against KiCad fills: none of it missed cut spokes, half of it flagged kept ones.
                    d = _segment_sdf(x1, y1, x2, y2, self.xs[None, ix0:ix1], self.ys[iy0:iy1, None])
                    iy, ix = np.nonzero(d < self.p)
                    flat = (iy + iy0) * self.W + ix + ix0
                    cells = layer * self.HW + flat
                    if (not pour[flat].any() or (self.hardf[0][cells] > mine[pad.net][cells]).any()
                            or not self._reaches_fill(pad, layer, x2, y2, gap, thin, fills[(pad.net, layer)],
                                                      mine[pad.net].reshape(self.hard[0].shape)[layer])):
                        continue
                    bands = []
                    for extent, map_clearance in self.maps:
                        iy, ix = np.nonzero(d < extent + max(clearance, map_clearance) + thin / 4 + self.margin)
                        bands.append(layer * self.HW + (iy + iy0) * self.W + ix + ix0)
                    group.append((bands, (pad.x, pad.y, x2, y2, width / 2)))
                if group:
                    self.corridors.append((pad, layer, gap, group))

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

    # ---- differential pairs --------------------------------------------------
    def _couple(self):
        """Coupled trunk of each differential pair (idea from KiCadRoutingTools: centreline plus offset).

        The centreline is searched on one layer against the fixed copper, on the pair's virtual
        class 2w + gap, then split into the two tracks at +-(w + gap)/2. Each track becomes fixed
        copper of its net, a pad-like trunk; the nets' own routes then only join their pads to it
        (the uncoupled breakout, vias allowed). The first trunk whose breakouts route on an empty
        board is kept; a pair without one routes as two single nets.
        """
        self.virtual, self.coupled, self.trunks, self.tuning = set(), {}, {}, {}
        for pair, (w, g, c, m) in self.couples.items():
            if pair in self.opt.skip_pairs:
                self.coupled[pair] = {"coupled": False, "reason": "its trunk gave way in a first try; routed as two nets"}
                continue
            nets, reason = [self.nets[name] for name in pair], "no candidate"
            for found in self._trunk(pair, w, g, m):
                if isinstance(found, str):
                    reason = found
                    continue
                layer, lines, crossed = found
                pads = {name: self._add_trunk(name, line, layer, w) for name, line in zip(pair, lines)}
                if self._breakouts(pair, pads):
                    self.coupled[pair] = {"coupled": True, "layer": self.board.layers[layer], "width_mm": w,
                                          "gap_mm": round(_gap(*lines, w), 4), "crossed": crossed, "pads": pads}
                    break
                for name, index in pads.items():
                    self._drop_trunk(self.nets[name], index)
                reason = "the breakouts cannot reach the trunk"
            else:
                self.coupled[pair] = {"coupled": False, "reason": reason}
        if self.coupled:
            self._say("pairs: " + "; ".join("/".join(pair) + (f" coupled on {info['layer']}" if info["coupled"]
                                                              else f": {info['reason']}")
                                            for pair, info in self.coupled.items()))

    def _add_trunk(self, name, line, layer, width):
        """One track of a trunk as fixed copper (a pad) of net ``name``; returns its pad index."""
        ox, oy = line[len(line) // 2]
        shapes = tuple(("line", x1 - ox, y1 - oy, x2 - ox, y2 - oy, width) for (x1, y1), (x2, y2) in zip(line, line[1:]))
        index = len(self.board.pads)
        self.board.pads.append(Pad("", "pair", name, ox, oy, 0.0, shapes, (layer,)))
        own = {name: [[] for _ in self.maps]}
        self._static_pad(index, self.board.pads[index], own)
        self.trunks[index] = []
        for k, parts in enumerate(own[name]):
            cells = np.setdiff1d(np.unique(np.concatenate(parts)), self.own[name][k]) if parts else np.zeros(0, np.int64)
            self.hardf[k][cells] += 1
            self.own[name][k] = np.union1d(self.own[name][k], cells)
            self.trunks[index].append(cells)
        self.nets[name].pads.append(index)
        self.virtual.add(index)
        return index

    def _drop_trunk(self, net, index):
        net.pads.remove(index)
        self.board.pads[index] = replace(self.board.pads[index], net="", shapes=(), layers=())   # no copper left
        self.virtual.discard(index)
        for k, cells in enumerate(self.trunks.pop(index)):
            self.hardf[k][cells] -= 1
            self.own[net.name][k] = np.setdiff1d(self.own[net.name][k], cells)

    def _breakouts(self, pair, pads):
        """Whether both nets of a pair, alone on the board, join all their pads by their trunks (either first)."""
        nets = [self.nets[name] for name in pair]
        for order in (nets, nets[::-1]):
            for net in order:
                self._route(net, 0.0, strict=True)
                self._stamp(net)
            ok = not any(net.failed for net in nets) and self._used(pair, pads)
            for net in nets:
                self._ripup(net)
            if ok:
                return True
        return False

    def _trunk(self, pair, w, g, m):
        """Candidate (layer, (P line, N line), crossed) of a pair's coupled middle, shortest launch first;
        a string says why there is none."""
        pads, nets = self.board.pads, [self.nets[name] for name in pair]
        ps, ns = ([i for i in net.pads if pads[i].ref] for net in nets)
        close = [(i, j) for i in ps for j in ns if math.dist((pads[i].x, pads[i].y), (pads[j].x, pads[j].y)) <= 3.0]

        def mid(e):
            return (pads[e[0]].x + pads[e[1]].x) / 2, (pads[e[0]].y + pads[e[1]].y) / 2

        def side(e, d):   # P pad on the +normal side of the travel direction d
            return d[0] * (pads[e[0]].y - pads[e[1]].y) - d[1] * (pads[e[0]].x - pads[e[1]].x) > 0

        def crossing(ea, eb):
            d = (mid(eb)[0] - mid(ea)[0], mid(eb)[1] - mid(ea)[1])
            return side(ea, d) != side(eb, d), math.dist(mid(ea), mid(eb))

        def group(e0):   # the P/N pad pairs of one end, e.g. both rows of a USB-C connector
            return [e for e in close if math.dist(mid(e), mid(e0)) <= 1.5]
        if len(close) < 2:
            yield "the nets do not sit side by side at two places"
            return
        ea, eb = max(((x, y) for k, x in enumerate(close) for y in close[k + 1:]),
                     key=lambda e: math.dist(mid(e[0]), mid(e[1])))
        a, b = min(((x, y) for x in group(ea) for y in group(eb)), key=lambda e: crossing(*e))
        if math.dist(mid(a), mid(b)) < 2.0:
            yield "too short to couple"
            return
        span = max(math.dist((pads[i].x, pads[i].y), (pads[j].x, pads[j].y)) for net in nets
                   for i in net.pads for j in net.pads if pads[i].ref and pads[j].ref)
        if math.dist(mid(a), mid(b)) < 0.5 * span:   # e.g. a bus whose wires only meet at one transceiver
            yield "the nets run side by side over less than half their span"
            return
        common = set.intersection(*(set(pads[i].layers) for i in (*a, *b)))
        layers = [x for x in range(self.L) if not self.plane[x]]
        yield "no room for the coupled pair on one layer"
        # The pads' own layers first, at any launch radius; elsewhere the breakouts take vias.
        for reach, group in ((reach, group) for group in ([x for x in layers if x in common],
                                                          [x for x in layers if x not in common])
                             for reach in (0.75, 1.25, 2.0, 3.0, 4.5)):   # launch radius, nearest first
            found = sorted((len(path), layer, path) for layer in group
                           if (path := self._centreline(m, layer, mid(a), mid(b), reach)))
            for _, layer, path in found:
                path = self._straighten(None, path, m)
                verts = [path[0]] + [path[k] for k in range(1, len(path) - 1)
                                     if path[k + 1] - path[k] != path[k] - path[k - 1]] + [path[-1]]
                xy = [np.array(self._xy(cell)) for cell in verts]
                start, end = side(a, xy[1] - xy[0]), side(b, xy[-1] - xy[-2])
                lines = [_offset(xy, (w + g) / 2 * (1 if start == positive else -1)) for positive in (True, False)]
                if all(line and all(self._link_clear(net, layer, p, q, w, routed=False) for p, q in zip(line, line[1:]))
                       for net, line in zip(nets, lines)):
                    yield layer, lines, bool(start != end)

    def _centreline(self, m, layer, a, b, reach):
        """Cells of a one-layer search on map ``m`` between free cells within ``reach`` mm of a and of b."""
        free = self.hard[m][layer] == 0
        (ay, ax), (by, bx) = ((round((y - self.y0) / self.p), round((x - self.x0) / self.p)) for x, y in (a, b))
        r = reach / self.p
        for grow in (1, 4):
            pad = int(max(3.0 / self.p, 0.3 * max(abs(ay - by), abs(ax - bx))) * grow + r)
            y0, y1 = max(0, min(ay, by) - pad), min(self.H, max(ay, by) + pad + 1)
            x0, x1 = max(0, min(ax, bx) - pad), min(self.W, max(ax, bx) + pad + 1)
            Y, X = np.mgrid[y0:y1, x0:x1]
            win = free[y0:y1, x0:x1]
            begin, goal = win & (np.hypot(Y - ay, X - ax) <= r), win & (np.hypot(Y - by, X - bx) <= r)
            if not begin.any() or not goal.any():
                return None
            h, w = y1 - y0 + 2, x1 - x0 + 2
            cost = np.full((h, w), -1.0)
            # Open ground is cheaper: a trunk hugging pins would wall in other parts' escapes.
            cost[1:-1, 1:-1] = np.where(win, np.where(_erode(win, int(0.5 / self.p)), 1.0, 2.0), -1.0)
            flags = np.zeros((h, w), np.uint8)
            flags[1:-1, 1:-1] = goal
            gy, gx = np.nonzero(goal)
            sy, sx = np.nonzero(begin)
            path = _search(memoryview(cost.reshape(-1)), memoryview(np.full(h * w, -1.0)), w, h * w, 1,
                           ((sy + 1) * w + sx + 1).tolist(), bytearray(flags.tobytes()),
                           (int(gx.min()) + 1, int(gy.min()) + 1, int(gx.max()) + 1, int(gy.max()) + 1), self.weight)
            if path:
                return ([layer * self.HW + (cell // w - 1 + y0) * self.W + cell % w - 1 + x0 for cell in path]
                        if len(path) > 2 else None)
        return None

    def _joins(self, net, index):
        """Arc lengths along a trunk where the net's routes end on it."""
        pad = self.board.pads[index]
        line = self._trunk_line(index)
        return [found for path in net.paths for cell in (path[0], path[-1]) if cell // self.HW == pad.layers[0]
                if (found := _along(line, self._xy(cell), pad.shapes[0][5] / 2 + 1e-6)) is not None]

    def _used(self, pair, pads):
        """Whether each net's routes join its trunk near both trunk ends, not bypass it."""
        for name in pair:
            at, line = self._joins(self.nets[name], pads[name]), self._trunk_line(pads[name])
            if len(at) < 2 or max(at) - min(at) < 0.5 * sum(math.dist(a, b) for a, b in zip(line, line[1:])):
                return False
        return True

    def _uncouple(self):
        """A trunk that left its own pair unrouted, or that its routes bypass, gives way: both
        nets are routed singly instead. Returns whether any did."""
        undone = False
        for pair, info in list(self.coupled.items()):
            nets = [self.nets[name] for name in pair]
            if not info["coupled"]:
                continue
            if any(net.failed for net in nets):
                reason = "the trunk left the pair unrouted; routed as two nets"
            elif not self._used(pair, info["pads"]):
                reason = "the routes bypassed the trunk; routed as two nets"
            else:
                continue
            self.coupled[pair] = {"coupled": False, "reason": reason, "late": True}
            for net in nets:
                self._drop_trunk(net, info["pads"][net.name])
                self._ripup(net)
            for net in nets:
                self._route(net, 0.0, strict=True)
                self._stamp(net)
            undone = True
        return undone

    def _trunk_line(self, index):
        pad = self.board.pads[index]
        return [(pad.x + q[1], pad.y + q[2]) for q in pad.shapes] + [(pad.x + pad.shapes[-1][3], pad.y + pad.shapes[-1][4])]

    def _trim(self):
        """Cut each trunk back to its outermost joins: a trunk end past them dangles (KiCad warns)."""
        if self._uncouple():
            self.legalize()
        for pair, info in self.coupled.items():
            for name in pair if info["coupled"] else ():
                index = info["pads"][name]
                pad, line, at = self.board.pads[index], self._trunk_line(index), self._joins(self.nets[name], index)
                cut = _cut(line, min(at), max(at)) if len(at) > 1 else line
                if len(cut) > 1 and cut != line:
                    self._drop_trunk(self.nets[name], index)
                    info["pads"][name] = self._add_trunk(name, cut, pad.layers[0], pad.shapes[0][5])

    def _flight(self, net):
        """(delay ps, length mm) of a net as drawn, each segment at its own layer and width."""
        segments, vias = self.geometry(net)
        delay, length = len(vias) * self.via_delay_ps, 0.0
        for layer, a, b, width in segments:
            delay += math.dist(a, b) * self.models[layer].delay_ps_per_mm(width)
            length += math.dist(a, b)
        return delay, length

    def tune(self):
        """Delay matching per pair (idea from KiCadRoutingTools' trombone meanders, here on the grid).

        The faster net gets square bumps on its straight runs, only in cells free of all other
        copper, until it is within ``Options.skew_ps`` of its partner. A bump of height h adds
        2h grid steps, so what is left is at most one step's delay (about 0.6 ps at 0.1 mm).
        """
        self.tuning = {}
        for pair in self.pairs if self.opt.skew_ps else ():
            nets = [self.nets[name] for name in pair]
            if any(net.failed or not net.connections for net in nets):
                continue
            flights = [self._flight(net) for net in nets]
            fast = int(flights[1][0] < flights[0][0])
            self.tuning[pair] = {"skew_ps_before": round(abs(flights[0][0] - flights[1][0]), 2),
                                 "skew_mm_before": round(abs(flights[0][1] - flights[1][1]), 3),
                                 "meanders": self._meander(nets[fast], flights[1 - fast][0])}

    def _meander(self, net, target):
        """Bumps on ``net`` until its delay reaches ``target`` ps; returns how many were added."""
        bumps = 0
        for _ in range(8):   # exact bumps on the trunk, as many as fit
            added = self._trunk_bump(net, target - self._flight(net)[0])
            if not added:
                break
            bumps += added
        paths, necks, failed, connections = self._state(net)
        self._ripup(net)   # its own keep-off band leaves the maps while bumps are placed
        net.paths, net.necks = [list(path) for path in paths], necks
        bumps, tried = 0, set()
        while True:
            short = target - self._flight(net)[0]
            runs = []
            for k, path in enumerate(net.paths):
                i = 0
                while i < len(path) - 1:
                    step, j = path[i + 1] - path[i], i + 1
                    while j < len(path) - 1 and path[j + 1] - path[j] == step:
                        j += 1
                    if abs(step) < self.HW and not self.plane[path[i] // self.HW] and (k, path[i], step) not in tried:
                        runs.append((j - i, k, i, step))
                    i = j
            if short <= self.opt.skew_ps / 2 or not runs:
                break
            n, k, i, step = max(runs)
            tried.add((k, net.paths[k][i], step))
            bumps += self._bumps(net, k, i, n, step, short)
        self._restore(net, (net.paths, necks, failed, connections))
        return bumps

    def _trunk_bump(self, net, short):
        """Exact bumps on the outer side of the net's coupled trunk worth ``short`` ps; returns how many.

        Off the grid, so the match is exact; each new leg is checked against all other copper.
        """
        pair = next((pair for pair, info in self.coupled.items() if info["coupled"] and net.name in pair), None)
        if pair is None or short <= self.opt.skew_ps / 2:
            return 0
        info = self.coupled[pair]
        index, other = info["pads"][net.name], self.board.pads[info["pads"][pair[1 - pair.index(net.name)]]]
        pad = self.board.pads[index]
        layer, width = pad.layers[0], pad.shapes[0][5]
        line = [(pad.x + q[1], pad.y + q[2]) for q in pad.shapes] + [(pad.x + pad.shapes[-1][3], pad.y + pad.shapes[-1][4])]
        partner = np.array([(other.x + q[1], other.y + q[2]) for q in other.shapes])
        height = short / self.models[layer].delay_ps_per_mm(width) / 2
        arm, cache = width + net.cls.clearance, {}
        first = math.ceil(height / self.opt.meander_mm)
        segments = sorted(range(len(line) - 1), key=lambda k: -math.dist(line[k], line[k + 1]))[:6]
        # All of it in a few tall bumps first; then lower ones that buy only part of it (the caller repeats).
        for count, part, k in ((count, part, k) for part in (1, 0.5, 0.25) for count in (first, 2 * first)
                               for k in segments):
            a, b = np.array(line[k]), np.array(line[k + 1])
            span, need = math.dist(a, b), (2 * count - 1) * arm
            u = (b - a) / span
            normal = np.array((-u[1], u[0]))
            middle = (a + b) / 2
            if (np.hypot(*(partner - middle - normal).T).min() < np.hypot(*(partner - middle + normal).T).min()):
                normal = -normal   # away from the partner
            # Centred first, then slid along the leg in half-arm steps.
            starts = sorted(np.arange(arm, span - need - arm + 1e-9, arm / 2), key=lambda x: abs(x - (span - need) / 2))
            for start in starts[:12]:
                bump = []
                for j in range(count):
                    foot = a + u * (start + 2 * j * arm)
                    rise = normal * height * part / count
                    bump += [foot, foot + rise, foot + rise + u * arm, foot + u * arm]
                bump = [tuple(np.round(q, 6).tolist()) for q in bump]
                if all(self._link_clear(net, layer, q, r, width, cache=cache) for q, r in zip(bump, bump[1:])):
                    self._drop_trunk(net, index)
                    info["pads"][net.name] = self._add_trunk(net.name, line[:k + 1] + bump + line[k + 1:], layer, width)
                    return count
        return 0

    def _bumps(self, net, k, i, n, step, short):
        """Trombone bumps on the straight run i..i+n of path k worth up to ``short`` ps; returns how many."""
        path, W = net.paths[k], self.W
        dy = (step + W // 2) // W
        dx = step - dy * W
        size = self.p * math.hypot(dx, dy)
        width, t = (self.neck_width, net.neck) if k in net.necks else (net.cls.width, net.t)
        need = round(short / (2 * size * self.models[path[i] // self.HW].delay_ps_per_mm(width)))
        arm = math.ceil((width + net.cls.clearance) / size)   # spacing of the arms, in steps
        if need < 1 or n < 5 * arm:
            return 0
        hard, soft = self.hardf[t], self.softf[t]
        others = np.array([c for kk, other in enumerate(net.paths) for q, c in enumerate(other)
                           if kk != k or not i < q < i + n], np.int64)   # run ends stay: they may be vias
        reach = max(width, net.cls.via_diameter) / 2 + width / 2 + net.cls.clearance
        mine = np.unique((others[:, None] + self._disk(reach)[None, :]).ravel()) if others.size else others

        def free(cells):
            cells = np.clip(cells, 0, hard.size - 1)
            return (hard[cells] == 0) & (soft[cells] == 0) & ~np.isin(cells, mine)
        best = (0, 0, [])
        for normal in (dx * W - dy, dy - dx * W):
            placed, left, pos = [], need, 2 * arm
            while left and pos + 3 * arm <= n:
                base = path[i + pos]
                heights = np.arange(1, min(left, int(self.opt.meander_mm / size)) + 1)
                ok = free(base + heights * normal) & free(base + heights * normal + arm * step)
                h = int(np.argmin(ok)) if not ok.all() else len(heights)
                while h and not free(base + h * normal + np.arange(arm + 1) * step).all():
                    h -= 1
                if h:
                    placed.append((pos, h))
                    left, pos = left - h, pos + 2 * arm
                else:
                    pos += 1
            if need - left > best[0]:
                best = (need - left, normal, placed)
        _, normal, placed = best
        for pos, h in reversed(placed):
            base = path[i + pos]
            path[i + pos:i + pos + arm + 1] = ([base + q * normal for q in range(h + 1)]
                                               + [base + h * normal + q * step for q in range(1, arm + 1)]
                                               + [base + q * normal + arm * step for q in range(h - 1, -1, -1)])
        return len(placed)

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
        for layer, a, b, width in net.links:
            (narrow if width < net.cls.width else tracks).update(self._link_cells(layer, a, b).tolist())
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
            for layer, a, b, width in net.links:   # off-grid: one more pitch for the rounding
                parts.append((self._link_cells(layer, a, b)[:, None] + self._disk(width / 2 + gap + self.p)[None, :]).ravel())
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
        vextra = np.zeros((y1 - y0, x1 - x0))
        for name, extra in self.surcharge.items():   # dearer victims: the rescue's churn control
            other = self.nets[name]
            if other is not net and other.stamps is not None:
                layer, iy, ix = self._in_window(other.stamps[t], y0, y1, x0, x1)
                cost[layer, iy, ix] += extra
                np.add.at(vextra, self._in_window(other.stamps[net.v], y0, y1, x0, x1)[1:], extra)
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
        vcost = via_cost + pres * via_soft + vextra + (0 if strict else self.vhist[y0:y1, x0:x1])
        vcost[via_blocked] = -1
        h, w = y1 - y0 + 2, x1 - x0 + 2
        padded = np.full((self.L, h, w), -1.0)
        padded[:, 1:-1, 1:-1] = cost
        vpadded = np.full((h, w), -1.0)
        vpadded[1:-1, 1:-1] = vcost
        gpadded = np.zeros((self.L, h, w), np.uint8)
        gpadded[:, 1:-1, 1:-1] = goal
        # memoryviews: indexed as fast as lists by the search, without converting whole windows
        return memoryview(padded.reshape(-1)), memoryview(vpadded.reshape(-1)), gpadded, h, w

    def _in_window(self, cells, y0, y1, x0, x1):
        """Global flat cells -> (layer, row, column) inside the window, dropping the rest."""
        layer, rest = np.divmod(cells, self.HW)
        iy, ix = np.divmod(rest, self.W)
        keep = (iy >= y0) & (iy < y1) & (ix >= x0) & (ix < x1)
        return layer[keep], iy[keep] - y0, ix[keep] - x0

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
        self._stop_if_cancelled()
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
        with self._owned(net):
            if net.plane_layers:
                self._route_to_plane(net, pres, strict, keep or ())
            elif len(net.pads) > 1:
                self._route_tree(net, pres, strict, planes_ok, keep or (), thin)

    @contextmanager
    def _owned(self, net):
        """While routing ``net``: its own pads are free, its drilled pads keep vias off."""
        for m, cells in (*enumerate(self.own[net.name]), *self.tied.get(net.name, ())):
            self.hardf[m][cells] -= 1
        self.vown = np.zeros((self.H, self.W), bool)
        for index in self.virtual.intersection(net.pads):   # nor on its trunk: KiCad wants vias centred on track ends
            cells = self.pad_cells[index]
            self.vown.reshape(-1)[np.unique((cells[:, None] + self._disk(net.cls.via_diameter / 2 + self.p)).ravel())] = True
        for index in net.pads:   # no via too close to the net's own drilled pads
            pad = self.board.pads[index]
            if pad.drill:
                centre = int(round((pad.y - self.y0) / self.p)) * self.W + int(round((pad.x - self.x0) / self.p))
                reach = pad.drill / 2 + net.cls.via_drill / 2 + self.hole_to_hole + self.margin
                self.vown.reshape(-1)[centre + self._disk(reach)] = True
        try:
            yield
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
        # A pair's trunk joins first, so the breakouts hang off it rather than bypass it.
        pairs = sorted((0.0 if a in self.virtual or b in self.virtual else math.dist(where[a], where[b]), a, b)
                       for n, a in enumerate(pads) for b in pads[n + 1:])
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
        if self._uncouple():
            self.legalize()
        for _ in range(self.opt.optimize_passes):
            self.optimize()
            self._mend_planes()
            if not any(n.failed for n in self.order) or self._late():
                break
            self.legalize()   # the tidy-up freed room: try the dropped connections again
            self.rescue()
            self._mend_planes()
        self.relieve()
        self._trim()
        self.tune()
        self._say(f"done: {sum(n.connections - n.failed for n in self.order)}/"
                  f"{sum(n.connections for n in self.order)} connections routed")
        return self

    def _reaches_fill(self, pad, layer, x, y, gap, thin, fill, mine, radius=3.0):
        """Whether the fill at a spoke's end (x, y) is more than a pocket that only this pad touches.

        KiCad does not count spokes into a fill island with a single connection. Fixed copper
        only: a pocket that stays within ``radius`` mm and meets no other copper of the net is one.
        """
        iy0, iy1, ix0, ix1 = self._window_of(pad.x, pad.y, pad.x, pad.y, _pad_reach(pad) + radius)
        X, Y = self.xs[None, ix0:ix1], self.ys[iy0:iy1, None]
        rim = _pad_sdf(pad, X, Y)
        free = fill[iy0:iy1, ix0:ix1] & (rim > gap)
        other = mine[iy0:iy1, ix0:ix1] & (rim > gap + self.reach)
        seen = free & (np.hypot(X - x, Y - y) <= thin / 2 + 2 * self.p)
        while seen.any():
            if seen[0].any() or seen[-1].any() or seen[:, 0].any() or seen[:, -1].any() or (seen & other).any():
                return True
            grown = seen.copy()
            grown[1:] |= seen[:-1]
            grown[:-1] |= seen[1:]
            grown[:, 1:] |= seen[:, :-1]
            grown[:, :-1] |= seen[:, 1:]
            grown &= free
            if (grown == seen).all():
                break
            seen = grown
        return False

    def _occupancy(self):
        """Per map: flat array of 1 + the index (in ``self.order``) of the net whose centre line is there."""
        occ = [np.zeros(self.L * self.HW, np.int16) for _ in self.maps]
        for i, net in enumerate(self.order, 1):
            for index, path in enumerate(net.paths):
                occ[net.neck if index in net.necks else net.t][path] = i
                vias = [a % self.HW for a, b in zip(path, path[1:]) if a % self.HW == b % self.HW]
                for layer in range(self.L) if vias else ():
                    occ[net.v][layer * self.HW + np.array(vias, np.int64)] = i
        return occ

    def _own_spokes(self, pad, layer, gap, segments, spokes):
        """Own tracks KiCad counts as spokes: one end in the pad, the other in the fill.

        In the fill: on a spoke that reaches the pour (``spokes``: their axes), or in the pour
        clear of every pad of the net and its thermal gap (a via in the gap does not count).
        """
        count = 0
        for a, b in segments:
            inside = [float(_pad_sdf(pad, np.array(e[0]), np.array(e[1]))) <= 0 for e in (a, b)]
            if inside[0] == inside[1]:
                continue
            x, y = b if inside[0] else a
            if any(_segment_sdf(*axis[:4], x, y) <= axis[4] for axis in spokes):
                count += 1
                continue
            iy, ix = int(round((y - self.y0) / self.p)), int(round((x - self.x0) / self.p))
            if not (0 <= iy < self.H and 0 <= ix < self.W and self.pour[(pad.net, layer)][iy, ix]):
                continue
            near = (self.board.pads[i] for i in self.nets[pad.net].pads)
            count += all(abs(q.x - x) > 3 or abs(q.y - y) > 3 or layer not in q.layers
                         or float(_pad_sdf(q, np.array(x), np.array(y))) > gap for q in near)
        return count

    def _jogs(self, nets):
        """Short jogs off the middle of another track: KiCad reports such a stub as dangling."""
        count = 0
        for net in nets:
            segments, vias = self.geometry(net)
            ends = [e for _, a, b, _ in segments for e in (a, b)]
            for layer, a, b, width in segments:
                if math.dist(a, b) > width + 1e-6:
                    continue
                for e in (a, b):
                    if ends.count(e) == 1 and e not in vias and any(
                            at == layer and e not in (p, q) and _segment_sdf(*p, *q, *e) < 1e-6
                            for at, p, q, _ in segments):
                        count += 1
        return count

    def relieve(self):
        """Re-route the tracks that leave a pad a single thermal spoke (KiCad wants two), around two corridors.

        Only where routing did the damage, and only if every net it touches still routes in
        full and clear (otherwise all is put back): a keep-out reserved up front walled in
        neighbours' escapes.
        """
        if not self.corridors:
            return
        fixed = given_up = 0
        occ = None
        for k, (pad, layer, gap, group) in enumerate(self.corridors):
            if self._late():
                break
            if occ is None:   # who crosses which corridor, and the spokes left whole
                occ, drawn, index = self._occupancy(), {}, {net.name: i for i, net in enumerate(self.order, 1)}
                cuts, whole = [], {}
                for other, at, _, corridors in self.corridors:
                    cuts.append([{int(i) for m, band in enumerate(bands) for i in np.unique(occ[m][band])}
                                 - {0, index.get(other.net)} for bands, _ in corridors])
                    whole.setdefault((other.net, at), []).extend(
                        axis for (_, axis), c in zip(corridors, cuts[-1]) if not c)
            name, cut = pad.net, cuts[k]
            # KiCad: a pad no spoke reaches is left to the connectivity check; one is starved below two.
            uncut, need = sum(not c for c in cut), 2
            if len(group) < need or not uncut or uncut >= need:
                continue
            if (name, layer) not in drawn:
                drawn[(name, layer)] = [(a, b) for at, a, b, _ in self.geometry(self.nets[name])[0] if at == layer]
            if uncut + self._own_spokes(pad, layer, gap, drawn[(name, layer)], whole[(name, layer)]) >= need:
                continue
            chosen = sorted(range(len(group)), key=lambda i: len(cut[i]))[:need]
            nets = [self.order[i - 1] for i in sorted(set().union(*(cut[i] for i in chosen)))]
            reserved = [(m, band) for i in chosen for m, band in enumerate(group[i][0])]
            saved, before, jogs = [self._state(net) for net in nets], sum(n.failed for n in self.order), self._jogs(nets)
            for m, band in reserved:
                self.hardf[m][band] += 1
            for net in nets:   # whole nets, as the tidy-up does: a partly kept net can leave a stub
                self._ripup(net)
                self._route(net, 0.0, strict=True)
                self._stamp(net)
            if (sum(n.failed for n in self.order) > before or any(self._in_conflict(n) for n in nets)
                    or self._jogs(nets) > jogs):
                for net, state in zip(nets, saved):
                    self._restore(net, state)
                for m, band in reserved:
                    self.hardf[m][band] -= 1
                given_up += 1
            else:
                fixed += 1
                occ = None
        if fixed or given_up:
            self._say(f"thermal relief: {fixed} pads cleared, {given_up} left as routed")

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

    def _dropped(self):
        return sum(n.failed for n in self.order)

    def _turn(self, net, push, price, keep_equal=True, check=None):
        """One rip-up turn: ``push(price)`` routes ``net`` through other copper at ``price`` per
        step and returns connections gained beyond ``failed``; the nets it crossed re-route
        their crossed paths strictly, least ripped first. A turn that leaves the board worse
        (or, without ``keep_equal``, no better, or failing ``check()``) is undone: returns None.
        Otherwise the victims that lost a connection."""
        before, touched = self._dropped(), {net.name: self._state(net)}
        # A net already open loses nothing more when crossed again: no surcharge for it.
        self.surcharge = {name: price * count for name, count in self.rips.items() if not self.nets[name].failed}
        try:
            gained = push(price)
        finally:
            self.surcharge = {}
        tracks, vias = self._conflicts(net)
        through = np.concatenate([layer * self.HW + vias for layer in range(self.L)])
        blockers = [] if not tracks.size and not vias.size else [
            other for other in self.order if other is not net and other.stamps is not None
            and (np.isin(tracks, other.stamps[net.t]).any()
                 or (net.neck is not None and np.isin(tracks, other.stamps[net.neck]).any())
                 or np.isin(through, other.stamps[net.v]).any())]
        ripped = []
        for other in sorted(blockers, key=lambda o: (self.rips.get(o.name, 0), len(o.pads))):
            touched[other.name], failed = self._state(other), other.failed
            self._reroute(other, 0.0, strict=True)   # they give way: only their crossed paths move
            if other.failed > failed:
                self.rips[other.name] = self.rips.get(other.name, 0) + 1
                ripped.append(other)
        if (self._dropped() - gained > before - (0 if keep_equal else 1) or self._in_conflict(net)
                or (check and not check())):
            for name, saved in touched.items():
                self._restore(self.nets[name], saved)
            return None
        return ripped

    def rescue(self):
        """Finish what the hard pass dropped, keeping the board free of conflicts throughout.

        Rip-up and re-route with a queue: a dropped net is routed straight through whatever
        is in its way, the nets it crossed are ripped and re-routed around it, and any of
        those that no longer fit take their turn in the queue. The best state seen is the
        one that stays. Last resort for what is still open: a short run on a reference
        plane layer, reported as such.

        Churn control (ideas from KiCadRoutingTools): a net that lost a connection as a
        victim costs more to push through each time (a per-net surcharge in the search), so
        the rescued net picks victims that have not been torn up yet; victims re-route least
        ripped first; a net ripped ``defer_after`` times waits for a final round instead of
        trading places; a turn that leaves more connections open than it found is undone.
        """
        opt, rips, dropped = self.opt, self.rips, self._dropped
        best, kept = dropped(), {n.name: self._state(n) for n in self.order}
        queue = deque(sorted((n for n in self.order if n.failed), key=lambda n: n.failed))
        deferred, undone = [], set()
        turns = opt.rescue_turns * len(queue)
        rng = np.random.default_rng(opt.seed)
        while (queue or deferred) and turns > 0 and not self._late():
            if not queue:   # the final round: the deferred nets, once the others have settled
                queue.extend(deferred)
                deferred = []
            turns -= 1
            net = queue.popleft()
            if not net.failed:
                continue
            # Jitter keeps two nets from trading places forever. A signal that was dropped at
            # its class width comes back at the escape width.
            thin = net.neck is not None and not net.plane_layers and net.cls.width <= 2 * self.neck_width + 1e-9
            ripped = self._turn(net, lambda price: self._reroute(net, price, thin=thin) or 0,
                                opt.rip_cost * (0.5 + rng.random()))
            if ripped is None:
                if net.name not in undone:   # once more in the final round, against the new prices
                    undone.add(net.name)
                    deferred.append(net)
                continue
            for other in ripped:
                (deferred if rips[other.name] >= opt.defer_after else queue).append(other)
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

    # ---- KiCad's verdict ---------------------------------------------------
    def reconnect(self, missing):
        """Route the connections KiCad's DRC reports open on the zone-refilled candidate.

        ``missing``: pairs of KiCad items, each (description, x, y) as in ``DrcResult.missing``.
        Each item maps back to the net's copper: a pad by its position, a track or via by
        the nearest copper drawn, a zone by the rest of the net. First a straight off-grid
        link between two pads of the net within reach (fine-pitch neighbours the grid phase
        walls off), then a strict search between the two copper islands, then one rip-up
        turn of the rescue that is kept only if it closes more than it opens. Returns the
        number of connections added; KiCad judges them on its next check.
        """
        added, rng = 0, np.random.default_rng(self.opt.seed)
        for pair in missing:
            items = [item for item in (_kicad_item(*raw) for raw in pair) if item]
            if len(items) != 2 or items[0][1] != items[1][1] or items[0][1] not in self.nets or self._late():
                continue
            net = self.nets[items[0][1]]
            if self._link(net, items, rng) or self._bridge(net, items, rng):
                added += 1
        return added

    def _pad_at(self, net, item):
        kind, _, _, x, y = item
        return next((i for i in net.pads if kind == "pad" and abs(self.board.pads[i].x - x) < 0.01
                     and abs(self.board.pads[i].y - y) < 0.01), None)

    def _islands(self, net):
        """Copper islands of a net as drawn; planes do not join (KiCad said they did not).

        Returns (node -> island, island -> cells), nodes being ("pad", index) and ("path", k).
        """
        cells = {}
        for index in net.pads:
            cells[("pad", index)] = self._pad_global(index)
        for k, path in enumerate(net.paths):   # where a new path may land: own vias on their centres only
            cells[("path", k)] = self._joinable(net, path)
        root = {node: node for node in cells}

        def find(node):
            while root[node] != node:
                node = root[node]
            return node
        owner = {}
        for node, here in cells.items():
            for cell in here.tolist():
                root[find(owner.setdefault(cell, node))] = find(node)
        for _, a, b, _ in net.links:
            ends = [("pad", i) for i in net.pads if (self.board.pads[i].x, self.board.pads[i].y) in (a, b)]
            for node in ends[1:]:
                root[find(node)] = find(ends[0])
        island, joined = {node: find(node) for node in cells}, {}
        for node, here in cells.items():
            joined.setdefault(island[node], []).append(here)
        return island, {key: np.unique(np.concatenate(parts)) for key, parts in joined.items()}

    def _node_at(self, net, item):
        kind, _, layer, x, y = item
        if kind == "pad":
            index = self._pad_at(net, item)
            return None if index is None else ("pad", index)
        if kind not in ("track", "via"):
            return None
        best = None
        for k, path in enumerate(net.paths):
            cells = np.array(path, np.int64)
            if kind == "track" and layer in self.board.layers:
                cells = cells[cells // self.HW == self.board.layers.index(layer)]
            if cells.size:
                rest = cells % self.HW
                d = float(np.hypot(self.x0 + rest % self.W * self.p - x, self.y0 + rest // self.W * self.p - y).min())
                best = min(best or (d, k), (d, k))
        return ("path", best[1]) if best and best[0] < 0.5 else None

    def _add_path(self, net, path, narrow):
        """Add a connection to a routed net; returns 1 if it was not counted as missing."""
        paths, necks, failed, connections = self._state(net)
        self._restore(net, (paths + [path], necks | ({len(paths)} if narrow else set()), max(0, failed - 1),
                            connections))
        return 0 if failed else 1

    def _link(self, net, items, rng):
        """A straight track between two pads of the net, checked exactly against all other copper.

        Where only routed copper of other nets is in the way, one rip-up turn moves it.
        """
        a, b = (self._pad_at(net, item) for item in items)
        if a is None:
            a, b = b, a
        if a is None:
            return False
        pads, island = self.board.pads, self._islands(net)[0]
        pa = pads[a]
        near = [b] if b is not None else sorted(
            (i for i in net.pads if island[("pad", i)] != island[("pad", a)] and i not in self.virtual),
            key=lambda i: math.dist((pa.x, pa.y), (pads[i].x, pads[i].y)))
        options = [(pads[c], layer, width) for c in near if math.dist((pa.x, pa.y), (pads[c].x, pads[c].y)) <= 2.0
                   for layer in pa.layers if layer in pads[c].layers and not self.plane[layer]
                   for width in sorted({net.cls.width, self.neck_width}, reverse=True)]

        def add(link):
            net.links.append(link)
            paths, necks, failed, connections = self._state(net)
            self._restore(net, (paths, necks, max(0, failed - 1), connections))
            return 0 if failed else 1
        for routed in (True, False):
            for pc, layer, width in options:
                link = (layer, (pa.x, pa.y), (pc.x, pc.y), width)
                if not self._link_clear(net, *link, routed=routed):
                    continue
                if routed:
                    add(link)
                elif self._turn(net, lambda price: add(link), self.opt.rip_cost * (0.5 + rng.random()),
                                keep_equal=False, check=lambda: self._link_clear(net, *link)) is None:
                    net.links.remove(link)
                    self._restore(net, self._state(net))
                    continue
                self._say(f"KiCad reconnect: {net.name} {pa.ref}.{pa.number}-{pc.ref}.{pc.number} "
                          f"linked at {width:g} mm" + ("" if routed else " after a rip-up"))
                return True
        return False

    def _link_cells(self, layer, a, b):
        n = int(math.dist(a, b) / (self.p / 2)) + 2
        ix = np.rint((np.linspace(a[0], b[0], n) - self.x0) / self.p).astype(np.int64)
        iy = np.rint((np.linspace(a[1], b[1], n) - self.y0) / self.p).astype(np.int64)
        return np.unique(layer * self.HW + iy * self.W + ix)

    def _link_clear(self, net, layer, a, b, width, routed=True, cache=None):
        """Exact clearance (5 um samples, 1 um spare) of a straight track to all copper not its own;
        without ``routed``, to the fixed copper only (pads, existing tracks, edges, keep-outs)."""
        n = int(math.dist(a, b) / 0.005) + 2
        X, Y = np.linspace(a[0], b[0], n), np.linspace(a[1], b[1], n)
        half, gap, board = width / 2 + 0.001, net.cls.clearance, self.board

        def near(x, y, reach):
            return float(_segment_sdf(*a, *b, np.array(x), np.array(y))) < reach + 3.0
        for pad in board.pads:
            if pad.net == net.name or not near(pad.x, pad.y, 0):
                continue
            if layer in pad.layers:
                rule = max(gap, pad.clearance or (board.netclass(pad.net).clearance if pad.net else gap))
                if _pad_sdf(pad, X, Y).min() < half + rule:
                    return False
            if pad.drill and np.hypot(X - pad.x, Y - pad.y).min() < half + pad.drill / 2 + board.rules.get(
                    "min_hole_clearance", 0):
                return False
        if cache is None or "copper" not in cache:   # ``cache``: dict kept by a caller that checks many legs
            routed_copper = [(other, other.name, *segment) for other in self.nets.values() if routed
                             for segment in self.geometry(other)[0]]
            vias = [(other, x, y) for other in self.nets.values() if routed for x, y in self.geometry(other)[1]]
            if cache is not None:
                cache.update(copper=routed_copper, vias=vias)
        else:
            routed_copper, vias = cache["copper"], cache["vias"]
        copper = [segment[1:] for segment in routed_copper if segment[0] is not net]
        copper += [(name, lay, (x1, y1), (x2, y2), w) for name, lay, x1, y1, x2, y2, w in board.tracks]
        for name, lay, (x1, y1), (x2, y2), w in copper:
            if name != net.name and lay == layer and near(x1, y1, math.dist((x1, y1), (x2, y2))):
                rule = max(gap, board.netclass(name).clearance)
                if _segment_sdf(x1, y1, x2, y2, X, Y).min() < half + w / 2 + rule:
                    return False
        for other, x, y in vias:
            if other is not net:
                rule = max(gap, other.cls.clearance)
                if near(x, y, 0) and np.hypot(X - x, Y - y).min() < half + other.cls.via_diameter / 2 + rule:
                    return False
        for x1, y1, x2, y2 in board.edges:
            if _segment_sdf(x1, y1, x2, y2, X, Y).min() < half + self.edge_clearance:
                return False
        for zone in board.zones:   # rule areas that keep tracks out
            if not zone.net and zone.no_tracks and layer in zone.layers and _poly_sdf(zone.points, X, Y).min() < half:
                return False
        for box in self.obstacles:
            if box.layer == board.layers[layer] and (np.maximum(np.maximum(box.xmin_mm - X, X - box.xmax_mm),
                                                                np.maximum(box.ymin_mm - Y, Y - box.ymax_mm))
                                                     < half + gap).any():
                return False
        return True

    def _bridge(self, net, items, rng):
        """Route between the two islands KiCad names: strict first, then one rip-up turn."""
        island, cells_of = self._islands(net)
        ids = [island.get(self._node_at(net, item)) for item in items]
        if ids[0] is None:
            ids.reverse()
            items = items[::-1]
        targets = [ids[1]] if ids[1] is not None else [key for key in cells_of if key != ids[0]]
        if ids[0] is None or ids[1] == ids[0] or not targets:
            return False   # unknown copper, or one island already: nothing the grid can add
        starts, target = cells_of[ids[0]], np.concatenate([cells_of[key] for key in targets])
        x, y = items[0][3:]
        here = int(round((y - self.y0) / self.p)) * self.W + int(round((x - self.x0) / self.p))
        rest = target % self.HW
        focus = np.array([here, rest[np.argmin(np.hypot(rest % self.W - here % self.W,
                                                        rest // self.W - here // self.W))]], np.int64)

        def attempt(price, strict):
            path = None
            with self._owned(net):
                for drawn in net.paths:
                    self._joinable(net, drawn)   # the net's own via keep-outs
                    # and no new via may swallow a corner (a track end) of it off-centre
                    corners = np.array([b for a, b, c in zip(drawn, drawn[1:], drawn[2:]) if b - a != c - b],
                                       np.int64) % self.HW
                    ring = (corners[:, None] + self._disk(net.cls.via_diameter / 2 + self.p)[None, :]).ravel()
                    self.vown.reshape(-1)[ring[~np.isin(ring, corners)]] = True
                for t in (net.t,) + (() if net.neck is None else (net.neck,)):
                    path = self._connect(net, starts, target, price, strict, focus=focus, t=t)
                    if path:
                        break
            return self._add_path(net, self._straighten(net, path, t), t != net.t) if path else 0
        before = self._dropped()
        if attempt(0.0, True) or self._dropped() < before:
            return True
        return self._turn(net, lambda price: attempt(price, False), self.opt.rip_cost * (0.5 + rng.random()),
                          keep_equal=False) is not None

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
        for layer, a, b, width in net.links:
            segments.add((layer, min(a, b), max(a, b), width))
        for index in self.virtual.intersection(net.pads):   # a pair's coupled trunk
            pad = self.board.pads[index]
            for _, x1, y1, x2, y2, width in pad.shapes:
                a, b = (round(pad.x + x1, 6), round(pad.y + y1, 6)), (round(pad.x + x2, 6), round(pad.y + y2, 6))
                segments.add((pad.layers[0], min(a, b), max(a, b), width))
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
            delay = self._flight(net)[0]
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
        pairs = []
        for pair in self.pairs:
            if pair[0] not in nets or pair[1] not in nets:
                continue
            flights = [self._flight(self.nets[name]) for name in pair]
            entry = {"nets": list(pair), "lengths_mm": [round(f[1], 3) for f in flights],
                     "skew_ps": round(abs(flights[0][0] - flights[1][0]), 2),
                     "skew_mm": round(abs(flights[0][1] - flights[1][1]), 3)}
            info = dict(self.coupled.get(pair, {"coupled": False, "reason": "coupling off"}))
            trunk = info.pop("pads", None)
            info.pop("late", None)
            if trunk:
                coupled = sum(math.dist(s[1:3], s[3:5]) for name in pair for s in self.board.pads[trunk[name]].shapes)
                info.update(coupled_mm=round(coupled / 2, 2),
                            coupled_pct=round(100 * coupled / sum(f[1] for f in flights), 1))
            pairs.append({**entry, **info, **self.tuning.get(pair, {})})
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
                "rounds": self.rounds, "seconds": round(time.monotonic() - self.started - self.kicad_s, 1),
                "kicad_check_s": round(self.kicad_s, 1),
                "connections": total, "unrouted": failed,
                "completion_pct": round(100 * (total - failed) / total, 1) if total else 100.0,
                "vias": sum(n["vias"] for n in nets.values()),
                "length_mm": round(sum(sum(n["length_mm"].values()) for n in nets.values()), 1),
                "nets": nets}


def _kicad_item(description, x, y):
    """(kind, net, layer name or None, x, y) of a KiCad DRC item such as 'Pad 4 [GND] of U14 on B.Cu'."""
    found = re.match(r"(\w+)[^\[]*\[(.*?)\](?= of | on |,|$)(.*)", description)
    if not found:
        return None
    layer = re.search(r" on ([^\s,]+)", found.group(3))
    return found.group(1).lower(), found.group(2), layer and layer.group(1), x, y


def kicad_check(router, cli, board_name, context=(), rounds=2):
    """KiCad-oracle loop (idea from KiCadRoutingTools): KiCad DRC with zones refilled on the
    routed copy, then ``Router.reconnect`` of exactly the connections it reports open, at
    most ``rounds`` times. Copper that KiCad counts no better (open plus violations) than
    the round before is rolled back. ``context``: project files (.kicad_pro, .kicad_dru)
    copied next to the candidate. Returns (DrcResult, board text) of the copper kept.
    """
    import shutil
    import tempfile
    from pathlib import Path

    from .candidate import candidate_text
    best = None
    with tempfile.TemporaryDirectory(prefix="vela-kicad-check-", ignore_cleanup_errors=True) as folder:
        path = Path(folder) / board_name
        for source in map(Path, context):
            if source.is_file():
                shutil.copy(source, path.with_suffix(source.suffix))
        for check in range(rounds + 1):
            text = candidate_text(router.board.text, router.copper())
            path.write_text(text, encoding="utf-8")
            started = time.monotonic()
            result = cli.drc(path)
            router.kicad_s += time.monotonic() - started
            score = (result.unconnected + result.violations, result.unconnected)
            if best and score >= best[0]:
                router._say(f"KiCad check: {result.unconnected} open, {result.violations} violations, "
                            "no better: rolled back")
                for net in router.nets.values():
                    saved, links = best[3][net.name]
                    if router._state(net)[0] is not saved[0] or net.links != links:
                        net.links = list(links)
                        router._restore(net, saved)
                break
            best = (score, result, text, {n.name: (router._state(n), list(n.links)) for n in router.nets.values()})
            router._say(f"KiCad check: {result.unconnected} connections open")
            if not result.unconnected or check == rounds or not router.reconnect(result.missing):
                break
    return best[1], best[2]


def route_board(board: Board, options: Options | None = None, obstacles=()) -> Router:
    """Best of up to ``options.tries`` runs that differ only in re-route order.

    ponytail: restarts on top of the negotiation; the order still matters on dense boards.
    A finer grid was tried instead (tracks pack 14 % tighter): 7x slower, no gain.
    A try is only started if the last one would fit in what is left: a cut-short try
    has not won on the bench, and the time is better spent on ``kicad_check``.
    """
    options = options or Options()
    started, best, last = time.monotonic(), None, 0.0
    tries = max(1, options.tries)
    attempt, skip = 0, tuple(options.skip_pairs)
    while attempt < tries:
        left = options.budget_s - (time.monotonic() - started)
        if best and left < max(10, 1.2 * last + 30):   # 30 s: room for the KiCad check
            break
        last = time.monotonic()
        seed = None if options.seed is None else options.seed + attempt
        router = Router(board, replace(options, seed=seed, budget_s=max(left, 10), skip_pairs=skip), obstacles).run()
        last = time.monotonic() - last
        late = tuple(pair for pair, info in router.coupled.items() if info.get("late"))
        if late and options.budget_s - (time.monotonic() - started) > 1.2 * last + 30:
            skip += late   # the board was routed around a trunk that then gave way: route it again without
            continue
        attempt += 1
        score = (sum(n.failed for n in router.order), sum(router._score(n)[1] for n in router.order))
        if best is None or score < best[0]:
            best = (score, router)
        if not score[0] or options.seed is None:
            break
    best[1].started, best[1].opt = started, replace(best[1].opt, budget_s=options.budget_s)
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
    parser.add_argument("--seed", type=int, default=Options.seed, help="first re-route order; later tries use the next seeds")
    parser.add_argument("--kicad-rounds", type=int, default=2,
                        help="with --kicad-cli: rounds of reconnecting what KiCad DRC reports open; 0 = judge only")
    args = parser.parse_args(argv)
    board = load_board(args.board)
    cli = KiCadCli(args.kicad_cli) if args.kicad_cli else None
    obstacles, notes = (), []
    if cli:
        try:   # copper text is only known through KiCad's own rendering
            obstacles = extract_copper_text(args.board, cli).obstacles
        except (CapabilityError, ValidationError) as exc:
            notes.append(f"copper text not modelled: {exc}")
    router = route_board(board, Options(pitch=args.pitch, budget_s=args.budget, seed=args.seed, log=lambda text: print(text, flush=True)), obstacles)
    routed = text = None
    if cli:
        routed, text = kicad_check(router, cli, args.board.name, [args.board.with_suffix(suffix) for suffix in
                                                                 (".kicad_pro", ".kicad_dru")], args.kicad_rounds)
    report = router.report()
    report["notes"] = notes
    folders = {name: args.out / name for name in ("routed", "baseline")}
    for name, folder in folders.items():
        folder.mkdir(parents=True, exist_ok=False)
        for suffix in (".kicad_pro", ".kicad_dru"):
            if args.board.with_suffix(suffix).is_file():
                shutil.copy(args.board.with_suffix(suffix), folder / (args.board.stem + suffix))
        (folder / args.board.name).write_text(board.text if name == "baseline" else text or candidate_text(
            board.text, router.copper()), encoding="utf-8")
    if cli:
        baseline = cli.drc(folders["baseline"] / args.board.name)
        blocking, _ = route_issues(baseline, routed)
        report["drc"] = {"unconnected_before": baseline.unconnected, "unconnected": routed.unconnected,
                         "blocking": blocking, "reasons": list(blocking_reasons(baseline, routed)),
                         "accepted": not routed.unconnected and not blocking}
    (args.out / "report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k not in ("nets", "electrical")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
