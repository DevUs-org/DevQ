'''
Tags: Main

The comparison modes (Phase 5.5b) — the reading surface over the 5.5a
engine. Two modes, each a pure selection/presentation over data
comparison.py already computed; they derive no new numbers:

  rank_sessions(bundle, metric)      inter-component: order the matrix's
                                     sessions by one metric
  present_sweep(sweep_result)        intra-component: read out one
                                     session's weight sweep

Each returns STRUCTURED data. `render_text(result, to=...)` turns either
into a plain-text table or report and, given a path, writes it to a `.txt`
file. The split is deliberate and matches the rest of the benchmark layer
(metrics compute vs. write; the sweep's primitive vs. aggregate): the mode
returns data so a second consumer — a test, a notebook, and the qbench
shell in Phase 5.7, which "folds in the comparison modes" — renders it its
own way without parsing a string or calling a parallel path. The text
renderer is one view over that data, not the data itself.

The *absolute* view (one session's own metric bundle) is NOT a mode here:
it is the 5.3 metric bundle, already shipped. 5.5b is the two genuine
comparisons that need the matrix and the sweep underneath.
'''

import json
import os


# ── Inter-component: rank sessions by a metric ────────────────────────────────

def rank_sessions(bundle, metric, descending=False):
    '''
    Order a matrix bundle's sessions by one metric, best first.

    `metric` is a dotted path into a session's metrics — the leaf must be a
    number — e.g. "rejection_rate.rate", "utilisation.system",
    "queue_latency.median", "load_imbalance.by_busy_time.load_balance".

    `descending` chooses the direction. The mode does NOT presume which end
    is "good" — lower is better for rejection rate, higher for utilisation,
    and only the caller knows which — so it ranks by the number and the
    caller says which way is best. Default ascending (lowest first).

    Returns a structured result:

        {"metric": <path>, "descending": <bool>,
         "rows": [{"rank", "session_id", "config", "value"}, ...],
         "missing": [session_id, ...]}

    A session whose metric is absent or null (e.g. fidelity on a mock
    provider) cannot be ranked and is listed under `missing` rather than
    sorted as a zero — the same honest treatment metrics.py gives an
    unmeasured population. `rows` is sorted; ties break by session id so
    the order is deterministic.
    '''
    ranked, missing = [], []
    for sid, entry in bundle.items():
        value = _dig(entry.get("metrics"), metric)
        if value is None:
            missing.append(sid)
        else:
            ranked.append((sid, entry.get("config"), value))

    ranked.sort(key=lambda r: (r[2], r[0]), reverse=descending)

    rows = [{"rank": i + 1, "session_id": sid, "config": config, "value": value}
            for i, (sid, config, value) in enumerate(ranked)]
    return {
        "metric"    : metric,
        "descending": descending,
        "rows"      : rows,
        "missing"   : sorted(missing),
    }


# ── Intra-component: present one session's sweep ──────────────────────────────

def present_sweep(sweep_result):
    '''
    Read out a sweep result (the dict comparison.sweep returned / wrote) as
    a structured presentation.

    A refused sweep (`faithful: false` — a non-scoring component, or a run
    whose recorded winner contradicts its scores) is presented AS a
    refusal, carrying its reason, not dropped: "this axis could not be
    swept, because ...". A faithful sweep is presented as its flips (the
    lattice EDGES where the winning distribution changes, the actionable
    output) plus the per-lattice-point winner distribution.

    Returns:

        {"session_id", "axis", "sweepable": <bool>,
         "reason": <str|None>,          # when not sweepable
         "coarse_m", "bisect",
         "flips": [{"between", "at", "from", "to"}, ...],   # when sweepable;
                                        # between/at are weight vectors
         "stable": <bool>,             # no flips anywhere on the simplex
         "distribution": [{"point", "dist"}, ...]}
    '''
    base = {
        "session_id": sweep_result.get("session_id"),
        "axis"      : sweep_result.get("axis"),
        "coarse_m"  : sweep_result.get("coarse_m"),
        "bisect"    : sweep_result.get("bisect"),
    }

    if not sweep_result.get("faithful"):
        base.update(sweepable=False,
                    reason=sweep_result.get("reason", "not sweepable"))
        return base

    agg   = sweep_result.get("aggregate", {})
    flips = agg.get("flips", [])
    base.update(
        sweepable    = True,
        reason       = None,
        flips        = flips,
        stable       = len(flips) == 0,
        distribution = agg.get("winner_distribution", []),
    )
    return base


# ── Text rendering ────────────────────────────────────────────────────────────

def render_text(result, to=None):
    '''
    Render a rank_sessions or present_sweep result as plain text, and — if
    `to` is a path — write it there (a `.txt` a user keeps). Returns the
    text either way. The kind is detected from the result's shape, so one
    renderer serves both modes.
    '''
    if "rows" in result:
        text = _render_ranking(result)
    else:
        text = _render_sweep(result)

    if to is not None:
        with open(to, "w") as handle:
            handle.write(text)
    return text


def _render_ranking(result):
    metric = result["metric"]
    order  = "highest first" if result["descending"] else "lowest first"
    lines  = [f"Sessions ranked by {metric} ({order})", ""]

    if result["rows"]:
        width = max(len(r["session_id"]) for r in result["rows"])
        lines.append(f"  {'#':>2}  {'session':<{width}}  value")
        lines.append(f"  {'-'*2}  {'-'*width}  {'-'*12}")
        for r in result["rows"]:
            lines.append(f"  {r['rank']:>2}  {r['session_id']:<{width}}  "
                         f"{_fmt(r['value'])}")
    else:
        lines.append("  (no session has this metric)")

    if result["missing"]:
        lines += ["", f"  not ranked ({len(result['missing'])}, metric "
                      f"absent or null): {', '.join(result['missing'])}"]
    return "\n".join(lines) + "\n"


def _render_sweep(result):
    axis = result["axis"]
    sid  = result["session_id"]
    head = f"weight-simplex sweep of the {axis} in session {sid}"

    if not result["sweepable"]:
        return f"{head}\n\n  not sweepable: {result['reason']}\n"

    lines = [head, ""]
    if result["stable"]:
        lines.append(f"  stable: the {axis} decision does not change across "
                     f"the swept simplex — no flip on any lattice edge.")
    else:
        lines.append(f"  {len(result['flips'])} flip edge(s) — lattice edges "
                     f"where the winning distribution changes:")
        for f in result["flips"]:
            lo, hi = f["between"]
            at = f" at w≈{_fmt_vec(f['at'])}" if f.get("at") is not None else ""
            lines.append(f"    between w={_fmt_vec(lo)} and w={_fmt_vec(hi)}{at}")
            lines.append(f"        from {_fmt_dist(f['from'])}")
            lines.append(f"        to   {_fmt_dist(f['to'])}")

    lines += ["", "  winner distribution by weight point:"]
    for entry in result["distribution"]:
        lines.append(f"    w={_fmt_vec(entry['point'])}: "
                     f"{_fmt_dist(entry['dist'])}")
    return "\n".join(lines) + "\n"


# ── Small helpers ─────────────────────────────────────────────────────────────

def _dig(obj, dotted):
    '''Follow a dotted path into nested dicts; None if any step is missing.'''
    cur = obj
    for key in dotted.split("."):
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur if isinstance(cur, (int, float)) else None


def _fmt(value):
    '''A number rendered compactly: ints stay int, floats to 6 sig figs.'''
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


def _fmt_dist(dist):
    '''A winner->count map rendered as winner:count pairs.'''
    return ", ".join(f"{k}:{v}" for k, v in dist.items())


def _fmt_vec(vec):
    '''A weight vector rendered compactly, e.g. (0.3, 0.7).'''
    return "(" + ", ".join(_fmt(x) for x in vec) + ")"


def load_bundle(run_dir):
    '''Convenience: read comparison.json from a run directory.'''
    with open(os.path.join(run_dir, "comparison.json")) as handle:
        return json.load(handle)