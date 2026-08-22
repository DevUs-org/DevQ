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
import tempfile

from benchmark import runner as R
from benchmark import metrics as M
from plugins.providers.ibm.ibm_simulated_provider import IBMSimulatedProvider

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


def _label_matches(circuit_path, only):
    '''True if a spec job's circuit path matches the --circuit label.

    `only` is a bare label (qft_n4) or a filename (qft_n4.qasm); a job's
    circuit is a repo-root-relative path. Match on basename, tolerating a
    missing .qasm extension on the label.
    '''
    base = os.path.basename(circuit_path)
    want = only if only.endswith(".qasm") else only + ".qasm"
    return base == want


def _single_circuit_spec(only):
    '''Write a temp spec identical to SPEC but with jobs filtered to the one
    circuit named by `only`, and return its path. Raises SystemExit if the
    label matches no job — so a typo fails loudly instead of silently
    running nothing (or, as before, everything).

    Filtering happens on the RAW spec JSON, before load_spec resolves
    ${ENV} placeholders, so device/secret/config resolution is unchanged;
    only the jobs list is narrowed. This makes --circuit actually execute
    one circuit rather than run the full spec and hide the rest at report
    time.
    '''
    with open(SPEC) as fh:
        raw = json.load(fh)

    jobs = [j for j in raw.get("jobs", [])
            if _label_matches(j.get("circuit", ""), only)]
    if not jobs:
        available = sorted({os.path.basename(j.get("circuit", ""))
                            for j in raw.get("jobs", [])})
        raise SystemExit(
            f"--circuit {only!r} matched no circuit in the spec. "
            f"Nothing was executed. Available: {', '.join(available)}"
        )
    raw["jobs"] = jobs

    fd, path = tempfile.mkstemp(prefix="qasmbench_one_", suffix=".json",
                               dir=_HERE)
    with os.fdopen(fd, "w") as fh:
        json.dump(raw, fh)
    return path


def _run(out_dir, only=None):
    '''Run the spec with ibm.simulated registered; return the JSONL path.

    When `only` is set, run a temp spec filtered to that single circuit so
    ONLY it is executed — not the full spec.
    '''
    # Circuit paths in the spec are repo-root-relative; ensure that is the
    # cwd so they resolve wherever this module was launched from.
    os.chdir(_REPO_ROOT)
    spec_path = SPEC
    tmp_path = None
    if only is not None:
        tmp_path = _single_circuit_spec(only)
        spec_path = tmp_path
    try:
        R.run(
            spec_path,
            out_dir=out_dir,
            register_providers={"ibm.simulated": IBMSimulatedProvider},
            quiet=True,
        )
    finally:
        if tmp_path is not None:
            os.unlink(tmp_path)
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

    summary = [r for r in records if r.get("event") == "summary"][-1]
    devices = summary.get("devices_attached", {})
    rows = {row["job_id"]: row for row in summary["per_job"]}

    # circuit_hash -> readable label. The summary's per_job rows now carry
    # `circuit_label` on EVERY job (basename, already secret-masked), so a
    # circuit shows its name whether it ran, was rejected, or FINISHED with
    # no ideal — the last case previously fell through to a raw hash because
    # it emits neither a `reference` nor a `reject` record. Source from the
    # per_job rows first; fall back to the `reference`/`reject` records for
    # older logs written before per_job carried the label.
    label = {}
    for row in rows.values():
        h, lbl = row.get("circuit_hash"), row.get("circuit_label")
        if h and lbl and h not in label:
            label[h] = lbl
    for r in records:
        if r.get("event") in ("reference", "reject"):
            h = r.get("circuit_hash")
            lbl = r.get("circuit_label") or r.get("label")
            if h and lbl and h not in label:
                label[h] = os.path.basename(str(lbl))

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
        help="run and report ONLY this circuit by label, e.g. qft_n4 "
             "(executes just this circuit, not the full spec)")
    args = parser.parse_args()

    os.makedirs(args.out, exist_ok=True)
    log_path = _run(args.out, only=args.circuit)
    _report(log_path, only=args.circuit)
    print(f"\nlog: {log_path}")


if __name__ == "__main__":
    main()