'''
Tags: Main

DevQ metrics layer — a finished run in, a metric bundle out.

OFFLINE AND PURE. Every function here reads a list of already-emitted
records and returns plain data. Nothing runs during execution, nothing
touches the kernel, and nothing here writes a file — persistence is a
separate concern (see `write_metrics`). The same records list serves a
`RecordSink().records` and a parsed `.jsonl`, so a caller never branches
on where the run came from.

The definitions below are the canonical implementation of
`docs/METRICS.md`. Read that first: it states each formula, the two-clock
rule (timings come from `*_at`, never `seq`/`cycle`), the population rule
(skip `None`, never zero-fill; empty population is `None`, not `0`), and
the reproducibility obligation (plain data, fixed conventions) that this
module is written to satisfy.

Currently implemented: throughput, queue latency, utilisation. Rejection
rate, load balance and fidelity are named in the doc but not yet built.
'''

from statistics import mean, median


# ── foundations ─────────────────────────────────────────────────────────

def _summary(records):
    '''
    The closing per-job table. Metrics aggregate these rows.

    A well-formed log closes with exactly one `summary`; we take the last
    so a caller passing extra trailing records (or a MultiSink that also
    captured something) still resolves to the run's real summary.
    '''
    summaries = [r for r in records if r.get("event") == "summary"]
    if not summaries:
        raise ValueError("no summary record — not a finished run")
    return summaries[-1]


def _valid(rows, *fields):
    '''
    THE POPULATION RULE, IN ONE PLACE. Keep only rows where every named
    field is present and non-None.

    A REJECTED or never-dispatched job has None timings; it is skipped,
    not folded in as a zero. Every metric routes its population through
    here, so the skip rule cannot drift between metrics.
    '''
    return [r for r in rows
            if all(r.get(f) is not None for f in fields)]


def _span(rows, start_field, end_field):
    '''
    max(end) - min(start) over rows valid on BOTH endpoints. None when
    the population is empty — an undefined span, never a zero one.
    '''
    valid = _valid(rows, start_field, end_field)
    if not valid:
        return None
    return (max(r[end_field] for r in valid)
            - min(r[start_field] for r in valid))


# ── throughput ──────────────────────────────────────────────────────────

def throughput(records):
    '''
    Two figures, each a job count over a wall-clock span. See
    docs/METRICS.md — the two dropped 2x2 cells are deliberate.
    '''
    rows = _summary(records)["per_job"]

    # Execution: jobs that completed without rejection, over the span in
    # which devices were actually working. Numerator and both span
    # endpoints range over the same dispatched-and-resolved set.
    exec_rows = _valid(rows, "dispatched_at", "resolved_at")
    exec_span = _span(exec_rows, "dispatched_at", "resolved_at")
    execution = (len(exec_rows) / exec_span
                 if exec_span else None)

    # Turnaround: everything submitted, over first-submit to last-resolve.
    # Span start exists for every job; the end is over jobs that resolved.
    turn_span = _span(rows, "submitted_at", "resolved_at")
    turnaround = (len(rows) / turn_span
                  if turn_span else None)

    return {"execution": execution, "turnaround": turnaround}


# ── queue latency ───────────────────────────────────────────────────────

def _percentile_nearest_rank(values, pct):
    '''
    Nearest-rank percentile: no interpolation. For sorted values of
    length n, rank = ceil(pct/100 * n), clamped to [1, n], and the
    value at that 1-based rank is returned.

    FIXED CONVENTION (docs/METRICS.md): the runs here have small job
    counts, so interpolating between samples would invent precision the
    data does not have and make a hand-computed test value a fussy
    fraction. Nearest-rank keeps p95 of five sorted values equal to the
    fifth — checkable by hand. This choice is load-bearing for
    reproducibility and must not be swapped for a library default.
    '''
    ordered = sorted(values)
    n = len(ordered)
    import math
    rank = math.ceil(pct / 100 * n)
    rank = max(1, min(rank, n))
    return ordered[rank - 1]


def queue_latency(records):
    '''
    Per-job pure queue wait (dispatched_at - submitted_at), reported as a
    distribution. The already-computed `queue_latency` field is the same
    quantity, so this aggregates rather than re-derives.
    '''
    rows = _summary(records)["per_job"]
    waits = [r["queue_latency"]
             for r in _valid(rows, "queue_latency")]

    if not waits:
        return {k: None for k in
                ("min", "median", "mean", "max", "p95")}

    return {
        "min"   : min(waits),
        "median": median(waits),
        "mean"  : mean(waits),
        "max"   : max(waits),
        "p95"   : _percentile_nearest_rank(waits, 95),
    }


# ── utilisation ─────────────────────────────────────────────────────────

def _union_length(intervals):
    '''
    Total length covered by a set of [start, end) intervals, counting
    overlap once. Concurrent jobs on one device overlap, so busy time is
    the union, not the sum — summing double-counts and can exceed the
    elapsed window.

    Sort by start, sweep, merge. A zero-length interval contributes
    nothing and cannot extend a merge.
    '''
    if not intervals:
        return 0.0
    ordered = sorted(intervals)
    total = 0.0
    cur_start, cur_end = ordered[0]
    for start, end in ordered[1:]:
        if start > cur_end:            # gap: close the run, open a new one
            total += cur_end - cur_start
            cur_start, cur_end = start, end
        else:                          # overlap or touch: extend
            cur_end = max(cur_end, end)
    total += cur_end - cur_start
    return total


def utilisation(records):
    '''
    Device-busy fraction, per device and system-wide.

    Numerator is the union of a device's [dispatched_at, resolved_at)
    intervals. Denominator is the SHARED run window — execution-span,
    max(resolved_at) - min(dispatched_at) across all dispatched jobs — so
    per-device fractions are comparable to each other and to the system
    figure, and the pre-dispatch queue period (no device could be busy
    then) is excluded.

    A job dispatched but never resolved contributes no interval, same as
    an undispatched one: an open-ended interval has no length.
    '''
    rows = _summary(records)["per_job"]
    dispatched = _valid(rows, "dispatched_at", "resolved_at")

    window = _span(dispatched, "dispatched_at", "resolved_at")
    if not window:
        # No job dispatched: the window is undefined, so every fraction
        # is undefined too — None, not zero.
        return {"per_device": {}, "system": None}

    by_device = {}
    for r in dispatched:
        by_device.setdefault(r["device"], []).append(
            (r["dispatched_at"], r["resolved_at"]))

    per_device = {}
    for dev in sorted(by_device):
        busy = _union_length(by_device[dev])
        per_device[dev] = busy / window

    # System-wide: total union-busy across all devices over
    # (window x device count). This is the busy-weighted mean of the
    # per-device fractions, so the two figures are consistent by
    # construction. Device count is the number of devices that ran work.
    total_busy = sum(_union_length(by_device[dev]) for dev in by_device)
    system = total_busy / (window * len(by_device))

    return {"per_device": per_device, "system": system}


# ── bundle ──────────────────────────────────────────────────────────────

def compute(records):
    '''
    The full metric bundle for one run. Plain data, deterministic key
    order, None preserved — a published-artifact surface, so two callers
    on the same records get byte-identical output.
    '''
    return {
        "throughput"  : throughput(records),
        "queue_latency": queue_latency(records),
        "utilisation" : utilisation(records),
    }


# ── persistence (the ONLY side-effect in this module) ───────────────────

def write_metrics(run_dir):
    '''
    Compute each session's metrics from its log and write metrics.json
    beside the manifest. Returns the mapping it wrote.

    This is the thin writer, kept apart from the pure functions above:
    the comparative modes (inter/intra-component) load metrics.json to
    diff runs, so the artifact has a reason to exist independent of any
    command. A future `qbench` (Phase 5.7) renders the SAME numbers to a
    terminal by calling `compute`; it does not recompute them a second
    way, so there is one source of truth and the file and the view never
    drift.
    '''
    import json
    import os

    manifest_path = os.path.join(run_dir, "manifest.json")
    with open(manifest_path) as handle:
        manifest = json.load(handle)

    out = {}
    for entry in manifest.get("sessions", []):
        log = entry.get("log")
        # A crashed session's log is deliberately kept under a name
        # readers should not trust; it has no clean summary, so skip it.
        if not log:
            continue
        log_path = os.path.join(run_dir, log)
        if not os.path.exists(log_path):
            continue
        with open(log_path) as handle:
            records = [json.loads(line) for line in handle if line.strip()]
        try:
            out[entry["session_id"]] = compute(records)
        except ValueError:
            # No summary — an incomplete or crashed log. Record nothing
            # rather than a misleading zero.
            continue

    # JSON has no integer keys, so the per-device utilisation map lands
    # on disk with STRING device keys. Round-trip the return value
    # through the same serialisation so what a caller gets in memory is
    # exactly what a reader of metrics.json gets — the comparative modes
    # read the file, and an in-memory dict with int keys would not match
    # it. One representation, no surprise at the file boundary.
    metrics_path = os.path.join(run_dir, "metrics.json")
    payload = json.dumps(out, indent=2, sort_keys=True)
    with open(metrics_path, "w") as handle:
        handle.write(payload)
    return json.loads(payload)