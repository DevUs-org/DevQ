'''
Tags: Research

mapomatic_comparison — benchmark the Mapomatic allocator against DevQ's
default NoiseGraph allocator on the QASMBench small suite, ranked on
FIDELITY.

This is the Phase 5.6 allocator-baseline payoff artifact, the allocator
analogue of naqjs_comparison. It runs the committed QASMBench workload
(research/workloads/qasmbench_small.json) through the comparison matrix with
the ALLOCATOR varied between `mapomatic` and `noise_graph` while the router
(`noise`) and scheduler (`packing`) are held at their defaults — so the
allocator is the only variable and any metric difference is attributable to
Mapomatic vs NoiseGraph, not a confounded router/scheduler change. The two
sessions are:

    mapomatic__noise__packing     — Mapomatic's product-of-fidelities layout
    noise_graph__noise__packing   — DevQ's additive-weighted-sum layout

WHY FIDELITY, AND WHY THIS WORKLOAD. An allocator's job is choosing WHICH
physical qubits a circuit runs on; two allocators that both place every job
successfully produce near-identical throughput and latency on an
uncontended batch (placement barely moves wall-clock — the noise-domination
finding from Phase 5.5). What placement actually changes is answer quality,
so FIDELITY is the metric that discriminates two allocators, and it is the
headline here; timing metrics are reported alongside but expected to be
near-equal. Fidelity requires a reference-capable provider (a noiseless
ideal per circuit), which is `ibm.simulated` — so this comparison runs on
the IBM fake-backend QASMBench workload, not the toy `devq.simulated` one
the NAQJS scheduler comparison uses (a scheduler comparison turns on timing,
which `devq.simulated` provides). The provider is registered in Python
before the run, exactly as research/run_qasmbench_small.py does it.

NO WEIGHT SWEEP. naqjs_comparison follows its ranking with an intra-component
weight sweep over NAQJS's simplex. Mapomatic has NO weights — its
product-of-fidelities cost is a fixed, parameter-free policy (that is the
whole methodological contrast with NoiseGraph's TUNABLE alpha*Sq + beta*Se).
There is therefore nothing to sweep, and this script honestly omits the
sweep section rather than manufacturing a degenerate one. The comparison is
purely inter-component: the two allocators, ranked on each metric.

Run:  python -m research.mapomatic_comparison
      python research/mapomatic_comparison.py       (also works)

Artifacts land under research/test_results/mapomatic_comparison/ (gitignored
scratch): the per-session logs, metrics.json, comparison.json, and a
rendered text summary printed to stdout and saved beside them.

NOTE ON NUMBERS: metrics are RELATIVE to a pinned simulated calibration
snapshot, not live hardware — valid for comparing the two allocators to each
other on this workload, not as absolute truth. Not beating the baseline is
fine and expected for a tools paper; the point is that the comparison is
correct and reproducible.
'''

import os
import sys

# benchmark/ and research/ are sibling top-level packages at the repo root, so
# imports are absolute; ensure the root is on sys.path however this is launched
# (mirrors research/naqjs_comparison.py and research/run_qasmbench_small.py).
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from benchmark import runner as R
from benchmark import comparison as C
from benchmark import comparison_modes as M
from benchmark.metrics import write_metrics
from plugins.providers.ibm.ibm_simulated_provider import IBMSimulatedProvider
from plugins.allocators.mapomatic.mapomatic_allocator import MapomaticAllocator


WORKLOAD = os.path.join(_HERE, "workloads", "qasmbench_small.json")
OUT_DIR  = os.path.join(_HERE, "test_results", "mapomatic_comparison")

# The allocator is the only varied axis; router and scheduler are pinned to
# their defaults so the comparison isolates Mapomatic vs NoiseGraph.
SELECT = {
    "allocator": ["mapomatic", "noise_graph"],
    "router":    ["noise"],
    "scheduler": ["packing"],
}

# Fidelity needs the reference-capable IBM provider; Mapomatic is the
# non-built-in allocator. Both are registered in Python before the run.
PROVIDER_MAP  = {"ibm.simulated": IBMSimulatedProvider}
ALLOCATOR_MAP = {"mapomatic": MapomaticAllocator}

# Metrics to rank on, with the direction that is "better" for each. FIDELITY
# FIRST — it is what an allocator comparison turns on; the timing metrics
# follow and are expected to be near-equal (placement barely moves wall-clock
# on an uncontended batch). The ranker does not presume which end is good — we
# say so per metric.
RANKINGS = [
    ("fidelity.hellinger.median", True,
     "Hellinger fidelity, median across jobs (higher better)  [HEADLINE]"),
    ("fidelity.hellinger.mean",   True,
     "Hellinger fidelity, mean across jobs (higher better)    [HEADLINE]"),
    ("fidelity.hellinger.min",    True,
     "Hellinger fidelity, worst job (higher better)           [HEADLINE]"),
    ("fidelity.tvd.median",       False,
     "TVD companion, median across jobs (lower better)        [HEADLINE]"),
    ("throughput.execution",      True,
     "execution throughput jobs/s (higher better)             [expected ~equal]"),
    ("queue_latency.median",      False,
     "median queue latency (lower better)                     [expected ~equal]"),
    ("utilisation.system",        True,
     "system utilisation (higher better)                      [expected ~equal]"),
    ("rejection_rate.rate",       False,
     "rejection rate (lower better)                           [expected equal]"),
]


def _alloc_of(session_id):
    '''The allocator label for a session id (session ids are
    "allocator__router__scheduler"; match on the allocator token so the
    output reads mapomatic/noise_graph rather than the raw id).'''
    return "mapomatic" if "mapomatic" in session_id else "noise_graph"


def _run_matrix():
    '''Run the QASMBench workload across the mapomatic/noise_graph allocator
    matrix and compute metrics. Returns the assembled comparison bundle.'''
    import shutil
    shutil.rmtree(OUT_DIR, ignore_errors=True)

    prev = os.getcwd()
    os.chdir(_REPO_ROOT)             # committed spec paths are repo-root-relative
    try:
        R.run(WORKLOAD, out_dir=OUT_DIR,
              register_providers=PROVIDER_MAP,
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

    out("Mapomatic vs NoiseGraph — allocator comparison (fidelity-ranked)")
    out("=" * 64)
    out("workload: research/workloads/qasmbench_small.json "
        "(allocator varied; router=noise, scheduler=packing fixed)")
    out("headline metric is FIDELITY — the quantity an allocator's qubit "
        "choice actually moves.")
    out("numbers are relative to a pinned simulated calibration snapshot, "
        "not live hardware.")
    out()

    bundle = _run_matrix()

    # Surface any crashed session loudly rather than silently ranking one row.
    crashed = [sid for sid in bundle if bundle[sid].get("metrics") is None]
    if crashed:
        for sid in crashed:
            out(f"  !! session {sid} did not produce metrics "
                f"(outcome: {bundle[sid].get('outcome')})")
        out()

    out(f"sessions: {', '.join(sorted(bundle))}")
    out()

    # ── Inter-component: rank mapomatic vs noise_graph on each metric ──────
    out("Ranking (mapomatic vs noise_graph)")
    out("-" * 64)
    for metric, descending, label in RANKINGS:
        ranking = M.rank_sessions(bundle, metric, descending=descending)
        out(f"{label}:")
        for row in ranking["rows"]:
            out(f"    {row['rank']}. {_alloc_of(row['session_id']):12s} "
                f"{metric} = {row['value']}")
        for sid in ranking["missing"]:
            out(f"    -  {_alloc_of(sid):12s} {metric} = (not measured)")
        out()

    # ── Why no weight sweep (recorded, not silently omitted) ──────────────
    out("Intra-component sweep")
    out("-" * 64)
    out("None. Mapomatic's product-of-fidelities cost is parameter-free, so "
        "it exposes no")
    out("weight simplex to sweep — the fixed-vs-tunable contrast with "
        "NoiseGraph is the point,")
    out("not a defect. See docs/REFERENCES.md [Mapomatic].")
    out()

    # ── Save the rendered summary beside the artifacts ────────────────────
    summary_path = os.path.join(OUT_DIR, "summary.txt")
    with open(summary_path, "w") as f:
        f.write("\n".join(lines) + "\n")
    out(f"artifacts + summary under {OUT_DIR}/")
    return 1 if crashed else 0


if __name__ == "__main__":
    sys.exit(main())