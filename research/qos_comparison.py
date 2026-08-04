'''
Tags: Research

qos_comparison — benchmark the QOS router against DevQ's default NoiseRouter
on the QASMBench small suite, ranked on FIDELITY, then sweep QOS's weight
space.

This is the Phase 5.6 router-baseline payoff artifact, the router analogue of
naqjs_comparison (scheduler) and mapomatic_comparison (allocator). It runs the
committed QASMBench workload (research/workloads/qasmbench_small.json) through
the comparison matrix with the ROUTER varied between `qos` and `noise` while
the allocator (`noise_graph`) and scheduler (`packing`) are held at their
defaults — so the router is the only variable and any metric difference is
attributable to QOS vs NoiseRouter, not a confounded allocator/scheduler
change. The two sessions are:

    noise_graph__qos__packing     — QOS's Sec. 6 fidelity + Sec. 8 relative-
                                    delta which-QPU policy
    noise_graph__noise__packing   — DevQ's min-max additive queue+noise router

It then sweeps QOS's two-weight (c, beta) space — the intra-component
first-flip sensitivity of the which-QPU decision across the fidelity-priority
and utilisation-priority weights, answered from the one recorded QOS run (NOT
a metric re-run per weight). QOS IS sweepable (unlike Mapomatic, which is
parameter-free), so this script includes the sweep section that
mapomatic_comparison honestly omits.

WHY FIDELITY, AND WHY THIS WORKLOAD. A router's job is choosing WHICH device a
job runs on; on an uncontended batch two routers that both place every job
produce near-identical throughput and latency (routing barely moves wall-clock
— the noise-domination finding from Phase 5.5). What the which-QPU choice
actually changes is answer quality, so FIDELITY is the metric that
discriminates two routers, and it is the headline here; timing metrics are
reported alongside but expected to be near-equal. Fidelity requires a
reference-capable provider (a noiseless ideal per circuit), which is
`ibm.simulated` — so this comparison runs on the IBM fake-backend QASMBench
workload (4 devices, so the which-QPU choice genuinely varies), not the toy
`devq.simulated` one. The provider is registered in Python before the run,
exactly as research/run_qasmbench_small.py does it.

QOS FAITHFULNESS CAVEATS (recorded at the use-site and in
docs/REFERENCES.md [QOS]): the Sec. 6 crosstalk product is dropped (DevQ
carries no crosstalk calibration term), the fidelity estimate is
device-representative rather than per-mapping (DevQ layers placement below
routing), and the Sec. 8 utilisation sign is INVERTED (QOS rewards
utilisation to serve its multi-programmer, which DevQ's router does not have,
so the faithful port spreads load). See research/baselines/qos_router.py.

Run:  python -m research.qos_comparison
      python research/qos_comparison.py       (also works)

Artifacts land under research/test_results/qos_comparison/ (gitignored
scratch): the per-session logs, metrics.json, comparison.json, the sweep
result, and a rendered text summary printed to stdout and saved beside them.

NOTE ON NUMBERS: metrics are RELATIVE to a pinned simulated calibration
snapshot, not live hardware — valid for comparing the two routers to each
other on this workload, not as absolute truth. Not beating the baseline is
fine and expected for a tools paper; the point is that the comparison is
correct and reproducible.
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
from benchmark import comparison_modes as M
from benchmark.metrics import write_metrics
from providers.ibm.ibm_simulated_provider import IBMSimulatedProvider
from research.baselines.qos_router import QOSRouter


WORKLOAD = os.path.join(_HERE, "workloads", "qasmbench_small.json")
OUT_DIR  = os.path.join(_HERE, "test_results", "qos_comparison")

# The router is the only varied axis; allocator and scheduler are pinned to
# their defaults so the comparison isolates QOS vs NoiseRouter.
SELECT = {
    "router":    ["qos", "noise"],
    "allocator": ["noise_graph"],
    "scheduler": ["packing"],
}

# Fidelity needs the reference-capable IBM provider; QOS is the non-built-in
# router. Both are registered in Python before the run.
PROVIDER_MAP = {"ibm.simulated": IBMSimulatedProvider}
REGISTRY_MAP = {"router": {"qos": QOSRouter}}

# Metrics to rank on, with the direction that is "better" for each. FIDELITY
# FIRST — it is what a router comparison turns on; the timing metrics follow
# and are expected to be near-equal (routing barely moves wall-clock on an
# uncontended batch). The ranker does not presume which end is good — we say
# so per metric.
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


def _router_of(session_id):
    '''The router label for a session id (session ids are
    "allocator__router__scheduler"; match on the router token so the output
    reads qos/noise rather than the raw id).'''
    return "qos" if "qos" in session_id else "noise"


def _qos_session(bundle):
    '''The session id of the QOS run in the bundle (the one whose router token
    is qos), for the intra-component sweep.'''
    for sid in bundle:
        if "qos" in sid:
            return sid
    return None


def _run_matrix():
    '''Run the QASMBench workload across the qos/noise router matrix and
    compute metrics. Returns the assembled comparison bundle.'''
    import shutil
    shutil.rmtree(OUT_DIR, ignore_errors=True)

    prev = os.getcwd()
    os.chdir(_REPO_ROOT)             # committed spec paths are repo-root-relative
    try:
        R.run(WORKLOAD, out_dir=OUT_DIR,
              register_providers=PROVIDER_MAP,
              register_routers=REGISTRY_MAP["router"],
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

    out("QOS vs NoiseRouter — router comparison (fidelity-ranked) + weight sweep")
    out("=" * 72)
    out("workload: research/workloads/qasmbench_small.json "
        "(router varied; allocator=noise_graph, scheduler=packing fixed)")
    out("headline metric is FIDELITY — the quantity a router's which-QPU "
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

    # ── Inter-component: rank qos vs noise on each metric ──────────────────
    out("Ranking (qos vs noise)")
    out("-" * 72)
    for metric, descending, label in RANKINGS:
        ranking = M.rank_sessions(bundle, metric, descending=descending)
        out(f"{label}:")
        for row in ranking["rows"]:
            out(f"    {row['rank']}. {_router_of(row['session_id']):8s} "
                f"{metric} = {row['value']}")
        for sid in ranking["missing"]:
            out(f"    -  {_router_of(sid):8s} {metric} = (not measured)")
        out()

    # ── Intra-component: sweep QOS's (c, beta) weight space ────────────────
    out("QOS weight sweep (first-flip sensitivity over the fidelity/util "
        "weights)")
    out("-" * 72)
    qos_sid = _qos_session(bundle)
    if qos_sid is None:
        out("  QOS session missing — cannot sweep.")
    else:
        sweep = C.sweep(OUT_DIR, qos_sid, "router",
                        coarse_m=8, bisect=True, registry_map=REGISTRY_MAP)
        presented = M.present_sweep(sweep)
        out(M.render_text(presented))
    out()

    # ── Save the rendered summary beside the artifacts ────────────────────
    summary_path = os.path.join(OUT_DIR, "summary.txt")
    with open(summary_path, "w") as f:
        f.write("\n".join(lines) + "\n")
    out(f"artifacts + summary under {OUT_DIR}/")
    return 1 if crashed else 0


if __name__ == "__main__":
    sys.exit(main())