'''
Tags: Research

qos_composition — run the three ported literature baselines together as one
DevQ stack, over the QASMBench small suite, and report the composed session's
metrics.

This is the cross-axis composition demonstration: QOS (router), NAQJS
(scheduler) and Mapomatic (allocator) are three independently-ported baselines,
each faithful to a different published system, and DevQ's independent
pluggability lets them run *together* in one session. This script builds and
runs exactly that single composed session:

    mapomatic__qos__naqjs   — Mapomatic allocator + QOS router + NAQJS scheduler

It is NOT a comparison against a control — it is a demonstration that three
separately-authored baselines compose into one coherent, fidelity-producing
stack with ZERO core edits, the concrete payoff of the "policies that span
more than one component" note in docs/EXTENDING.md. Each baseline occupies a
different DevQ axis, so there is no conflict; the point is that the composed
system runs end to end and yields real metrics, not that it beats anything.

WHY THIS WORKLOAD. Fidelity requires the reference-capable `ibm.simulated`
provider (a noiseless ideal per circuit), so this runs on the committed IBM
fake-backend QASMBench workload (research/workloads/qasmbench_small.json, four
devices), the same workload the QOS and Mapomatic fidelity comparisons use —
NOT the toy `devq.simulated` fixtures NAQJS and Mapomatic ship for their own
axis-isolation runs. Running all three over the fidelity workload is what makes
the composed session's fidelity meaningful; it does mean NAQJS and Mapomatic
are exercised outside their native single-device fixtures, which is expected
for a composition demonstration.

BASELINE CAVEATS carry through unchanged — each baseline keeps the faithfulness
caveats recorded at its own use-site and in docs/REFERENCES.md ([QOS],
[NAQJS], [Mapomatic]). Composing them does not remove or alter any caveat.

Run:  python -m research.qos_composition
      python research/qos_composition.py       (also works)

Artifacts land under research/test_results/qos_composition/ (gitignored
scratch): the session log, metrics.json, and a rendered text summary printed to
stdout and saved beside them.

NOTE ON NUMBERS: metrics are RELATIVE to a pinned simulated calibration
snapshot, not live hardware. This is a composition demonstration, so the
numbers show the composed stack produces coherent results — they are not a
ranking claim.
'''

import os
import sys

# benchmark/ and research/ are sibling top-level packages at the repo root, so
# imports are absolute; ensure the root is on sys.path however this is launched
# (mirrors research/mapomatic_comparison.py and research/naqjs_comparison.py).
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from benchmark import runner as R
from benchmark import comparison as C
from benchmark.metrics import write_metrics
from providers.ibm.ibm_simulated_provider import IBMSimulatedProvider
from research.baselines.qos_router import QOSRouter
from research.baselines.naqjs_scheduler import NAQJSScheduler
from research.baselines.mapomatic_allocator import MapomaticAllocator


WORKLOAD = os.path.join(_HERE, "workloads", "qasmbench_small.json")
OUT_DIR  = os.path.join(_HERE, "test_results", "qos_composition")

# The single composed session: all three axes are research baselines at once.
SELECT = {
    "router":    ["qos"],
    "scheduler": ["naqjs"],
    "allocator": ["mapomatic"],
}

# Fidelity needs the reference-capable IBM provider; all three baselines are
# non-built-ins, registered in Python before the run.
PROVIDER_MAP  = {"ibm.simulated": IBMSimulatedProvider}
ROUTER_MAP    = {"qos": QOSRouter}
SCHEDULER_MAP = {"naqjs": NAQJSScheduler}
ALLOCATOR_MAP = {"mapomatic": MapomaticAllocator}

# Metrics to report for the composed session. There is no second session to
# rank against — this is a demonstration — so each metric is reported as a
# single value with the direction that is "better" noted for context.
METRICS = [
    ("fidelity.hellinger.median", "Hellinger fidelity, median across jobs (higher better)"),
    ("fidelity.hellinger.mean",   "Hellinger fidelity, mean across jobs (higher better)"),
    ("fidelity.hellinger.min",    "Hellinger fidelity, worst job (higher better)"),
    ("fidelity.tvd.median",       "TVD companion, median across jobs (lower better)"),
    ("throughput.execution",      "execution throughput jobs/s (higher better)"),
    ("queue_latency.median",      "median queue latency (lower better)"),
    ("utilisation.system",        "system utilisation (higher better)"),
    ("rejection_rate.rate",       "rejection rate (lower better)"),
]


def _get_metric(metrics, dotted):
    '''Read a dotted metric path (e.g. "fidelity.hellinger.median") out of the
    nested metrics dict, returning None if any segment is absent.'''
    node = metrics
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _run_session():
    '''Run the single composed all-baselines session and return its assembled
    bundle row (config + metrics).'''
    import shutil
    shutil.rmtree(OUT_DIR, ignore_errors=True)

    prev = os.getcwd()
    os.chdir(_REPO_ROOT)             # committed spec paths are repo-root-relative
    try:
        R.run(WORKLOAD, out_dir=OUT_DIR,
              register_providers=PROVIDER_MAP,
              register_routers=ROUTER_MAP,
              register_schedulers=SCHEDULER_MAP,
              register_allocators=ALLOCATOR_MAP,
              select=SELECT, quiet=True)
        write_metrics(OUT_DIR)
    finally:
        os.chdir(prev)

    return C.assemble_matrix(OUT_DIR)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    lines = []

    def out(s=""):
        print(s)
        lines.append(s)

    out("QOS + NAQJS + Mapomatic — cross-axis composition demonstration")
    out("=" * 64)
    out("workload: research/workloads/qasmbench_small.json (4 IBM devices)")
    out("one composed session: mapomatic (allocator) + qos (router) + naqjs "
        "(scheduler)")
    out("three separately-ported baselines running as one stack, ZERO core "
        "edits.")
    out("this is a DEMONSTRATION that they compose — not a ranking against a "
        "control.")
    out("numbers are relative to a pinned simulated calibration snapshot, "
        "not live hardware.")
    out()

    bundle = _run_session()

    # There should be exactly one session; surface a crash loudly.
    crashed = [sid for sid in bundle if bundle[sid].get("metrics") is None]
    if crashed:
        for sid in crashed:
            out(f"  !! composed session {sid} did not produce metrics "
                f"(outcome: {bundle[sid].get('outcome')})")
            tb = bundle[sid].get("traceback")
            if tb:
                out(tb)
        out()
        summary_path = os.path.join(OUT_DIR, "summary.txt")
        with open(summary_path, "w") as f:
            f.write("\n".join(lines) + "\n")
        return 1

    (sid, row), = bundle.items()
    out(f"composed session: {sid}")
    out()

    metrics = row.get("metrics", {})
    out("Composed-session metrics")
    out("-" * 64)
    for dotted, label in METRICS:
        value = _get_metric(metrics, dotted)
        shown = value if value is not None else "(not measured)"
        out(f"    {label}:")
        out(f"        {dotted} = {shown}")
    out()

    out("Reading this. The composed stack ran end to end and produced real "
        "fidelity and")
    out("timing metrics, which is the whole claim: three baselines authored "
        "against three")
    out("different papers occupy three DevQ axes and cooperate in one session "
        "with no core")
    out("edit. Each baseline's faithfulness caveats (docs/REFERENCES.md "
        "[QOS]/[NAQJS]/")
    out("[Mapomatic]) carry through unchanged; composition neither adds nor "
        "removes any.")
    out()

    summary_path = os.path.join(OUT_DIR, "summary.txt")
    with open(summary_path, "w") as f:
        f.write("\n".join(lines) + "\n")
    out(f"artifacts + summary under {OUT_DIR}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())