'''
Tags: Main

Cross-config comparison for a finished run (Phase 5.5a).

Two offline reads over a run directory, both pure in the sense metrics.py
is pure — they read logs and the manifest and compute; they execute no
circuits and touch no device:

  assemble_matrix(run_dir)  -> the comparison bundle, written comparison.json
  sweep(run_dir, session_id, axis, coarse_m=20, bisect=False, registry_map=None)
        -> the weight-sweep result, written sweep_comp.<axis>.json

The matrix bundle is the inter-component surface: one row per session, its
config and its metrics, so the 5.5b modes can diff config A against config
B without re-reading logs. The sweep is the intra-component surface: it
re-derives one session's router, allocator, OR scheduler decisions across
that component's weight-group simplex (the Scheffe {n, m} lattice; at n=2 the
historical (alpha, 1-alpha) grid) FROM THE RECORDED SCORES, so "how would this
config decide at other weights" is answered from one recorded run rather than
by re-executing.

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
        "event"        : "route",
        "winner_key"   : "device",
        "cand_key"     : "device",
        "kind"         : "router",
        # A scored router's swept weight keys come from its own live_params()
        # (None = derive), so a plugin router with its OWN weight group (e.g.
        # QOS's qos.fidelity_weight/qos.util_weight) is sweepable without a
        # core edit — the same generic treatment the scheduler axis has. The
        # built-in NoiseRouter's live_params() returns exactly the qubit/edge
        # cost split, so deriving reproduces the historical group for it; its
        # fixed queue/noise mix is kept out of live_params() and recovered from
        # the recorded terms on replay.
        "weight_group" : None,
    },
    "allocator": {
        "event"        : "allocate",
        "winner_key"   : "block",
        "cand_key"     : "block",
        "kind"         : "allocator",
        # Derived from live_params() (None), as the router and scheduler axes
        # are — so a plugin allocator that scores on its own weight group is
        # sweepable without a core edit. NoiseGraphAllocator's live_params()
        # returns the qubit/edge split, reproducing the historical group.
        "weight_group" : None,
    },
    "scheduler": {
        "event"        : "schedule",
        "winner_key"   : "winner",
        "cand_key"     : "job_id",
        "kind"         : "scheduler",
        # A scored scheduler's weight keys are plugin-specific (e.g. NAQJS's
        # naqjs_width/shots/seq_weight), so they are NOT hardcoded here —
        # naming a plugin in core would couple the sweep infra to it. None
        # means "derive the swept keys from the reconstructed component's
        # live_params()", which is the contract's own authoritative
        # declaration of the weights it scores with. Keeps the scheduler axis
        # generic: any scored scheduler is sweepable without a core edit.
        "weight_group" : None,
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
    scoring router, allocator, or scheduler leaves a scores-bearing event, a
    non-scoring one does not — so a reader knows where an intra-component sweep
    is available without opening the log.
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
    # Derived from _AXES rather than a hardcoded event list, so a newly wired
    # axis (e.g. scheduler) is detected automatically — the same reason the
    # ranking below reads the axis set from _AXES.
    axis_events = {spec["event"] for spec in _AXES.values()}
    events = set()
    for rec in _records(log_path):
        ev = rec.get("event")
        if ev in axis_events and rec.get("scores"):
            events.add(ev)
    return sorted(axis for axis, spec in _AXES.items()
                  if spec["event"] in events)


# ── Weight sweep ──────────────────────────────────────────────────────────────

def sweep(run_dir, session_id, axis, coarse_m=20, bisect=False,
          registry_map=None):
    '''
    Re-derive one session's decisions on `axis` ("router", "allocator", or
    "scheduler") across its weight-group simplex, from the recorded scores, and
    write sweep_comp.<axis>.json beside the manifest. Returns the result dict.

    The weight group of n terms lives on the Scheffe {n, m} simplex-lattice
    (Scheffe 1958; see _simplex_lattice and docs/REFERENCES.md). `coarse_m`
    is the lattice resolution m: points = C(m+n-1, n-1). At n=2 (the router and
    allocator, which sweep the shared qubit/edge pair) the lattice is the
    historical (alpha, 1-alpha) grid; a scored scheduler's weight group may be
    larger and its keys are derived from the component's live_params(). bisect:
    if true, localise each winner flip along the lattice EDGE that brackets
    it, by bisection (pure, same sweep hooks) to a small tolerance — exact
    where valid (an edge is a single-crossing 1-D interval), never along an
    interior chord.

    The result carries:
      - `faithful`: did replay at the run's own weights reproduce every
        recorded winner. If false, `decisions`/`aggregate` are omitted and
        `reason` explains the refusal — the session is not swept.
      - `decisions`: the PRIMITIVE — per recorded decision, the winner at
        each lattice point ({point, winner} records). The honest raw result.
      - `aggregate`: the PRESENTABLE view — per lattice point, the winner
        distribution across decisions, plus the flip edges where that
        distribution shifts (localised along the edge if bisect).
    '''
    if axis not in _AXES:
        raise ValueError(
            f"unknown sweep axis {axis!r}; choose one of {sorted(_AXES)}")
    spec = _AXES[axis]

    log_path = _session_log(run_dir, session_id)
    engine   = _reconstruct(spec["kind"], session_id, run_dir, registry_map)

    result = {
        "session_id": session_id,
        "axis"      : axis,
        "coarse_m"  : coarse_m,
        "bisect"    : bisect,
    }

    if engine is None or not engine.is_sweepable():
        result["faithful"] = False
        result["reason"]   = (
            f"the {axis} in session {session_id!r} is not a scoring "
            f"component, so there is nothing to sweep")
        return _write(_sweep_path(run_dir, axis), result)

    # The swept weight keys: an axis may hardcode them (router/allocator share
    # the core qubit/edge pair), or leave weight_group None to derive them from
    # the component's own live_params() — used by the scheduler axis so a
    # plugin's private key names (naqjs_*) never enter core. Sorted for a
    # stable lattice-coordinate -> key mapping across runs.
    weight_keys = spec["weight_group"]
    if weight_keys is None:
        weight_keys = sorted(engine.live_params().keys())
    result["weight_keys"] = weight_keys

    decisions = _read_decisions(log_path, spec)
    if not decisions:
        result["faithful"] = False
        result["reason"]   = (
            f"no {spec['event']} decisions with scores in session "
            f"{session_id!r}")
        return _write(_sweep_path(run_dir, axis), result)

    # Faithfulness anchor: replay each decision at the parameters the run
    # actually used and require the recorded winner back. The run's params
    # are recorded in every score's terms (live_params of the component), and
    # are recovered by the component's OWN full live_params keys — the same
    # authoritative set the swept keys derive from — never by a name
    # convention. (The full set, not just the swept weight-group: e.g. the
    # router sweeps qubit/edge but its scoring also reads the fixed
    # queue/noise weights.)
    anchor_params = _recorded_params(decisions[0], engine.live_params().keys())
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

    # The lattice: the Scheffe {n, m} simplex over this axis's weight group.
    n = len(weight_keys)
    int_pts = _int_lattice(n, coarse_m)
    points  = [tuple(k / coarse_m for k in c) for c in int_pts]

    # The primitive: each decision's winner at each lattice point. Points are
    # weight vectors (JSON arrays), not scalars — a list of {point, winner}
    # records rather than an alpha-keyed dict, so it reads faithfully at any n.
    swept = []
    for d in decisions:
        winner_by_point = [
            {"point": [round(x, 6) for x in p],
             "winner": _jsonable(_winner_at(engine, d["recorded_terms"], p, axis, weight_keys))}
            for p in points
        ]
        swept.append({
            "job_id"          : d["job_id"],
            "winner_by_point" : winner_by_point,
        })
    result["decisions"] = swept

    # The presentable aggregate, derived over the lattice edge graph.
    result["aggregate"] = _aggregate(
        engine, decisions, int_pts, points, axis, weight_keys, bisect)

    return _write(_sweep_path(run_dir, axis), result)


# ── Sweep internals ───────────────────────────────────────────────────────────

# Adaptive simplex-refinement engine. The weight space of n linear-combination
# terms is the Scheffe (n-1)-simplex (see _simplex_lattice and docs/REFERENCES.md
# — Scheffe 1958). The winner a weight point induces is piecewise-constant: it is
# constant within cells and jumps across straight tie-loci where two candidates'
# scores cross. We sample a coarse lattice, find the lattice EDGES whose endpoints
# disagree, and localise each crossing by bisection ALONG THAT EDGE. Bisection is
# valid only along an edge (a one-unit move between two weights): there the segment
# is a 1-D interval a single tie-locus crosses once, so the midpoint bracket holds.
# It is NOT valid along an arbitrary chord through the interior (multiple crossings,
# no single flip), which is why flip detection walks the edge graph, never
# list-consecutive points. At n=2 the edge graph IS the consecutive chain, so this
# reduces exactly to the historical scalar-alpha grid + interval bisection.

def _lattice_edges(int_points):
    '''
    The geometric adjacency graph of an integer simplex lattice: two points are
    neighbours iff one differs from the other by moving a single unit from one
    coordinate to another (all others equal). Returns index pairs (i, j) with
    i < j, over the canonical-order `int_points`. At n=2 this is exactly the
    consecutive chain (0,1),(1,2),...; at n>=3 it is the connected edge graph of
    the triangle/tetrahedron/... whose crossings witness every cell boundary.
    '''
    index = {pt: i for i, pt in enumerate(int_points)}
    edges = []
    for pt in int_points:
        i = index[pt]
        for a in range(len(pt)):
            if pt[a] == 0:
                continue
            for b in range(len(pt)):
                if a == b:
                    continue
                nb = list(pt)
                nb[a] -= 1
                nb[b] += 1
                j = index.get(tuple(nb))
                if j is not None and i < j:
                    edges.append((i, j))
    return edges


def _int_lattice(n, m):
    '''The Scheffe {n, m} lattice as integer compositions (canonical lex order);
    _simplex_lattice divides these by m. Kept separate so edge adjacency can be
    expressed on the exact integer coordinates, where "one-unit move" is crisp.'''
    def _compositions(total, parts):
        if parts == 1:
            yield (total,)
            return
        for first in range(total + 1):
            for rest in _compositions(total - first, parts - 1):
                yield (first,) + rest
    return sorted(_compositions(m, n))


def _refine_edge(decide, lo_pt, hi_pt, tol):
    '''
    Localise the winner flip along the straight segment lo_pt -> hi_pt (two
    normalised weight tuples forming a lattice edge) to weight-space tolerance
    `tol`, by bisection. Pure — uses only `decide`, the point->winner callback.

    Returns (flip_point, winners_seen): flip_point is the normalised weight tuple
    at (or just past) the crossing, or None if the endpoints agree. winners_seen
    is every distinct winner observed while refining, so the caller's winner set
    stays complete even for winners that live only in a thin sliver mid-edge.
    '''
    dim = len(lo_pt)

    def at(t):
        w = tuple(lo_pt[k] * (1 - t) + hi_pt[k] * t for k in range(dim))
        s = sum(w) or 1.0
        return tuple(x / s for x in w)

    d_lo = decide(at(0.0))
    d_hi = decide(at(1.0))
    seen = {d_lo, d_hi}
    if d_lo == d_hi:
        return None, seen

    seg = sum((hi_pt[k] - lo_pt[k]) ** 2 for k in range(dim)) ** 0.5
    lo_t, hi_t = 0.0, 1.0
    while (hi_t - lo_t) * seg > tol:
        mid = (lo_t + hi_t) / 2
        d_mid = decide(at(mid))
        seen.add(d_mid)
        if d_mid == d_lo:
            lo_t = mid
        else:
            hi_t = mid
    return at(hi_t), seen


def _read_decisions(log_path, spec):
    '''
    Pull every scores-bearing decision of one axis out of the log as
    {job_id, winner, recorded_terms}, where recorded_terms is the
    [(candidate_key, terms), ...] the Sweepable hooks consume.

    A batch scheduler is special: it emits one `schedule` event PER dispatched
    job in a cycle, but all of them share ONE ranking snapshot (the same
    recorded_terms), and each event's `winner` field is the job THAT event
    dispatched — not the ranking's argmin. For the sweep, a cycle's ranking is
    ONE decision, whose winner is the argmin of that ranking (lowest
    (score, key)), consistent with how a router/allocator's single choice is
    its argmin. So scheduler events are deduplicated by their ranking snapshot
    and the winner is recomputed as the argmin, rather than taken from the
    per-dispatch `winner` field. Router/allocator (one event = one choice) are
    unaffected.
    '''
    dedup = spec["kind"] == "scheduler"
    out = []
    seen_snapshots = set()
    for rec in _records(log_path):
        if rec.get("event") != spec["event"] or not rec.get("scores"):
            continue
        recorded_terms = [(_cand_key(s[spec["cand_key"]]), s["terms"])
                          for s in rec["scores"]]

        if dedup:
            # Collapse events that share this ranking; the winner is the
            # ranking's argmin from the recorded per-candidate scores, not the
            # per-event dispatched job.
            snapshot = tuple((k, tuple(sorted(t.items())))
                             for k, t in recorded_terms)
            if snapshot in seen_snapshots:
                continue
            seen_snapshots.add(snapshot)
            winner = min(rec["scores"],
                         key=lambda s: (s["score"],
                                        _cand_key(s[spec["cand_key"]])))
            winner = _cand_key(winner[spec["cand_key"]])
        else:
            winner = _cand_key(rec[spec["winner_key"]])

        out.append({
            "job_id"        : rec.get("job_id"),
            "winner"        : winner,
            "recorded_terms": recorded_terms,
        })
    return out


def _winner_at(engine, recorded_terms, point, axis, weight_keys):
    '''Re-derive the winner at the weight vector `point` (a normalised tuple
    mapped onto weight_keys).'''
    return engine.sweep_decision(recorded_terms,
                                 _cost_params(point, axis, weight_keys))


def _aggregate(engine, decisions, int_pts, points, axis, weight_keys, bisect):
    '''
    Per lattice point, the distribution of winners across decisions, plus the
    flip EDGES where that distribution changes. A flip is detected on a lattice
    edge (a one-unit move between two weights), never along a list-consecutive
    chord — at n=2 the edge graph is the consecutive chain, so this matches the
    historical behaviour; at n>=3 the edges tile the simplex boundary. Each flip
    is localised along its own edge by bisection when `bisect` is set.
    '''
    def dist_at(point):
        counts = {}
        for d in decisions:
            w = _winner_at(engine, d["recorded_terms"], point, axis, weight_keys)
            counts[w] = counts.get(w, 0) + 1
        return counts

    dist = [dist_at(p) for p in points]

    def group_dist(point):
        '''The winner distribution as a hashable key, for edge refinement.'''
        return tuple(sorted(dist_at(point).items()))

    flips = []
    for i, j in _lattice_edges(int_pts):
        if dist[i] != dist[j]:
            at = None
            if bisect:
                flip_pt, _seen = _refine_edge(group_dist, points[i], points[j],
                                              tol=1e-4)
                at = [round(x, 6) for x in flip_pt] if flip_pt else None
            flips.append({
                "between": [[round(x, 6) for x in points[i]],
                            [round(x, 6) for x in points[j]]],
                "at"     : at,
                "from"   : _dist_jsonable(dist[i]),
                "to"     : _dist_jsonable(dist[j]),
            })

    return {
        "winner_distribution": [
            {"point": [round(x, 6) for x in p], "dist": _dist_jsonable(c)}
            for p, c in zip(points, dist)
        ],
        "flips": flips,
    }


def _dist_jsonable(counts):
    '''A winner->count map with candidate keys rendered as JSON strings
    (a block tuple becomes its list's str, a device index its own str).'''
    return {str(_jsonable(k)): v for k, v in counts.items()}


def _reconstruct(kind, session_id, run_dir, registry_map=None):
    '''
    A default-constructed instance of the session's registered component
    for `kind`, used purely as a scoring engine (the sweep passes weights
    explicitly, so the instance's own weights are irrelevant). Reads the
    component name from the manifest's per-session config. Returns None if
    the session or its component cannot be resolved.

    `registry_map` is an optional {kind: {name: class}} map of components
    registered for the run but not globally (a research/ baseline like
    NAQJS, registered per-run via register_schedulers). Without it, only
    built-in components resolve — so a sweep of a plugin must pass the same
    class map the run registered, or the fresh DevQ() here cannot rebuild
    it. Built-in sweeps (router/allocator) pass nothing and are unchanged.
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
        owner = DevQ()
        for reg_kind, classes in (registry_map or {}).items():
            register = getattr(owner, f"register_{reg_kind}")
            for name, cls in classes.items():
                register(name, cls)
        cls = owner._registry.get(kind, config[kind])
        if kind == "scheduler":
            # A scheduler's constructor requires (memory_manager,
            # process_table), unlike routers/allocators which default
            # everything. The sweep uses the instance PURELY as a scoring
            # engine — it calls only the Sweepable hooks (live_params,
            # _sweep_*, sweep_decision), never schedule()/the queue/memory —
            # so None placeholders are safe and never dereferenced. Weights
            # are irrelevant here too: the sweep passes them explicitly per
            # lattice point, and the faithfulness anchor reads the run's
            # actual weights from the logged terms, not from this instance.
            return cls(None, None)
        return cls()
    except Exception:
        return None


def _recorded_params(decision, param_keys):
    '''
    The parameters the run actually used, read from a decision's recorded
    terms — the component logged its live_params into every score's terms,
    so the anchor replays against exactly what ran.

    `param_keys` is the component's FULL live_params() key set — every
    parameter its scoring reads, not only the swept weight-group. The
    router, for instance, sweeps the qubit/edge pair but its scoring also
    reads the (held-fixed) queue/noise weights; the anchor must supply all
    of them. Filtering by the component's OWN declared keys — rather than
    by a "_weight" name convention — is what keeps the anchor correct for a
    third-party scoring component whose keys are named otherwise
    (e.g. qos.alpha): it agrees with live_params() exactly.
    '''
    _key, terms = decision["recorded_terms"][0]
    return {k: terms[k] for k in param_keys if k in terms}


def _simplex_lattice(n, m):
    '''
    The Scheffe {n, m} simplex-lattice: every normalised weight n-tuple whose
    entries are multiples of 1/m. Reference: Scheffe, H. (1958), "Experiments
    with Mixtures", J. R. Statist. Soc. B 20(2):344-360 — see docs/REFERENCES.md.

    Construction: enumerate the integer compositions of m into n non-negative
    parts (k_1 + ... + k_n = m) and divide each part by m. Every point sums to
    1 exactly, so there are no off-simplex points to discard. The count is
    C(m+n-1, n-1) (Scheffe's formula), and because the ranking a weight vector
    induces is scale-invariant, this lattice is the *complete* faithful search
    space for n linear-combination weights, normalised or not.

    Contract — canonical order: points are emitted in ascending lexicographic
    order of their integer composition. This is a stable, documented order so
    that "adjacent lattice points" is well-defined (bisect relies on it), and
    at n=2 it reproduces the historical ascending grid
    [(0, m/m), (1/m, (m-1)/m), ..., (m/m, 0)] point-for-point in sequence.
    '''
    def _compositions(total, parts):
        if parts == 1:
            yield (total,)
            return
        for first in range(total + 1):
            for rest in _compositions(total - first, parts - 1):
                yield (first,) + rest

    return [tuple(k / m for k in comp)
            for comp in sorted(_compositions(m, n))]


def _cost_params(point, axis, weight_keys):
    '''
    Cost weights for a sweep point. `point` is a normalised weight tuple (a
    Scheffe lattice point, sums to 1); its coordinates map onto `weight_keys`
    in order. At n=2 the group is (qubit, edge), so point (a, 1-a) reproduces
    the historical (alpha, 1-alpha) mapping exactly. `weight_keys` is resolved
    by the caller — an axis's hardcoded group, or a scored scheduler's own
    `weight_keys` is resolved
    by the caller from the component's own live_params() — so no plugin-
    specific key names live in this module, and a component's FIXED inputs
    (a router's queue/noise mix, a scheduler's eta) are not swept: they stay
    out of live_params() and are recovered from the recorded terms on replay.
    '''
    return dict(zip(weight_keys, point))


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