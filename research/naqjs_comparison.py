'''
Tags: Research

naqjs_comparison — benchmark NAQJS against DevQ's default Packing scheduler,
and sweep NAQJS's weight space, on the committed workload.

This is the Phase 5.6 payoff artifact: it runs the committed workload
(research/workloads/naqjs.json) through the comparison matrix with the
SCHEDULER varied between naqjs and packing while the allocator (noise_graph)
and router (noise) are held at their defaults — so the scheduler is the only
variable and any metric difference is attributable to NAQJS vs Packing, not a
confounded allocator/router change. It then:

  1. assembles comparison.json (inter-component: naqjs vs packing on every
     metric),
  2. ranks the two sessions by throughput, queue latency and utilisation,
  3. sweeps NAQJS's three-weight simplex (intra-component: how NAQJS's
     scheduling decisions flip across its width/shots/seq weights) — the
     5.5c decision-space first-flip sweep, answered from the one recorded
     NAQJS run, NOT a metric re-run per weight.

Run:  python -m research.naqjs_comparison
      python research/naqjs_comparison.py         (also works)

Artifacts land under research/test_results/naqjs_comparison/ (gitignored
scratch): the per-session logs, metrics.json, comparison.json, the sweep
result, and a rendered text summary printed to stdout and saved beside them.

NOTE ON NUMBERS: metrics are RELATIVE to a pinned simulated calibration
snapshot, not live hardware — valid for comparing the two schedulers to each
other on this workload, not as absolute truth. Not beating the baseline is
fine and expected for a tools paper; the point is that the comparison is
correct and reproducible.
'''

import os
import sys

# benchmark/ and research/ are sibling top-level packages at the repo root, so
# imports are absolute; ensure the root is on sys.path however this is launched
# (mirrors research/run_qasmbench_small.py).
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from benchmark import runner as R
from benchmark import comparison as C
from benchmark import comparison_modes as M
from benchmark.metrics import write_metrics
from plugins.schedulers.naqjs.naqjs_scheduler import NAQJSScheduler


WORKLOAD = os.path.join(_HERE, "workloads", "naqjs.json")
OUT_DIR  = os.path.join(_HERE, "test_results", "naqjs_comparison")

# The scheduler is the only varied axis; allocator and router are pinned to
# their defaults so the comparison isolates NAQJS vs Packing.
SELECT = {
    "scheduler": ["naqjs", "packing"],
    "allocator": ["noise_graph"],
    "router":    ["noise"],
}
REGISTRY_MAP = {"scheduler": {"naqjs": NAQJSScheduler}}

# Metrics to rank on, with the direction that is "better" for each. The ranker
# does not presume which end is good — we say so per metric.
RANKINGS = [
    ("throughput.execution",  True,  "execution throughput jobs/s (higher better)"),
    ("throughput.turnaround", True,  "turnaround throughput jobs/s (higher better)"),
    ("queue_latency.median",  False, "median queue latency (lower better)"),
    ("utilisation.system",    True,  "system utilisation (higher better)"),
]


def _run_matrix():
    '''Run the workload across the naqjs/packing scheduler matrix and compute
    metrics. Returns the assembled comparison bundle.'''
    import shutil
    shutil.rmtree(OUT_DIR, ignore_errors=True)

    prev = os.getcwd()
    os.chdir(_REPO_ROOT)             # committed spec paths are repo-root-relative
    try:
        R.run(WORKLOAD, out_dir=OUT_DIR,
              register_schedulers=REGISTRY_MAP["scheduler"],
              select=SELECT, quiet=True)
        write_metrics(OUT_DIR)
    finally:
        os.chdir(prev)

    return C.assemble_matrix(OUT_DIR)


def _naqjs_session(bundle):
    '''The session id of the NAQJS run in the bundle.'''
    for sid in bundle:
        if "naqjs" in sid:
            return sid
    raise SystemExit("no naqjs session in the matrix — did registration fail?")


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    lines = []

    def out(s=""):
        print(s)
        lines.append(s)

    out("NAQJS vs Packing — comparison and weight sweep")
    out("=" * 48)
    out("workload: research/workloads/naqjs.json "
        "(scheduler varied; allocator=noise_graph, router=noise fixed)")
    out("numbers are relative to a pinned simulated calibration snapshot, "
        "not live hardware.")
    out()

    bundle = _run_matrix()
    out(f"sessions: {', '.join(sorted(bundle))}")
    out()

    # ── Inter-component: rank naqjs vs packing on each metric ──────────────
    out("Ranking (naqjs vs packing)")
    out("-" * 48)
    for metric, descending, label in RANKINGS:
        ranking = M.rank_sessions(bundle, metric, descending=descending)
        out(f"{label}:")
        for row in ranking["rows"]:
            sched = "naqjs" if "naqjs" in row["session_id"] else "packing"
            out(f"    {row['rank']}. {sched:8s}  {metric} = {row['value']}")
        for sid in ranking["missing"]:
            sched = "naqjs" if "naqjs" in sid else "packing"
            out(f"    -  {sched:8s}  {metric} = (not measured)")
        out()

    # ── Intra-component: sweep NAQJS's three-weight simplex ────────────────
    out("NAQJS weight sweep (first-flip sensitivity over the width/shots/seq "
        "simplex)")
    out("-" * 48)
    naqjs_sid = _naqjs_session(bundle)
    sweep = C.sweep(OUT_DIR, naqjs_sid, "scheduler",
                    coarse_m=8, bisect=True, registry_map=REGISTRY_MAP)
    presented = M.present_sweep(sweep)
    out(M.render_text(presented))

    # ── Save the rendered summary beside the artifacts ────────────────────
    summary_path = os.path.join(OUT_DIR, "summary.txt")
    with open(summary_path, "w") as f:
        f.write("\n".join(lines) + "\n")
    out(f"artifacts + summary under {OUT_DIR}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())