'''
Tags: Research

run_qasmbench_small.py — QASMBench fidelity validation for the paper.

WHAT THIS IS. A standalone harness that runs the vendored QASMBench
`small/` circuits through DevQ's finished fidelity metric and prints a
per-circuit table plus a session summary. It is PAPER TOOLING: it *uses*
DevQ, it does not test it. That is why it lives here and not as a block in
run_tests.py — run_tests.py verifies DevQ's own behaviour, and a benchmark
sweep whose numbers depend on a pinned calibration snapshot is a different
kind of thing. It is also why everything here lives under the research/
package — spec, vendored circuits, and this runner together — rather than
in benchmark/workloads/, which run_tests.py auto-enumerates as fixtures and
would run on every suite invocation.

WHAT IT VALIDATES ANYWAY. Running real benchmark circuits exercises the
lowering, the reference path, and fidelity end-to-end on distributions the
Bell/GHZ fixtures never produce. Two DevQ bugs surfaced here first — the
silent gate skip (now a raise) and negative float-dust in the reference
ideal (now clamped). Neither was visible to a suite whose circuits stayed
inside clean amplitudes. So while this is not a DevQ test, it is a genuine
integration check, and worth re-running when the lowering or reference
path changes.

HOW TO RUN. This lives in the research/ package and imports DevQ's
top-level packages (benchmark, providers). Run it as a module with -m
from the repo root, so the root is on sys.path and the imports resolve:

    python -m research.run_qasmbench_small                 # full sweep
    python -m research.run_qasmbench_small --circuit qft_n4  # one circuit

Running it as a plain script (python research/run_qasmbench_small.py) will
NOT work: that puts research/ on sys.path instead of the repo root, so
`from benchmark import ...` fails with ModuleNotFoundError. Use -m.

The spec path and the vendored-circuit paths are anchored to this file's
location, so the circuits resolve regardless.

ibm.simulated is registered here exactly as example.py does it — it is not
a DevQ built-in, so add_device() would refuse a device from an
unregistered provider. The provider is seeded so the sweep reproduces.

COST. The fidelity number rests on an EXACT density-matrix reference whose
cost grows with circuit qubit count; the noisy run is a normal shot-based
sim. The small set is chosen to keep a full sweep to a few tens of seconds,
but the two 8-10q circuits dominate. Use --circuit to re-check one without
paying for the rest.

NUMBERS ARE AGAINST A PINNED SNAPSHOT. The noisy side uses FakeXxxV2
calibration tied to the pinned qiskit-ibm-runtime; a version bump changes
every number here. The paper must state that the fidelity is against a
historical calibration snapshot, not live hardware. See docs/REFERENCES.md.
'''

import argparse
import json
import os

from benchmark import runner as R
from benchmark import metrics as M
from providers.ibm.ibm_simulated_provider import IBMSimulatedProvider

# Anchor paths to THIS file, not the caller's working directory. The spec
# lives beside this script under research/workloads/; the circuit paths
# INSIDE the spec are written relative to the repo root (that is where
# load_spec -> frontend.parse opens them from). So we resolve the spec
# absolutely from here, and run with the repo root as the working
# directory, which makes `python -m research.run_qasmbench_small` behave
# identically no matter where it is launched from.
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
SPEC = os.path.join(_HERE, "workloads", "qasmbench_small.json")
SEED = 42


def _run(out_dir):
    '''Run the spec with ibm.simulated registered; return the JSONL path.'''
    # Circuit paths in the spec are repo-root-relative; ensure that is the
    # cwd so they resolve wherever this module was launched from.
    os.chdir(_REPO_ROOT)
    R.run(
        SPEC,
        out_dir=out_dir,
        register_providers={"ibm.simulated": IBMSimulatedProvider},
        quiet=True,
    )
    logs = [f for f in os.listdir(out_dir) if f.endswith(".jsonl")]
    if not logs:
        raise SystemExit(
            f"no JSONL log in {out_dir} — the session likely crashed. "
            f"Check {out_dir} for a .crashed log."
        )
    return os.path.join(out_dir, logs[0])


def _report(log_path, only=None):
    '''Print the per-circuit fidelity table and the session aggregate.'''
    records = [json.loads(line) for line in open(log_path)]

    # circuit_hash -> readable label, from the one reference record per
    # distinct circuit.
    label = {
        r.get("circuit_hash"): os.path.basename(
            str(r.get("circuit_label") or r.get("label") or "?"))
        for r in records if r.get("event") == "reference"
    }

    summary = [r for r in records if r.get("event") == "summary"][-1]
    devices = summary.get("devices_attached", {})
    rows = {row["job_id"]: row for row in summary["per_job"]}

    fid = M.fidelity(records)
    per_job = fid["per_job"]

    print(f"\n{'circuit':28}{'device':10}{'state':11}{'HF':>9}{'TVD':>9}")
    print("-" * 67)
    seen = set()
    for job_id, row in rows.items():
        chash = row["circuit_hash"]
        name = label.get(chash, (chash or "?")[:8])
        if name in seen:
            continue
        seen.add(name)
        if only and name != (only if only.endswith(".qasm") else only + ".qasm"):
            continue
        f = per_job.get(job_id, {})
        hf, tvd = f.get("hellinger"), f.get("tvd")
        dev = devices.get(str(row.get("device")), str(row.get("device")))
        hf_s = f"{hf:.4f}" if isinstance(hf, (int, float)) else str(hf)
        tvd_s = f"{tvd:.4f}" if isinstance(tvd, (int, float)) else str(tvd)
        print(f"{name:28}{str(dev):10}{row['state']:11}{hf_s:>9}{tvd_s:>9}")

    if not only:
        print("\nsession Hellinger distribution:")
        dist = fid["hellinger"]
        if dist and dist.get("median") is not None:
            for k in ("min", "median", "mean", "max", "p95"):
                v = dist.get(k)
                print(f"  {k:8}{v:.4f}" if isinstance(v, (int, float))
                      else f"  {k:8}{v}")
        else:
            print("  (no qualifying jobs — check for rejections or missing ideals)")


def main():
    parser = argparse.ArgumentParser(
        description="Run QASMBench small-scale circuits through DevQ fidelity.")
    parser.add_argument(
        "--out", default="research/results/qasmbench_small",
        help="output directory for the JSONL log (default: results/qasmbench_small)")
    parser.add_argument(
        "--circuit", default=None,
        help="report only this circuit by label, e.g. qft_n4 "
             "(still runs the full spec; use a smaller spec to run just one)")
    args = parser.parse_args()

    os.makedirs(args.out, exist_ok=True)
    log_path = _run(args.out)
    _report(log_path, only=args.circuit)
    print(f"\nlog: {log_path}")


if __name__ == "__main__":
    main()