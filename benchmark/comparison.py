'''
Tags: Main

Cross-config comparison for a finished run (Phase 5.5a).

Two offline reads over a run directory, both pure in the sense metrics.py
is pure — they read logs and the manifest and compute; they execute no
circuits and touch no device:

  assemble_matrix(run_dir)  -> the comparison bundle, written comparison.json
  sweep(run_dir, session, axis, grid, bisect) -> the weight-sweep result,
                                                 written sweep_comp.<axis>.json

The matrix bundle is the inter-component surface: one row per session, its
config and its metrics, so the 5.5b modes can diff config A against config
B without re-reading logs. The sweep is the intra-component surface: it
re-derives one session's routing or allocation decisions across an α/β
grid FROM THE RECORDED SCORES, so "how would this config route at other
weights" is answered from one recorded run rather than by re-executing.

Both write an artifact rather than only returning data: a sweep that a
user ran and then disconnected from would otherwise be lost, and — like
metrics.json — a computed view worth reading is worth persisting. A future
qbench (5.7) is a shell OVER these functions; it renders what they return
and never recomputes a second way, so the file and any view cannot drift.

WHY THE SWEEP NEEDS NOTHING BUT THE LOG. A scoring component records, per
decision, the weight-free inputs to its score (the α/β-free cost
decomposition, the raw queue pressure). Re-weighting those recorded inputs
and re-ranking reproduces the decision at any weights — this is the
Sweepable contract (kernel/sweep.py). The driver borrows the session's
component purely as a scoring engine: it reconstructs the registered class
by name and calls its sweep hooks on the logged terms, computing no score
itself, so a third-party scoring component sweeps identically once
registered.

FAITHFULNESS IS GUARDED. Before trusting any swept output, the driver
replays each decision at the run's OWN recorded weights and checks it
reproduces the winner the log recorded. A component whose decision is not
a pure function of its logged terms (a stochastic or stateful policy)
fails this and the session is refused with a reason rather than emitting
fiction — the same decision-determinism the rest of the benchmark layer
requires.
'''

import json
import os

from devq import DevQ


# The two sweepable axes and how to read a decision of each out of the
# log: the event kind, the key naming the chosen candidate, and the key
# under each score naming that candidate. Routers choose devices; the
# allocator chooses blocks. Adding a third scored axis (a scored scheduler,
# Phase 5.6) is a row here, not new driver logic.
_AXES = {
    "router": {
        "event"      : "route",
        "winner_key" : "device",
        "cand_key"   : "device",
        "kind"       : "router",
    },
    "allocator": {
        "event"      : "allocate",
        "winner_key" : "block",
        "cand_key"   : "block",
        "kind"       : "allocator",
    },
}


# ── Matrix bundle ─────────────────────────────────────────────────────────────

def assemble_matrix(run_dir):
    '''
    Collect every session's config and metrics into one bundle keyed by
    session id, and write comparison.json beside the manifest. Returns the
    mapping it wrote.

    Pure assembly: it reads each session's metrics.json entry (computed by
    metrics.write_metrics) and the manifest's per-session config; it does
    not recompute metrics. Run write_metrics first — if metrics.json is
    absent, a session simply carries `metrics: None`, so the bundle still
    assembles rather than failing, and the gap is visible.

    Each row also records which axes are sweepable in that session — a
    scoring router/allocator leaves a scores-bearing event, a non-scoring
    one does not — so a reader knows where an intra-component sweep is
    available without opening the log.
    '''
    manifest = _load_json(os.path.join(run_dir, "manifest.json"))
    metrics  = _load_json(os.path.join(run_dir, "metrics.json"), default={})

    out = {}
    for entry in manifest.get("sessions", []):
        sid = entry.get("session_id")
        log = entry.get("log")
        if not sid or not log:
            continue
        out[sid] = {
            "config"         : entry.get("config"),
            "outcome"        : entry.get("outcome"),
            "metrics"        : metrics.get(sid),
            "sweepable_axes" : _sweepable_axes(os.path.join(run_dir, log)),
        }

    return _write(os.path.join(run_dir, "comparison.json"), out)


def _sweepable_axes(log_path):
    '''Which axes have at least one scores-bearing decision in this log.'''
    if not os.path.exists(log_path):
        return []
    events = set()
    for rec in _records(log_path):
        ev = rec.get("event")
        if ev in ("route", "allocate") and rec.get("scores"):
            events.add(ev)
    return sorted(axis for axis, spec in _AXES.items()
                  if spec["event"] in events)


# ── Weight sweep ──────────────────────────────────────────────────────────────

def sweep(run_dir, session_id, axis, grid=None, bisect=False):
    '''
    Re-derive one session's decisions on `axis` ("router" or "allocator")
    across an α/β grid, from the recorded scores, and write
    sweep_comp.<axis>.json beside the manifest. Returns the result dict.

    grid: iterable of α values in [0, 1]; β is 1-α. Defaults to 0.00..1.00
    in 0.05 steps. bisect: if true, localise each winner flip between
    adjacent grid points by bisection (pure, same sweep hooks) to a small
    tolerance, so the reported flip is exact rather than grid-limited.

    The result carries:
      - `faithful`: did replay at the run's own weights reproduce every
        recorded winner. If false, `decisions`/`aggregate` are omitted and
        `reason` explains the refusal — the session is not swept.
      - `decisions`: the PRIMITIVE — per recorded decision, the winner at
        each grid point. The honest raw result.
      - `aggregate`: the PRESENTABLE view derived from it — per grid point,
        the winner distribution across decisions, plus the flip points
        where that distribution shifts (localised if bisect).
    '''
    if axis not in _AXES:
        raise ValueError(
            f"unknown sweep axis {axis!r}; choose one of {sorted(_AXES)}")
    spec = _AXES[axis]
    grid = list(grid) if grid is not None else [round(0.05 * i, 2)
                                                for i in range(21)]

    log_path = _session_log(run_dir, session_id)
    engine   = _reconstruct(spec["kind"], session_id, run_dir)

    result = {
        "session_id": session_id,
        "axis"      : axis,
        "grid"      : grid,
        "bisect"    : bisect,
    }

    if engine is None or not engine.is_sweepable():
        result["faithful"] = False
        result["reason"]   = (
            f"the {axis} in session {session_id!r} is not a scoring "
            f"component, so there is nothing to sweep")
        return _write(_sweep_path(run_dir, axis), result)

    decisions = _read_decisions(log_path, spec)
    if not decisions:
        result["faithful"] = False
        result["reason"]   = (
            f"no {spec['event']} decisions with scores in session "
            f"{session_id!r}")
        return _write(_sweep_path(run_dir, axis), result)

    # Faithfulness anchor: replay each decision at the weights the run
    # actually used and require the recorded winner back. The run's weights
    # are recorded in every score's terms (live_params of the component).
    anchor_params = _recorded_params(decisions[0])
    for d in decisions:
        replayed = engine.sweep_decision(d["recorded_terms"], anchor_params)
        if replayed != d["winner"]:
            result["faithful"] = False
            result["reason"]   = (
                f"replay at the run's own weights did not reproduce the "
                f"recorded winner (job {d['job_id']}: recorded "
                f"{d['winner']}, replayed {replayed}); this {axis} is not a "
                f"pure function of its logged terms and cannot be swept")
            return _write(_sweep_path(run_dir, axis), result)
    result["faithful"] = True

    # The primitive: each decision's winner at each grid point.
    swept = []
    for d in decisions:
        winners = {a: _winner_at(engine, d["recorded_terms"], a, axis)
                   for a in grid}
        swept.append({
            "job_id" : d["job_id"],
            "winner_by_alpha": {str(a): _jsonable(w) for a, w in winners.items()},
        })
    result["decisions"] = swept

    # The presentable aggregate, derived in the same pass.
    result["aggregate"] = _aggregate(engine, decisions, grid, axis, bisect)

    return _write(_sweep_path(run_dir, axis), result)


# ── Sweep internals ───────────────────────────────────────────────────────────

def _read_decisions(log_path, spec):
    '''
    Pull every scores-bearing decision of one axis out of the log as
    {job_id, winner, recorded_terms}, where recorded_terms is the
    [(candidate_key, terms), ...] the Sweepable hooks consume.
    '''
    out = []
    for rec in _records(log_path):
        if rec.get("event") != spec["event"] or not rec.get("scores"):
            continue
        recorded_terms = [(_cand_key(s[spec["cand_key"]]), s["terms"])
                          for s in rec["scores"]]
        out.append({
            "job_id"        : rec.get("job_id"),
            "winner"        : _cand_key(rec[spec["winner_key"]]),
            "recorded_terms": recorded_terms,
        })
    return out


def _winner_at(engine, recorded_terms, alpha, axis):
    '''Re-derive the winner at cost weights (alpha, 1-alpha).'''
    return engine.sweep_decision(recorded_terms, _cost_params(alpha, axis))


def _aggregate(engine, decisions, grid, axis, bisect):
    '''
    Per grid point, the distribution of winners across decisions, plus the
    α values where that distribution changes (the flips), optionally
    localised by bisection between the bracketing grid points.
    '''
    dist_by_alpha = {}
    for a in grid:
        counts = {}
        for d in decisions:
            w = _winner_at(engine, d["recorded_terms"], a, axis)  # hashable key
            counts[w] = counts.get(w, 0) + 1
        dist_by_alpha[a] = counts

    flips = []
    for lo, hi in zip(grid, grid[1:]):
        if dist_by_alpha[lo] != dist_by_alpha[hi]:
            at = _bisect_flip(engine, decisions, lo, hi, axis) if bisect else None
            flips.append({
                "between": [lo, hi],
                "at"     : at,
                "from"   : _dist_jsonable(dist_by_alpha[lo]),
                "to"     : _dist_jsonable(dist_by_alpha[hi]),
            })

    return {
        "winner_distribution": {str(a): _dist_jsonable(c)
                                for a, c in dist_by_alpha.items()},
        "flips": flips,
    }


def _dist_jsonable(counts):
    '''A winner->count map with candidate keys rendered as JSON strings
    (a block tuple becomes its list's str, a device index its own str).'''
    return {str(_jsonable(k)): v for k, v in counts.items()}


def _bisect_flip(engine, decisions, lo, hi, axis, tol=1e-4):
    '''
    Localise the α where the aggregate winner distribution flips, between
    lo and hi, to tolerance tol — pure, using only the sweep hooks. Returns
    the α at (or just above) the flip.
    '''
    def dist(a):
        c = {}
        for d in decisions:
            w = _winner_at(engine, d["recorded_terms"], a, axis)  # hashable
            c[w] = c.get(w, 0) + 1
        return c

    lo_dist = dist(lo)
    while hi - lo > tol:
        mid = (lo + hi) / 2
        if dist(mid) == lo_dist:
            lo = mid
        else:
            hi = mid
    return round(hi, 6)


def _reconstruct(kind, session_id, run_dir):
    '''
    A default-constructed instance of the session's registered component
    for `kind`, used purely as a scoring engine (the sweep passes weights
    explicitly, so the instance's own weights are irrelevant). Reads the
    component name from the manifest's per-session config. Returns None if
    the session or its component cannot be resolved.
    '''
    manifest = _load_json(os.path.join(run_dir, "manifest.json"))
    config = None
    for entry in manifest.get("sessions", []):
        if entry.get("session_id") == session_id:
            config = entry.get("config")
            break
    if not config or kind not in config:
        return None
    try:
        cls = DevQ()._registry.get(kind, config[kind])
        return cls()
    except Exception:
        return None


def _recorded_params(decision):
    '''
    The weights the run actually used, read from a decision's recorded
    terms — the component logged its live_params into every score's terms,
    so the anchor replays against exactly what ran.
    '''
    _key, terms = decision["recorded_terms"][0]
    return {k: v for k, v in terms.items() if k.endswith("_weight")}


def _cost_params(alpha, axis):
    '''
    Cost weights for a sweep point. Both scored axes read qubit/edge
    weights; the router additionally needs its queue/noise split, which the
    sweep holds fixed (it sweeps the α/β cost ratio, the shared-scope axis
    COST_MODEL describes, not the router's queue/noise mix). The fixed 0.5
    split matches the router default and is the recorded run's own value
    for the shipped configs.
    '''
    params = {"qubit_error_weight": alpha, "edge_error_weight": 1 - alpha}
    if axis == "router":
        params["router_queue_weight"] = 0.5
        params["router_noise_weight"] = 0.5
    return params


# ── Small shared helpers ──────────────────────────────────────────────────────

def _cand_key(value):
    '''A candidate key: a device index stays an int; a block list becomes a
    tuple so it is hashable and comparable, matching the allocator's key.'''
    return tuple(value) if isinstance(value, list) else value


def _jsonable(key):
    '''A candidate key rendered for JSON (tuples -> lists).'''
    return list(key) if isinstance(key, tuple) else key


def _records(log_path):
    with open(log_path) as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def _session_log(run_dir, session_id):
    manifest = _load_json(os.path.join(run_dir, "manifest.json"))
    for entry in manifest.get("sessions", []):
        if entry.get("session_id") == session_id:
            return os.path.join(run_dir, entry["log"])
    raise ValueError(f"no session {session_id!r} in {run_dir}")


def _sweep_path(run_dir, axis):
    return os.path.join(run_dir, f"sweep_comp.{axis}.json")


def _load_json(path, default=None):
    if default is not None and not os.path.exists(path):
        return default
    with open(path) as handle:
        return json.load(handle)


def _write(path, payload):
    '''
    Serialise deterministically and round-trip the return value through the
    same JSON, so what a caller gets in memory is exactly what a reader of
    the file gets (string keys and all) — one representation, no surprise
    at the file boundary, the same discipline write_metrics uses.
    '''
    text = json.dumps(payload, indent=2, sort_keys=True)
    with open(path, "w") as handle:
        handle.write(text)
    return json.loads(text)