'''
Tags: Research

run_real_hardware — a curated proof-run on REAL IBM quantum hardware.

WHAT THIS DEMONSTRATES. DevQ executing end-to-end on physical QPUs: it pulls
live calibration from three real IBM backends, its NoiseRouter picks the best
device per circuit from that real data, executes on the chosen device
honouring the allocator's placement, and measures each result's Hellinger
fidelity against a noiseless ideal. The headline result is "DevQ ran on real
hardware, its routing policy chose devices from real calibration, and here is
the measured fidelity of each choice" — an honest, small, re-runnable result,
not a performance claim.

WHY CURATED AND SMALL. A free IBM plan gives ~10 minutes of QPU time PER
MONTH and a shared world queue that can hold a job for minutes to hours. The
full research/ comparison suite issues ~200+ circuit executions — it would
exhaust a month's budget in one partial run and queue for days. So this runs
SEVEN tiny circuits (2-3 qubits) once each at 512 shots: two hand controls
(Bell, GHZ, whose ideals are textbook) plus five of the smallest QASMBench
circuits. That is ~7 jobs, well under a minute of billed QPU time, leaving
budget to re-run. The full sweep/comparison machinery stays on the simulator
where it was validated; real hardware is a spot-check that the platform runs
on a physical QPU.

WHERE THE IDEAL COMES FROM. A real QPU cannot produce a noiseless ideal
(IBMRealProvider.reference_ideal returns None), so fidelity is computed
against an ideal from a SEPARATE simulated provider held here. The ideal is a
property of the CIRCUIT, not the device — noiseless means backend-independent
— so one IBMSimulatedProvider on any backend produces every ideal. This is
the exact vendor-neutral inversion the provider contract describes; the
run's log carries the real measured counts, and this script joins them to
locally-computed ideals through the shipped, pure fidelity() metric.

CREDENTIALS AND BACKENDS come from the environment, resolved via the spec's
${} placeholders (backend names) and passed to the provider instance (token):

    export IBM_QUANTUM_TOKEN=...           # your IBM Quantum API token
    export IBM_BACKEND_A=ibm_brisbane      # your three open backends
    export IBM_BACKEND_B=ibm_sherbrooke
    export IBM_BACKEND_C=ibm_torino
    python -m research.run_real_hardware

The token never reaches disk: it is passed to the provider instance, not
written into the spec, and the spec logs backend names as ${...} placeholders
(the Phase 5.2 leak-safe mechanism). Add --dry-run to validate wiring and
compute ideals WITHOUT touching hardware or spending QPU time.
'''

import argparse
import json
import os
import sys

_HERE      = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)

from benchmark import runner as R
from benchmark import metrics as M
from providers.ibm.ibm_simulated_provider import IBMSimulatedProvider
from research.providers.ibm_real_provider import IBMRealProvider
from frontends.qasm2.qasm2_frontend import QASM2Frontend
from benchmark.reference import compute_ideals

WORKLOAD = os.path.join(_HERE, "workloads", "real_hardware.json")
OUT_DIR  = os.path.join(_HERE, "results", "real_hardware")

# The curated circuit set, by spec path. Kept here too so --dry-run can
# compute ideals without running the spec.
CURATED = [
    "test_circuits/bell.qasm",
    "test_circuits/ghz.qasm",
    "research/circuits/qasmbench/small/deutsch_n2.qasm",
    "research/circuits/qasmbench/small/iswap_n2.qasm",
    "research/circuits/qasmbench/small/wstate_n3.qasm",
    "research/circuits/qasmbench/small/toffoli_n3.qasm",
    "research/circuits/qasmbench/small/qaoa_n3.qasm",
]


def _require_env(name):
    val = os.environ.get(name)
    if not val:
        sys.exit(
            f"Environment variable {name} is not set.\n"
            f"See the module docstring: this run needs IBM_QUANTUM_TOKEN and "
            f"IBM_BACKEND_A/B/C set to your account's token and three open "
            f"backends."
        )
    return val


def _ideals_for_curated():
    '''
    Compute the noiseless ideal for each curated circuit, keyed by circuit
    hash, using a standalone simulated provider. The ideal is device-
    independent, so any fake backend serves; FakeSherbrooke is arbitrary.
    Returns the {hash: {"ideal":..., "label":...}} map compute_ideals
    produces, with labels filled from the spec paths.
    '''
    sim = IBMSimulatedProvider(seed=0)
    fe  = QASM2Frontend()
    circuits, labels = [], {}
    for path in CURATED:
        cr = fe.parse(os.path.join(_REPO_ROOT, path))
        circuits.append(cr)
        from benchmark.reference import circuit_hash
        labels[circuit_hash(cr)] = path
    ideals = compute_ideals(circuits, sim)
    for chash, data in ideals.items():
        data["label"] = labels.get(chash)
    return ideals


def _reference_records(ideals):
    '''Turn the ideal map into log-shaped reference records the shipped
    fidelity() metric reads, identical to what the runner emits for a
    reference-capable provider.'''
    return [
        {"event": "reference", "circuit_hash": chash,
         "ideal": data["ideal"], "label": data.get("label")}
        for chash, data in ideals.items()
    ]


def _load_log_records(out_dir):
    '''Read the single run's JSONL log back into a list of record dicts.'''
    logs = [f for f in os.listdir(out_dir) if f.endswith(".jsonl")]
    if not logs:
        sys.exit(f"No run log produced in {out_dir}.")
    path = os.path.join(out_dir, logs[0])
    return [json.loads(line) for line in open(path) if line.strip()]


def _routing_summary(records):
    '''
    Per-job "which device won", read from the summary's per_job rows joined
    to the attached-device roster. The NoiseRouter's own per-candidate score
    reasoning is in the route records (explain seam); here we report the
    OUTCOME — device chosen — which is what the proof-run headline needs.
    '''
    summary = [r for r in records if r.get("event") == "summary"][-1]
    roster  = summary["devices_attached"]           # index -> device id
    rows = []
    for pj in summary["per_job"]:
        rows.append({
            "job_id": pj["job_id"],
            "device": roster.get(str(pj["device"]), f"d{pj['device']}"),
            "state" : pj["state"],
            "chash" : pj["circuit_hash"],
        })
    return rows


def run(dry_run=False):
    import shutil
    shutil.rmtree(OUT_DIR, ignore_errors=True)
    os.makedirs(OUT_DIR, exist_ok=True)

    # Ideals first — needed in both modes, and computing them is free (local
    # noiseless simulation), so --dry-run can validate the whole non-hardware
    # half of the pipeline.
    ideals = _ideals_for_curated()
    print(f"Computed {len(ideals)} noiseless ideals for {len(CURATED)} "
          f"curated circuits (device-independent, via a simulated provider).")

    if dry_run:
        print("\n[--dry-run] Skipping hardware submission. Ideals per circuit:")
        for chash, data in ideals.items():
            top = sorted(data["ideal"].items(), key=lambda kv: -kv[1])[:3]
            top_str = ", ".join(f"{k}:{v:.3f}" for k, v in top)
            print(f"  {data.get('label'):55s} -> {top_str}")
        print("\nWiring OK. Set IBM_QUANTUM_TOKEN and IBM_BACKEND_A/B/C, drop "
              "--dry-run to run on hardware.")
        return None

    token = _require_env("IBM_QUANTUM_TOKEN")
    for name in ("IBM_BACKEND_A", "IBM_BACKEND_B", "IBM_BACKEND_C"):
        _require_env(name)

    # Register the provider CLASS — DevQ constructs it and passes each
    # device's credentials in through the spec's resolved backend block
    # (${IBM_QUANTUM_TOKEN} etc.), so the token never reaches the log.
    provider_map = {"ibm.real": IBMRealProvider}

    # Report each backend's real GLOBAL queue depth before running — context
    # for the routing story (the router does NOT see this; it routes on
    # calibration/noise, per its contract). Uses a throwaway authenticated
    # instance since the run's own provider is built internally by the runner.
    print("\nReal backend global queue depths (world queue, informational):")
    probe = IBMRealProvider(secrets={"token": token})
    for envname in ("IBM_BACKEND_A", "IBM_BACKEND_B", "IBM_BACKEND_C"):
        bname = os.environ[envname]
        try:
            pj = probe.pending_jobs(bname)
        except Exception:
            pj = None
        print(f"  {bname:20s} pending_jobs = {pj if pj is not None else 'n/a'}")

    print("\nSubmitting 7 curated circuits — routed across 3 real QPUs by "
          "NoiseRouter.\nEach job BLOCKS on IBM's queue; this can take a "
          "while on a free plan.\n")

    prev = os.getcwd()
    os.chdir(_REPO_ROOT)      # spec circuit paths are repo-root-relative
    try:
        R.run(WORKLOAD, out_dir=OUT_DIR,
              register_providers=provider_map, quiet=False)
    finally:
        os.chdir(prev)

    # Join measured counts (in the log) to locally-computed ideals and run
    # the shipped fidelity metric over the combined records.
    records = _load_log_records(OUT_DIR)
    records += _reference_records(ideals)
    fid = M.fidelity(records)
    routing = _routing_summary(records)

    _report(routing, fid, ideals)
    return {"routing": routing, "fidelity": fid}


def _report(routing, fid, ideals):
    label_by_hash = {h: d.get("label") for h, d in ideals.items()}
    print("\n" + "=" * 72)
    print("DevQ on REAL IBM hardware — routing + measured fidelity")
    print("=" * 72)
    print("Each circuit was routed across 3 physical QPUs by NoiseRouter "
          "(real calibration),")
    print("executed on the chosen device honouring allocator placement, and "
          "scored vs a")
    print("noiseless ideal from a separate simulated provider.\n")

    per_job = fid["per_job"]
    print(f"{'circuit':40s} {'device':10s} {'state':10s} {'Hellinger':>10s}")
    print("-" * 72)
    for row in routing:
        label = label_by_hash.get(row["chash"], row["chash"])
        short = os.path.basename(label) if label else "?"
        h = per_job.get(row["job_id"], {}).get("hellinger")
        hstr = f"{h:.4f}" if h is not None else "  n/a"
        print(f"{short:40s} {row['device']:10s} {row['state']:10s} {hstr:>10s}")

    print("-" * 72)
    dist = fid["hellinger"]
    def f(x): return f"{x:.4f}" if x is not None else "n/a"
    print(f"Hellinger fidelity across jobs: "
          f"median={f(dist['median'])}  mean={f(dist['mean'])}  "
          f"min={f(dist['min'])}  max={f(dist['max'])}")
    print("\nInterpretation: higher is closer to the noiseless ideal. The "
          "hand controls\n(Bell -> 00/11, GHZ -> 000/111, iswap -> 10, "
          "toffoli -> 111) are the sanity\nanchors; a low control fidelity "
          "points at the pipeline, a low benchmark\nfidelity is the real "
          "device's noise.")


def main():
    ap = argparse.ArgumentParser(
        description="Curated proof-run on real IBM hardware.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Validate wiring and compute ideals without touching "
                         "hardware or spending QPU time.")
    args = ap.parse_args()
    run(dry_run=args.dry_run)


if __name__ == "__main__":
    main()