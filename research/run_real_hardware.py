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


def smoke(backend_env="IBM_BACKEND_A"):
    '''
    Submit ONE Bell circuit to ONE real backend and print the raw counts.

    This exercises the single code path that dry-run cannot: the real
    SamplerV2 submission — transpile with optimization_level=0 +
    initial_layout, run on hardware, and read counts back via
    result[0].data.c.get_counts(). That last step rests on the shared
    lowering naming its classical register "c"; a wrong assumption there
    returns empty counts, and it is far cheaper to learn that from one Bell
    job (a few seconds of QPU time) than from a failed 7-circuit run. A
    correct result is ~50/50 on "00" and "11".
    '''
    token = _require_env("IBM_QUANTUM_TOKEN")
    backend_name = _require_env(backend_env)

    fe  = QASM2Frontend()
    cr  = fe.parse(os.path.join(_REPO_ROOT, "test_circuits/bell.qasm"))

    ibm = IBMRealProvider(secrets={"token": token})
    print(f"Smoke test: 1 Bell circuit -> {backend_name} @ 512 shots.")
    print("Building device (pulls real calibration)…")
    device = ibm.get_device(backend_name)

    # on_attach normally runs when the kernel attaches the device; here we
    # drive execute() directly, so give it the index-keyed session by hand.
    device.index = 0
    ibm.on_attach(device)

    # Bell on 2 qubits: identity placement is fine for a smoke test — the
    # point is the submit/counts path, not the allocator.
    v2p_map = {0: 0, 1: 1}

    print("Submitting… (this BLOCKS on IBM's real queue — may take a while)")
    future = ibm.execute(cr, v2p_map, shots=512, device=device)
    result = future.result()

    print("\n" + "=" * 60)
    if not result.success:
        print("SMOKE FAILED — execute returned an error:")
        print(f"  {result.error}")
        print("The submit path is not working; do NOT run the full proof-run "
              "yet.")
        return result

    counts = result.counts
    total  = sum(counts.values()) or 1
    print("SMOKE RESULT — raw counts from real hardware:")
    for bits in sorted(counts, key=lambda b: -counts[b]):
        bar = "#" * round(40 * counts[bits] / total)
        print(f"  {bits}: {counts[bits]:5d}  {bar}")

    # Sanity read: a Bell state should concentrate on 00 and 11.
    good = counts.get("00", 0) + counts.get("11", 0)
    frac = good / total
    width_ok = all(len(b) == 2 for b in counts)
    print(f"\n  '00'+'11' share: {frac:.1%} of shots "
          f"(noiseless is 100%; real hardware loses some to noise).")
    if frac > 0.6 and width_ok:
        print("  Counts are shaped correctly (2-bit strings, mass on the Bell "
              "peaks).\n  The submit/counts path works — the full proof-run "
              "is safe to run.")
    else:
        print("  ⚠ Counts look off (wrong width, or mass not on 00/11). "
              "Inspect before\n  running the full proof-run — this is exactly "
              "what the smoke test is for.")
    return result


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
    provider_map = {"ibm.real": IBMRealProvider, "ibm.simulated": IBMSimulatedProvider}

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

    report_text = _report(routing, fid, ideals)
    _persist_results(OUT_DIR, routing, fid, ideals, report_text)
    return {"routing": routing, "fidelity": fid}


def _persist_results(out_dir, routing, fid, ideals, report_text):
    '''
    Write the interpreted result to disk, not just the terminal. A real run
    costs QPU time and hours of queue waiting, so its RESULT — the routing
    decision and per-circuit fidelity — must be durable and citable, not
    scrollback. Two forms beside the run's raw log/manifest/metrics:

      results.json — machine-readable (routing + fidelity), for the paper.
      results.txt  — the exact human-readable table _report printed.

    The raw measured counts already live in the run's .jsonl log in the same
    directory; this adds the joined-with-ideal interpretation the log does
    not itself carry (a real provider records no ideals — fidelity is joined
    here against the simulated ideal).
    '''
    label_by_hash = {h: d.get("label") for h, d in ideals.items()}
    rows = []
    for r in routing:
        rows.append({
            "job_id"   : r["job_id"],
            "circuit"  : label_by_hash.get(r["chash"], r["chash"]),
            "device"   : r["device"],
            "state"    : r["state"],
            "hellinger": fid["per_job"].get(r["job_id"], {}).get("hellinger"),
            "tvd"      : fid["per_job"].get(r["job_id"], {}).get("tvd"),
        })
    payload = {
        "per_circuit": rows,
        "hellinger"  : fid["hellinger"],
        "tvd"        : fid["tvd"],
    }
    with open(os.path.join(out_dir, "results.json"), "w") as f:
        json.dump(payload, f, indent=2)
    with open(os.path.join(out_dir, "results.txt"), "w") as f:
        f.write(report_text)
    print(f"\nResults written to {out_dir}/ (results.json, results.txt) "
          f"alongside the raw run log.")


def _report(routing, fid, ideals):
    '''Build the human-readable result table, print it, and return the text
    so the caller can persist the exact same rendering to disk.'''
    label_by_hash = {h: d.get("label") for h, d in ideals.items()}
    n_devices = len({r["device"] for r in routing}) or 1
    lines = []
    def out(s=""):
        print(s)
        lines.append(s)

    out("\n" + "=" * 72)
    out("DevQ on REAL IBM hardware — routing + measured fidelity")
    out("=" * 72)
    out(f"Each circuit was routed across {n_devices} physical QPU(s) by "
        f"NoiseRouter (real")
    out("calibration), executed on the chosen device honouring allocator "
        "placement,")
    out("and scored vs a noiseless ideal from a separate simulated "
        "provider.\n")

    per_job = fid["per_job"]
    out(f"{'circuit':40s} {'device':14s} {'state':10s} {'Hellinger':>10s}")
    out("-" * 76)
    for row in routing:
        label = label_by_hash.get(row["chash"], row["chash"])
        short = os.path.basename(label) if label else "?"
        h = per_job.get(row["job_id"], {}).get("hellinger")
        hstr = f"{h:.4f}" if h is not None else "  n/a"
        out(f"{short:40s} {row['device']:14s} {row['state']:10s} {hstr:>10s}")

    out("-" * 76)
    dist = fid["hellinger"]
    def f(x): return f"{x:.4f}" if x is not None else "n/a"
    out(f"Hellinger fidelity across jobs: "
        f"median={f(dist['median'])}  mean={f(dist['mean'])}  "
        f"min={f(dist['min'])}  max={f(dist['max'])}")
    out("\nInterpretation: higher is closer to the noiseless ideal. The "
        "hand controls\n(Bell -> 00/11, GHZ -> 000/111, iswap -> 10, "
        "toffoli -> 111) are the sanity\nanchors; a low control fidelity "
        "points at the pipeline, a low benchmark\nfidelity is the real "
        "device's noise.")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(
        description="Curated proof-run on real IBM hardware.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Validate wiring and compute ideals without touching "
                         "hardware or spending QPU time.")
    ap.add_argument("--smoke", action="store_true",
                    help="Submit ONE Bell circuit to ONE backend "
                         "(IBM_BACKEND_A) and print raw counts — the cheap "
                         "sanity check before the full run.")
    args = ap.parse_args()
    if args.smoke:
        smoke()
    else:
        run(dry_run=args.dry_run)


if __name__ == "__main__":
    main()