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

Output lands in `results/<name>_<timestamp>/` (gitignored): one JSONL
event log per session plus a `manifest.json`. Override with `--out`.

| Spec | What it exercises |
|---|---|
| `smoke.json` | Two mock devices, five jobs, `no_exec_on`. No qiskit needed — the fastest way to see a run end to end. |
| `ibm_federation.json` | Two IBM fake backends plus a mock device, `exec_on`, and a threshold tight enough to reject. Requires the qiskit stack, and `ibm` registered — see below. |

`ibm_federation.json` names the `ibm` provider, which is not registered
by default. Register it in Python first — specs reference registered
names and never import by path:

```python
from providers.ibm.ibm_simulated_provider import IBMSimulatedProvider
from benchmark.runner import run

run("benchmark/workloads/ibm_federation.json",
    register_providers={"ibm": IBMSimulatedProvider})
```

Registering the CLASS rather than an instance lets the spec's seed
apply. A pre-constructed instance carrying its own seed wins over the
spec and the run warns — see the seed table in `docs/REGISTRY.md`.

Full schema, seed resolution and the strictness rules:
[`REGISTRY.md`](REGISTRY.md).