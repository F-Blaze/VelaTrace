"""DRC-guided removal of Freerouting's dead-end stubs, before the preview is shown.

Freerouting 2.1.0 often leaves a short track (or via) that ends on nothing. KiCad
DRC names each one (track_dangling / via_dangling with a single item). They are
removed on disposable candidates only, and a removal is kept only when KiCad DRC of
the trial shows no new issue and no lost connection. The caller validates the
returned plan as usual; this module never writes a board and uses only the
validator's public inspect() hook.
"""
from collections import Counter

from .ses import RoutePlan, Track

DANGLING = {"track_dangling", "via_dangling"}


def _sources(plan: RoutePlan, items) -> dict:
    """Item id -> ("edge", track index, edge index) or ("via", via index), in the
    order prepare_copper emits copper for a plan."""
    keys = [("edge", t, e) for t, track in enumerate(plan.tracks) for e in range(len(track.points_mm) - 1)]
    keys += [("via", v) for v in range(len(plan.vias))]
    kinds = ["segment" if key[0] == "edge" else "via" for key in keys]
    if len(items) != len(keys) or [item.kind for item in items] != kinds or len({item.id for item in items}) != len(items):
        return {}
    return {item.id: key for item, key in zip(items, keys)}


def _without(plan: RoutePlan, dropped: set) -> RoutePlan:
    tracks = []
    for t, track in enumerate(plan.tracks):
        run = [track.points_mm[0]]
        for e, end in enumerate(track.points_mm[1:]):
            if ("edge", t, e) in dropped:
                if len(run) > 1:
                    tracks.append(Track(track.net, track.layer, track.width_mm, tuple(run)))
                run = [end]
            else:
                run.append(end)
        if len(run) > 1:
            tracks.append(Track(track.net, track.layer, track.width_mm, tuple(run)))
    return RoutePlan(plan.base_design, tuple(tracks),
                     tuple(via for v, via in enumerate(plan.vias) if ("via", v) not in dropped))


def _dangling(result, ids) -> set:
    return {uuids[0] for kind, _, uuids in result.issues if kind in DANGLING and len(uuids) == 1 and uuids[0] in ids}


def repair_dangling(plan: RoutePlan, dsn, constraints, validator, *, max_passes: int = 4) -> RoutePlan:
    """The plan without DRC-identified dead ends, or the plan itself when nothing can
    be removed safely."""
    first = validator.inspect(dsn, plan, constraints)
    items, sources = first.items, _sources(plan, first.items)
    if not sources or any(len(candidate.issues) != candidate.violations + candidate.schematic_parity
                          for _, candidate in first.passes):
        return plan  # Unknown item or issue identities: nothing is provably a dead end.
    best = (plan, first)
    removed = set()
    for _ in range(max_passes):
        current = [candidate for _, candidate in best[1].passes]
        ids = set(sources) - removed
        targets = set.intersection(*(_dangling(result, ids) for result in current))
        if not targets:
            break
        trial_plan = _without(plan, {sources[i] for i in removed | targets})
        trial_items = tuple(item for item in items if item.id not in removed | targets)
        if not trial_items:
            break
        trial = validator.inspect(dsn, trial_plan, constraints, trial_items)
        def worse(before, after):
            # A removal may only expose the next dead end of the same stub.
            new = Counter(after.issues) - Counter(before.issues)
            return (after.unconnected > before.unconnected or len(after.issues) > len(before.issues)
                    or any(kind not in DANGLING or len(uuids) != 1 or uuids[0] not in ids for kind, _, uuids in new))
        if any(worse(before, after) for before, (_, after) in zip(current, trial.passes)):
            break
        removed |= targets
        best = (trial_plan, trial)
    validator.reuse(best[1])  # validate() of the returned plan needs no further DRC.
    return best[0]
