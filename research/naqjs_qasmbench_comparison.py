'''
Tags: Research

naqjs_qasmbench_comparison — benchmark NAQJS against DevQ's default Packing
scheduler, and sweep NAQJS's weight space, on the QASMBench small-scale suite
run across four simulated IBM devices.

This is the QASMBench counterpart to naqjs_comparison.py. Where that runs the
tiny hand-built naqjs.json workload (one devq.simulated device, three toy
jobs), this runs the vendored QASMBench `small/` circuits (research/workloads/
qasmbench_small.json) across four IBM fake backends (Nairobi, Lagos, Jakarta,
Kolkata), with the SCHEDULER varied between naqjs and packing while the
allocator (noise_graph) and router (noise) are held at their defaults — so
the scheduler is the only variable and any metric difference is attributable
to NAQJS vs Packing, not a confounded allocator/router change. It then:

  1. assembles comparison.json (inter-component: naqjs vs packing on every
     metric),
  2. ranks the two sessions by throughput, queue latency and utilisation,
  3. sweeps NAQJS's three-weight simplex (intra-component: how NAQJS's
     scheduling decisions flip across its width/shots/seq weights) — the
     decision-space first-flip sweep, answered from the one recorded NAQJS
     run, NOT a metric re-run per weight.

Two things differ from the toy comparison and are worth stating up front:

  - ibm.simulated is NOT a DevQ built-in, so it is registered here exactly as
    example.py / run_qasmbench_small.py do it; add_device() would otherwise
    refuse a device from an unregistered provider.
  - The QASMBench jobs specify NO shots, so NAQJS's shots feature falls back
    to a per-plugin assumed value or a neutral tie (see _resolve_shots in
    naqjs_scheduler.py). Set naqjs.default_shots on the devices if you want
    the shots axis to be live rather than tied on this workload.

Run:  python -m research.naqjs_qasmbench_comparison
      python research/naqjs_qasmbench_comparison.py         (also works)

Artifacts land under research/test_results/naqjs_qasmbench_comparison/
(gitignored scratch): the per-session logs, metrics.json, comparison.json,
the sweep result, and a rendered text summary printed to stdout and saved
beside them.

NOTE ON NUMBERS — READ BEFORE QUOTING ANY THROUGHPUT FIGURE. Metrics are
RELATIVE to a pinned simulated calibration snapshot, not live hardware. More
importantly, throughput and turnaround are WALL-CLOCK-derived, and DevQ
guarantees DECISION determinism but NOT completion-order determinism (async
finish order varies with OS thread scheduling). On a workload with little
contention the two schedulers dispatch everything promptly and the wall-clock
metrics are dominated by run-to-run jitter — a single-run ranking of naqjs vs
packing on throughput is NOT trustworthy and should not be read as one
scheduler "winning". The utilisation ratio is stable; the decision-space
SWEEP is deterministic and trustworthy. Treat the ranking block below as a
demonstration of the comparison machinery, not as a performance claim; for a
performance claim, run each scheduler N times and report mean ± noise floor
on a CONTENDED workload (see the deterministic-vs-wall-clock split in
docs/METRICS.md).
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
from research.baselines.naqjs_scheduler import NAQJSScheduler
from providers.ibm.ibm_simulated_provider import IBMSimulatedProvider


WORKLOAD = os.path.join(_HERE, "workloads", "qasmbench_contended.json")
OUT_DIR  = os.path.join(_HERE, "test_results", "naqjs_qasmbench_comparison")

# The scheduler is the only varied axis; allocator and router are pinned to
# their defaults so the comparison isolates NAQJS vs Packing.
SELECT = {
    "scheduler": ["naqjs", "packing"],
    "allocator": ["noise_graph"],
    "router":    ["noise"],
}
REGISTRY_MAP  = {"scheduler": {"naqjs": NAQJSScheduler}}
# ibm.simulated is not a built-in; the QASMBench workload's devices come from
# it, so it must be registered before the matrix builds any device.
REGISTER_PROVIDERS = {"ibm.simulated": IBMSimulatedProvider}

# Metrics to rank on, with the direction that is "better" for each. The ranker
# does not presume which end is good — we say so per metric.
RANKINGS = [
    ("throughput.execution",  True,  "execution throughput jobs/s (higher better)"),
    ("throughput.turnaround", True,  "turnaround throughput jobs/s (higher better)"),
    ("queue_latency.median",  False, "median queue latency (lower better)"),
    ("utilisation.system",    True,  "system utilisation (higher better)"),
]


def _run_matrix():
    '''Run the QASMBench workload across the naqjs/packing scheduler matrix
    and compute metrics. Returns the assembled comparison bundle.'''
    import shutil
    shutil.rmtree(OUT_DIR, ignore_errors=True)

    prev = os.getcwd()
    os.chdir(_REPO_ROOT)             # committed spec paths are repo-root-relative
    try:
        R.run(WORKLOAD, out_dir=OUT_DIR,
              register_providers=REGISTER_PROVIDERS,
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

    out("NAQJS vs Packing on QASMBench (small) — comparison and weight sweep")
    out("=" * 66)
    out("workload: research/workloads/qasmbench_contended.json")
    out("devices : ibm.simulated x4 (Nairobi, Lagos, Jakarta, Kolkata)")
    out("varied  : scheduler (naqjs vs packing); allocator=noise_graph, "
        "router=noise fixed")
    out("numbers are relative to a pinned simulated calibration snapshot, "
        "not live hardware.")
    out("wall-clock throughput/turnaround are run-to-run noisy — see the "
        "module docstring; the SWEEP is the trustworthy part.")
    out()

    bundle = _run_matrix()
    out(f"sessions: {', '.join(sorted(bundle))}")
    out()

    # ── Inter-component: rank naqjs vs packing on each metric ──────────────
    out("Ranking (naqjs vs packing) — DEMONSTRATION, not a performance claim")
    out("-" * 66)
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
    out("-" * 66)
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