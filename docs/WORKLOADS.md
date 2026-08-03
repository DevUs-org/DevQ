# Workload specs

Specs in `benchmark/workloads/` are runnable examples AND test fixtures,
mirroring `config/config_examples/`: the suite executes them, so a
schema change that breaks one fails the tests rather than surfacing
when someone tries to run it.


Runnable examples for `benchmark/runner.py`. A spec describes a
benchmark run as data — devices, jobs, seed, config — so a result can
be traced back to the exact input that produced it.

```bash
# one session
python benchmark/runner.py benchmark/workloads/smoke.json

# every scheduler x allocator x router combination
python benchmark/runner.py benchmark/workloads/smoke.json --matrix

# re-run only what did not finish
python benchmark/runner.py benchmark/workloads/smoke.json --matrix --resume
```

Output lands in `results/<name>_<timestamp>/` — gitignored, so it never
pollutes the repo, and safe to delete whenever you are done with a run.
Each directory holds one JSONL event log per session, a `manifest.json`,
and a `metrics.json` computed from the logs once the run finishes (see
[`METRICS.md`](METRICS.md)). Override the location with `--out`.

### Where the test suite puts things

Two directories, both gitignored, for two different purposes:

| Directory | Written by | Lifetime |
|---|---|---|
| `results/` | you, running `benchmark/runner.py` | kept until you delete it |
| `test_results/` | `run_tests.py`, from the specs above | overwritten every test run |

`test_results/` exists so a run is inspectable after the suite finishes —
open `test_results/smoke/default.jsonl` to see exactly what the runner
produced, or `test_results/smoke/metrics.json` for the computed metrics.
It holds only these shipped specs.

The other 19 sessions the suite runs (`benchmark_runner`'s matrix and its
deliberately crashed session) still go to a temp directory and are
deleted. Keeping those would bury the runs you meant to keep under test
artifacts, and they exist to exercise crash handling rather than to be
read.

| Spec | What it exercises |
|---|---|
| `smoke.json` | Two mock devices, five jobs, `no_exec_on`. No qiskit needed — the fastest way to see a run end to end. |
| `ibm_federation.json` | Two IBM fake backends plus a mock device, `exec_on`, and a threshold tight enough to reject. Requires the qiskit stack, and `ibm` registered — see below. |
| `placeholders.json` | Five jobs whose seed, provider and threshold come from `${NAME}` environment placeholders, resolved at load. Shows the credential-safe spec mechanism; needs the `DEVQ_*` vars set (see the suite's `PLACEHOLDER_ENV`). |
| `rejection.json` | Four jobs, half carrying an impossibly strict `max_qubit_error` so no device is feasible and routing rejects them terminally while the rest complete. The fixture for the rejection-rate metric — a deliberately aggressive threshold sweep, yielding a rejection rate of 0.5. Finishes `completed_with_failures`, which is a result, not a crash. |
| `contention.json` | Twenty-five jobs across two devices under batch arrival, so jobs queue behind one another and their waits spread. The fixture for queue-latency p95 at a realistic job count: with nearest-rank, p95 only falls below max at n ≥ 21, so this is the spec where the two differ. Also a strong load-imbalance case — sticky routing sends the whole batch to one device. |
| `per_job_shots.json` | One mock device, two Bell jobs: the first names `"shots": 333`, the second names none. Shows the per-job shot tier that sits above the config cascade — the first job runs its own count, the second defers to the device-resolved default. The fixture behind the `per_job_shots` test block. |
| `gate_error_filter.json` | One 7-qubit mock device, two Bell jobs, the first carrying `max_1q_gate_error: 0.01`. Exercises the single-qubit-gate-error placement filter (`--max-1q-gate-error`) independently of the readout/edge thresholds. The fixture behind the `max_1q_gate_error_filter` test block. |

`ibm_federation.json` names the `ibm` provider, which is not registered
by default. Register it in Python first — specs reference registered
names and never import by path:

```python
from providers.ibm.ibm_simulated_provider import IBMSimulatedProvider
from benchmark.runner import run

run("benchmark/workloads/ibm_federation.json",
    register_providers={"ibm.simulated": IBMSimulatedProvider})
```

Providers are registered as **classes**, so the runner constructs each
one with the spec's seed — or unseeded if the spec names none. Nothing
pre-existing can hold a competing seed, so there is no conflict to
arbitrate and no override warning. A caller who wants a seed the spec
does not name constructs the provider themselves and attaches its device
with `add_device()` instead.

### Research workloads (`research/workloads/`)

Separate from the shipped `benchmark/workloads/` fixtures above, the
`research/` package carries its own specs for the paper's baseline
comparisons. They are **not** test fixtures — `run_tests.py` never
enumerates `research/`, so they are exercised only by their own research
tooling, and their numbers depend on the pinned calibration snapshot.
They name the `ibm.simulated` provider (not a DevQ built-in), so the
research runners register it the same way shown above.

| Spec | What it exercises |
|---|---|
| `naqjs.json` | The minimal NAQJS scheduler workload — one `devq.simulated` device, three toy jobs with explicit shots. The fixture behind `research/naqjs_comparison.py`. |
| `mapomatic.json` | The minimal Mapomatic allocator workload — one `devq.simulated` device, three toy jobs (2q and 3q) selecting the `mapomatic` allocator. The fixture behind `research/test_mapomatic.py`'s placement block. (Its fidelity-ranked comparison, `research/mapomatic_comparison.py`, runs on `qasmbench_small.json` instead, because fidelity needs the reference-capable `ibm.simulated` provider.) |
| `qasmbench_small.json` | The full QASMBench small suite (43 circuits) across four IBM fake backends. Behind `research/naqjs_qasmbench_comparison.py` and `research/run_qasmbench_small.py`. Jobs specify no shots, so a scored scheduler's shots feature falls back to its plugin default or a neutral tie. |
| `qasmbench_contended.json` | Ten wide (4–5q) QASMBench jobs on a single 7-qubit device, so pairs cannot co-reside and jobs serialise — dispatch order determines completion. The high-contention half of the comparison-mode validation: it is the workload where scheduling has leverage, the contrast against the low-contention `qasmbench_small.json` run. |



Full schema, seed resolution and the strictness rules:
[`EVENT_LOG.md`](EVENT_LOG.md).