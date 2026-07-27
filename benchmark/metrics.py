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

    # Label per-device output by device id, consistent with load
    # imbalance and readable in metrics.json. The roster maps index -> id;
    # a device that ran always has an entry. Fall back to the index as a
    # string when no roster is present (a pre-roster log). Note the
    # population is unchanged — only devices that RAN appear here, matching
    # the system denominator; idle devices are load imbalance's concern.
    ids = dict(_roster(records))

    per_device = {}
    for dev in sorted(by_device):
        busy = _union_length(by_device[dev])
        per_device[ids.get(dev, str(dev))] = busy / window

    # System-wide: total union-busy across all devices over
    # (window x device count). This is the busy-weighted mean of the
    # per-device fractions, so the two figures are consistent by
    # construction. Device count is the number of devices that ran work.
    total_busy = sum(_union_length(by_device[dev]) for dev in by_device)
    system = total_busy / (window * len(by_device))

    return {"per_device": per_device, "system": system}


# ── rejection rate ──────────────────────────────────────────────────────

def rejection_rate(records):
    '''
    The fraction of submitted jobs the system terminally REFUSED.

    REJECTED only. A WAITING job is accepted-but-delayed — routing found
    it a feasible device and allocation is retrying — so it is NOT a
    rejection; its retry time already lands in queue latency, because
    `dispatched_at` is stamped at real dispatch, after all retries.
    Counting WAITING here would double-punish a busy config, once as
    latency and once as a phantom rejection.

    Every job is terminal at summary time (drain does not exit while any
    job is still WAITING), so the denominator is simply all submitted
    jobs — there is no stuck-WAITING third population to decide about.

    The counts are always the true integers, even on an empty run; only
    the ratio is `None` when there is nothing to divide, since a run with
    no jobs has no meaningful rejection fraction but still, truthfully,
    rejected zero of zero.
    '''
    rows = _summary(records)["per_job"]
    submitted = len(rows)
    rejected = sum(1 for r in rows if r.get("state") == "REJECTED")
    rate = rejected / submitted if submitted else None
    return {"rejected": rejected, "submitted": submitted, "rate": rate}


# ── load balance ────────────────────────────────────────────────────────

def _cv(values):
    '''
    Population coefficient of variation: stddev / mean, with population
    stddev (denominator n, not n-1). We measure the actual set of devices
    in this run, not a sample from a larger population, so n is correct —
    and it keeps the value reproducible and hand-checkable.

    Returns None when the mean is zero (no load to spread), since CV is
    undefined there rather than zero. A single value has zero spread, so
    CV is 0.0 — one device cannot be imbalanced.
    '''
    n = len(values)
    if n == 0:
        return None
    m = sum(values) / n
    if m == 0:
        return None
    var = sum((v - m) ** 2 for v in values) / n
    return (var ** 0.5) / m


def _roster(records):
    '''
    Attached-device roster as an ordered list of (index, id).

    Prefers the summary's `devices_attached` — the whole point of
    recording it is that per_job names only devices that RAN, so an idle
    device is invisible without the roster. Falls back to the devices
    that appear in per_job when the roster is absent (a hand-built or
    pre-roster log); that fallback cannot see idle devices, so a real run
    always carries the roster.
    '''
    summary = _summary(records)
    attached = summary.get("devices_attached")
    if attached:
        return [(int(i), name) for i, name in sorted(
            attached.items(), key=lambda kv: int(kv[0]))]
    # Fallback: only devices that ran are recoverable.
    seen = {}
    for r in summary["per_job"]:
        dev = r.get("device")
        if dev is not None:
            seen.setdefault(dev, str(dev))
    return sorted(seen.items())


def load_imbalance(records):
    '''
    Load imbalance: how evenly work spread across ALL attached devices —
    including idle ones, which is the whole point: a router that starves a
    device is the opposite of balanced, and that is only visible if idle
    devices count as zero load.

    Reported on two bases, because they can disagree — a device running
    one long job versus three short ones is balanced by count but not by
    busy time:
      - by_count:     dispatched job count per device
      - by_busy_time: union-busy time per device (reusing utilisation's
                      interval union, so "busy" means one thing)

    Each basis carries the per-device distribution (labelled by device
    id), the population coefficient of variation `cv` (0 = perfectly
    balanced, growing with imbalance), and a `load_balance` convenience
    reading `1 / (1 + cv)` in (0, 1] for whoever wants "higher = better"
    without inverting the CV themselves. When cv is None (no load to
    spread), load_balance is None too.
    '''
    rows = _summary(records)["per_job"]
    roster = _roster(records)

    # Count per device: every attached device, idle ones at 0.
    counts = {idx: 0 for idx, _ in roster}
    for r in rows:
        dev = r.get("device")
        if dev is not None and dev in counts:
            counts[dev] += 1

    # Busy time per device: union of each device's intervals, idle at 0.0.
    intervals = {idx: [] for idx, _ in roster}
    for r in rows:
        dev = r.get("device")
        if (dev is not None and dev in intervals
                and r.get("dispatched_at") is not None
                and r.get("resolved_at") is not None):
            intervals[dev].append((r["dispatched_at"], r["resolved_at"]))
    busy = {idx: _union_length(ivs) for idx, ivs in intervals.items()}

    def basis(per_index):
        labelled = {name: per_index[idx] for idx, name in roster}
        cv = _cv([per_index[idx] for idx, _ in roster])
        balance = 1 / (1 + cv) if cv is not None else None
        return {"per_device": labelled, "cv": cv, "load_balance": balance}

    return {"by_count": basis(counts), "by_busy_time": basis(busy)}


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
        "rejection_rate": rejection_rate(records),
        "load_imbalance": load_imbalance(records),
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