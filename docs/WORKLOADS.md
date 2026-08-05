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

**Provider credentials — the `secrets` block.** A provider that needs a
credential to run (an API token, an endpoint) receives it through a
top-level `secrets` object, resolved from the environment by the same
`${NAME}` placeholders and delivered to the provider's constructor:

```json
{
  "name": "real_hardware",
  "secrets": { "token": "${IBM_QUANTUM_TOKEN}" },
  "devices": [ { "id": "d0", "provider": "ibm.real",
                 "backend": { "backend_name": "${IBM_BACKEND_A}" } } ],
  "jobs": [ ... ]
}
```

Three things make this safe and general. **DevQ owns resolution:** the
`${...}` values resolve at load like any other placeholder, and a missing
variable fails at load, not three layers down. **DevQ owns leak-safety:**
the resolved secret is delivered only to the constructor and never reaches
disk — the `secrets` block is masked in the logged spec (its keys kept, its
values shown as `***`), the verbatim log keeps the `${NAME}` literal, and
device-build errors never echo resolved values. So the log shows *which*
secrets a run used, never their values. **The provider owns the
vocabulary:** DevQ passes the whole `secrets` dict as one opaque argument
and never inspects the key names, so one provider's `token` is another's
`key` or `endpoint`. A provider opts in by naming a `secrets` parameter in
its constructor (`def __init__(self, seed=None, secrets=None)`) and reading
its own keys out of the dict; a provider that names no such parameter is
constructed exactly as before and never sees it. The `secrets` block is the
one place a credential belongs — never a device `backend` field, never a
config key, both of which are logged in full. See
[`REGISTRY.md`](REGISTRY.md) for why credentials stay off the config
cascade.

**Registering plugin components.** `register_providers` is one of a set —
`run()` accepts a `register_*` map for every registrable kind, so a
research baseline that lives outside core (a plugin scheduler, allocator or
router) is registered into the run the same way its provider is:

```python
from benchmark.runner import run
from research.baselines.qos_router import QOSRouter

run("research/workloads/qos.json",
    register_providers={"ibm.simulated": IBMSimulatedProvider},
    register_routers={"qos": QOSRouter})
```

The full set is `register_providers`, `register_schedulers`,
`register_allocators`, `register_routers`, and `register_frontends`. Each
maps a registered name to the plugin **class** (never an instance — see
[`REGISTRY.md`](REGISTRY.md)), and the name becomes a legal value for that
kind's config key for the duration of the run.

**Selecting which component a run uses.** A spec with `config: null`
routes through the defaults (`noise` router, and so on). To drive a run
through a *specific* named component — the usual case for a baseline
comparison — pass `select=`, a map from kind to a **list** of names to
run:

```python
run("research/workloads/qos.json",
    register_providers={"ibm.simulated": IBMSimulatedProvider},
    register_routers={"qos": QOSRouter},
    select={"router": ["qos"]})
```

`select` names a session (or, with more than one name per kind, the matrix
of sessions) by the registered component names — this is how a comparison
script pins, say, `noise_graph` allocator × `packing` scheduler × the
router under test, and runs the baseline against the default in one
invocation. The value is a list even for a single name.

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
| `qos.json` | The minimal QOS router workload — three `devq.simulated` devices (random / linear / fully_connected) so the router has a genuine which-QPU choice, three toy jobs, selecting the `qos` router via top-level config. The fixture behind `research/test_qos.py`. (Its fidelity-ranked comparison, `research/qos_comparison.py`, and the composition demonstration, `research/qos_composition.py`, both run on `qasmbench_small.json` instead, because fidelity needs the reference-capable `ibm.simulated` provider — and a router needs multiple devices, which the four-device suite provides.) |
| `qasmbench_contended.json` | Ten wide (4–5q) QASMBench jobs on a single 7-qubit device, so pairs cannot co-reside and jobs serialise — dispatch order determines completion. The high-contention half of the comparison-mode validation: it is the workload where scheduling has leverage, the contrast against the low-contention `qasmbench_small.json` run. |



Full schema, seed resolution and the strictness rules:
[`EVENT_LOG.md`](EVENT_LOG.md).