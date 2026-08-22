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
(IBMRealProvider.reference_ideal returns None), so the attached ibm.real
devices are not reference-capable. DevQ's three-tier reference path handles
this automatically: with no reference-capable provider attached, it computes
each circuit's ideal from the core statevector engine (tier 2), or — for a
circuit the engine declines (an entangled reset, or > 20 qubits) — from a
registered reference-capable provider (tier 3). This curated set is seven
small, pure circuits the engine handles entirely, so tier 2 supplies every
ideal and tier 3 is not needed or wired here. The runner emits those ideals
as `reference` records straight into the run's log, so this script computes no
ideals itself — it just runs the shipped fidelity() metric over the log, which
already holds both the real measured counts and the ideals.

CREDENTIALS AND BACKENDS come from the environment, resolved via the spec's
${} placeholders (backend names) and passed to the provider instance (token):

    export IBM_QUANTUM_TOKEN=...           # your IBM Quantum API token
    export IBM_BACKEND_A=ibm_brisbane      # your three open backends
    export IBM_BACKEND_B=ibm_sherbrooke
    export IBM_BACKEND_C=ibm_torino
    python -m research.run_real_hardware

The token never reaches disk: it is passed to the provider instance, not
written into the spec, and the spec logs backend names as ${...} placeholders
(the Phase 5.2 leak-safe mechanism).
'''

import argparse
import json
import os
import sys

_HERE      = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)

from benchmark import runner as R
from benchmark import metrics as M
from providers.ibm.ibm_real_provider import IBMRealProvider
from frontends.qasm2.qasm2_frontend import QASM2Frontend

WORKLOAD = os.path.join(_HERE, "workloads", "real_hardware.json")
OUT_DIR  = os.path.join(_HERE, "results", "real_hardware")


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


def _labels_from_records(records):
    '''
    hash -> human label, read from the `reference` records the run emitted.

    The three-tier reference path stamps each reference record with the
    circuit's source-path label, so the log itself carries the hash->label
    map; this pulls it out for the report, replacing the old local ideal
    computation that used to hold the labels.
    '''
    return {r["circuit_hash"]: r.get("label")
            for r in records if r.get("event") == "reference"}


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

    This exercises the real SamplerV2 submission end to end — transpile with
    optimization_level=0 + initial_layout, run on hardware, and read counts
    back via result[0].data.c.get_counts(). That last step rests on the shared
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


def run():
    import shutil
    shutil.rmtree(OUT_DIR, ignore_errors=True)
    os.makedirs(OUT_DIR, exist_ok=True)

    token = _require_env("IBM_QUANTUM_TOKEN")
    for name in ("IBM_BACKEND_A", "IBM_BACKEND_B", "IBM_BACKEND_C"):
        _require_env(name)

    # Register only the real provider. The attached devices run on ibm.real,
    # which is NOT reference-capable (a real QPU has no noiseless ideal), so
    # the runner's three-tier reference path supplies the ideals itself. For
    # this curated set — seven small, pure circuits (no reset, all <= 3
    # qubits) — tier 2, the core statevector engine, computes every ideal, so
    # no reference-capable provider needs to be attached OR registered. That
    # is the whole point: the fake device and its per-job no_exec_on that this
    # run once needed are gone. DevQ emits a `reference` record per distinct
    # circuit into the run's log; no ideal is computed in this file.
    #
    # Tier 3 (a registered reference-capable provider, for a circuit the engine
    # declines — an entangled reset, or > 20 qubits) is intentionally NOT wired
    # here, because nothing in this curated set reaches it. A run that added
    # such a circuit would register ibm.simulated in this map to supply tier 3.
    provider_map = {"ibm.real": IBMRealProvider}

    # Report each backend's real GLOBAL queue depth before running — context
    # for the routing story (the router does NOT see this; it routes on
    # calibration/noise, per its contract). Uses a throwaway authenticated
    # instance since the run's own provider is built internally by the runner.
    print("Real backend global queue depths (world queue, informational):")
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

    # The log already carries the measured counts AND the `reference` records
    # (ideals) the three-tier path emitted, so the shipped fidelity metric
    # runs over the log records directly — no local ideal join.
    records = _load_log_records(OUT_DIR)
    fid = M.fidelity(records)
    routing = _routing_summary(records)

    report_text = _report(routing, fid, records)
    _persist_results(OUT_DIR, routing, fid, records, report_text)
    return {"routing": routing, "fidelity": fid}


def _persist_results(out_dir, routing, fid, records, report_text):
    '''
    Write the interpreted result to disk, not just the terminal. A real run
    costs QPU time and hours of queue waiting, so its RESULT — the routing
    decision and per-circuit fidelity — must be durable and citable, not
    scrollback. Two forms beside the run's raw log/manifest/metrics:

      results.json — machine-readable (routing + fidelity), for the paper.
      results.txt  — the exact human-readable table _report printed.

    The raw measured counts already live in the run's .jsonl log in the same
    directory, along with the `reference` records the three-tier reference
    path emitted; this adds the joined-with-ideal interpretation (fidelity per
    circuit) the log does not itself carry.
    '''
    label_by_hash = _labels_from_records(records)
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


def _report(routing, fid, records):
    '''Build the human-readable result table, print it, and return the text
    so the caller can persist the exact same rendering to disk.'''
    label_by_hash = _labels_from_records(records)
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
    ap.add_argument("--smoke", action="store_true",
                    help="Submit ONE Bell circuit to ONE backend "
                         "(IBM_BACKEND_A) and print raw counts — the cheap "
                         "sanity check before the full run.")
    args = ap.parse_args()
    if args.smoke:
        smoke()
    else:
        run()


if __name__ == "__main__":
    main()