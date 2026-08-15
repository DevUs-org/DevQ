# DevQ — A Microkernel & Job Orchestrator for the Quantum World

Copyright 2026 Devjyot Singh Sidhu

DevQ is an open-source quantum execution middleware that applies classical
operating-system abstractions to quantum computing: a microkernel with a
process table, noise-aware qubit allocators, pluggable job schedulers, a
noise-aware device router for distributed execution across multiple
backends, a hardware-agnostic device abstraction, and an interactive
inspection shell.

Quantum platforms today are fragmented, vendor-locked, and opaque — qubit
selection, scheduling, and topology decisions happen inside closed runtimes.
DevQ is the transparent layer beneath them: **Linux for quantum computing**.
It does not compete with Qiskit or Braket; it makes the execution decisions
they hide *inspectable, controllable, and extensible* — type `qerrors` to see
every device's noise map, `qmap <id>` to see exactly which device and physical
qubits your circuit used, and read the source that made that decision.

The entire system initialises in three lines of user code:

```python
from devq import DevQ
from providers.devq.devq_simulated_provider import DevQSimulatedProvider

DevQ(DevQSimulatedProvider().get_device("random", 10)).start()
```

Attaching multiple backends is one chained call per device:

```python
DevQ(config_path="~/devq.config.json") \
    .register_provider("ibm.simulated", IBMSimulatedProvider) \
    .add_device(DevQSimulatedProvider().get_device("random", 7)) \
    .add_device(ibm.get_device("FakeNairobiV2")) \
    .add_device(ibm.get_device("FakeLagosV2"), "~/lagos.config.json") \
    .start()
```

A provider must be registered before a device it built can be attached.
`DevQSimulatedProvider` ships registered; everything else is one line.
Register the **class** — constructing it, with a seed or credentials or
anything else DevQ knows nothing about, stays yours.

`example.py` is a runnable reference session; `run_tests.py` verifies the
whole plugin matrix (`--list` to see the blocks, `-c` to see every
assertion, `-v` for full session transcripts).

> **Working with an AI assistant?** Point it at
> [`AGENTS.md`](AGENTS.md) — task-oriented orientation for using,
> extending, testing and benchmarking DevQ, routing into the reference
> documents in `docs/`.

Devices are indexed `d0..dn` in add order — stable for the session, shown by
`qdevices`, and referenced by `--exec`/`--no-exec` flags and device-scoped
commands. `add_devices([d1, d2, ...])` attaches several at once;
`start()` raises `DevQError` if no device is attached.

### Device names

A device can be given a name, which acts as an **alias for its index** —
never a replacement. `d1` and `nairobi` refer to the same device
everywhere: in `--exec`/`--no-exec` lists and in every device-scoped
command.

```python
DevQ() \
    .add_device(sim_device) \
    .add_devices([(nairobi_device, "nairobi"), (lagos_device, "lagos")]) \
    .start()
```

`add_device(device, config_path, name)` names a device that also needs
its own config file; `add_devices()` takes bare devices, `(device, name)`
tuples, or a mix. Naming is optional and per-device — an unnamed device
is simply referred to by index.

```
qerrors q nairobi          # same as: qerrors q d1
qrun bell.qasm --exec=nairobi,d2
```

Named devices display as `nairobi (d1)`; unnamed ones as `d0`. Names are
case-insensitive, must be unique, and are rejected at attach time if
they are empty, contain whitespace or commas, look like an index
(`d0`, `d7`, ...), or shadow a shell subcommand argument (`q`, `e`, `b`)
— all of which would make a reference ambiguous.

---

## Code Tags

Every source file carries a tag in its module docstring describing its role:

| Tag | Meaning |
|---|---|
| **Main** | Part of the core DevQ abstraction. Hardware-independent; should support most existing quantum infrastructure. |
| **Default** | The default implementation of a pluggable component (NoiseGraphAllocator, PackingScheduler, NoiseRouter). Part of the core distribution; swappable via config. |
| **Alt** | Configurable alternatives to the Default components (Static/Graph allocators, FCFS/SDF schedulers, RoundRobin router) usable for debugging, testing, baselines, and optimisation comparisons. |
| **Provider** | Hardware-provider code: everything that adapts a specific backend or framework to DevQ, including simulated/testing backends. Not part of the core abstraction; grows as more hardware support is added. |
| **Research** | Paper and benchmark tooling that *uses* DevQ but is not part of it — the `research/` package (e.g. the QASMBench fidelity runner). Outside the test suite; its results depend on a pinned calibration snapshot, so it is kept separate from the system under test. |

---

## Architecture

Seven layers, strict separation of concerns — each layer talks only to its
immediate neighbours. Two-level scheduling, the classical cluster pattern:
the **router** decides *which* device a job runs on; each device's local
**scheduler** decides *when* it runs there. The kernel never knows which
provider backs a device; the shell never touches the scheduler directly;
providers know nothing about job IDs or lifecycle states.

```
User layer          qrun · qsubmit · qrunpack · qdevices · QShell CLI
Circuit rep         CircuitRep · QASM parser · get_depth() · [Silq, Q#, Qiskit …]
DevQ kernel         ProcessTable · QCB · federation host (step / futures)
Device router       NoiseRouter (default) · RoundRobin — binds job → device
Device context      per-device: MemoryManager · QubitPool · Scheduler
Qubit allocator     Static · Graph · Noise-Graph (default)
Device abstraction  BaseProvider · QuantumDevice · load_device()
Hardware provider   DevQSimulatedProvider · IBMSimulatedProvider · [Cirq, IonQ …]
```

Every pluggable layer has an enforced contract:
- `BaseProvider` — providers implement exactly `get_device()` + `execute()`
- `BaseAllocator` — allocators implement `allocate(circuit, device, pool, max_qubit_error=None, max_edge_error=None)`; optionally override `feasible()` (default provided) to classify unsatisfiable jobs
- `BaseScheduler` — schedulers implement `schedule()`, returning the jobs processed in a cycle — dispatched (RUNNING) and/or rejected (REJECTED)
- `BaseRouter` — routers implement `select(qcb, candidates)`, choosing among feasible candidate devices; the base class handles device constraints, per-device feasibility, and rejection-reason aggregation

**One circuit, one device.** There are no quantum links between backends, so
a circuit never spans devices. DevQ therefore federates rather than merges:
each attached device keeps its own qubit pool, allocator, and scheduler
instance inside a `DeviceContext` — a node in the cluster — and physical
qubit indices remain local to their device everywhere in the system.

---

## Status

| Phase | | Delivered |
|---|---|---|
| 0 | ✅ | Hardware abstraction — `BaseProvider`, `QuantumDevice`, topology and calibration |
| 1 | ✅ | QCB, process table, QShell |
| 2 | ✅ | Qubit allocation — static, graph, noise-aware |
| 3 | ✅ | Job scheduling — FCFS, SDF, packing |
| 4 | ✅ | Distributed scheduling — device federation, pluggable router |
| 5 | 🔬 | Research benchmarking mode — *in progress* |
| 6 | 🔭 | Interchangeable frontends — Silq, Q#, Qiskit circuits |
| 7 | 🔭 | Expanded provider ecosystem — real IBM hardware, Cirq, IonQ |
| 8 | 💡 | Claims validation — algorithms ship executable claims a reviewer can run |
| 9 | 💡 | Component distribution — a shared index, with claims re-verified on install |

✅ done · 🔬 in progress · 🔭 planned · 💡 idea, not committed

What each phase delivered, and why: [`docs/ROADMAP.md`](docs/ROADMAP.md).

---

## Extending DevQ

Every pluggable part of DevQ — scheduler, allocator, router, provider — is
attached to a `DevQ` instance through the component registry, with no
edits to DevQ core:

```python
devq = DevQ(config_path="my.config.json")
devq.register_scheduler("mine", MyScheduler)
devq.register_allocator("mine", MyAllocator)
devq.register_router("mine",    MyRouter)
devq.register_provider("ionq",  IonQProvider)
devq.start()
```

Registering a component makes its name a legal value of the corresponding
config key immediately — `{"scheduler": "mine"}` — because the set of legal
values is read from the registry rather than from a fixed list. A component
may also declare its own namespaced config keys (`mine.batch_window`), which
then cascade, validate and appear in `qconfig` exactly like core keys; for a
scheduler, allocator, or router, keys whose parameter name matches a
constructor parameter are also injected at construction — the dotted key
becomes the parameter name with the namespace dot rewritten to `___`
(`mine.batch_window` → `mine___batch_window`), see
[`docs/REGISTRY.md`](docs/REGISTRY.md).

Contracts are checked **at registration**, not when the component is
eventually constructed: the ABC, the constructor signature DevQ will call,
the methods the kernel invokes, and any declared configuration. DevQ's own
components register through this same path, so the extension path cannot
rot while the shipped system keeps working.

Full reference — the contract each kind implements and what is optional:
[`docs/EXTENDING.md`](docs/EXTENDING.md); registration, config scopes,
validators and normalisation groups:
[`docs/REGISTRY.md`](docs/REGISTRY.md).

---

## Documentation

This README is an overview. The reference material lives in `docs/`, and
together they are the authoritative description of DevQ.

| Document | Contents |
|---|---|
| [`docs/FEATURE_LIST.md`](docs/FEATURE_LIST.md) | Complete feature inventory (Phase 0 → present), with how each works in the core and how researchers, learners, and quantum developers benefit |
| [`docs/SHELL.md`](docs/SHELL.md) | Every QShell command, and the JobSpec syntax for per-job noise thresholds and device constraints |
| [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md) | The four-level config cascade, key scopes, seeding and reproducibility, and the components DevQ ships with |
| [`docs/REGISTRY.md`](docs/REGISTRY.md) | Registering a plugin — naming your scheduler, allocator, router, provider or frontend, what is validated, and declaring its configuration keys |
| [`docs/EXTENDING.md`](docs/EXTENDING.md) | Building a plugin — the contract each kind implements, what is required, optional, or opt-in, and the `Sweepable` scoring/sweep contract |
| [`docs/EVENT_LOG.md`](docs/EVENT_LOG.md) | What a run emits — the record kinds, running a workload, and the two-clock (`seq` vs `*_at`) timing model |
| [`docs/COST_MODEL.md`](docs/COST_MODEL.md) | Formal statement of the block cost `S` and the router's device score, with notation and worked values |
| [`docs/METRICS.md`](docs/METRICS.md) | Metrics computed from a completed run — throughput, queue latency, utilisation, rejection rate, load imbalance and fidelity, with definitions and the offline/reproducibility rules |
| [`docs/TEST_BLOCKS.md`](docs/TEST_BLOCKS.md) | Sanity test plan — what each block checks and why; run it with `python run_tests.py` |
| [`docs/MUTATION_TESTING.md`](docs/MUTATION_TESTING.md) | Whether those tests would catch a regression — mutants run, results, and the gaps they found |
| [`docs/ROADMAP.md`](docs/ROADMAP.md) | What each development phase delivered, and what the remaining phases are for |
| [`docs/REFERENCES.md`](docs/REFERENCES.md) | External works DevQ cites or builds on — papers, dependencies, benchmark suites, data provenance, and citation keys |
| [`AGENTS.md`](AGENTS.md) | Orientation for AI coding assistants — task-oriented routing into the documents above |

---

## Acknowledgements

The author thanks Prof. Yiming Zeng for guidance throughout CS 580Q:
Quantum Computing and Networks at Binghamton University, and Karan Patil
for his contributions to the baseline scheduling strategies (FCFS and
SDF) in an earlier phase of DevQ.

### Use of AI Tools

Large language models were used as development aids in this work:
Claude Fable 5/ Opus 4.8 (Anthropic) for code generation, debugging,
documentation, and figure preparation; GPT-5.5 (OpenAI) for architecture
refinement and design brainstorming; and Gemini 3.5 Flash (Google) for
literature search. All designs, AI-assisted code, and text were reviewed,
tested, and validated by the author, who takes sole responsibility for the
content and correctness of this project.

## Trademarks & attribution

DevQ is an independent, unaffiliated research project. "IBM", "Qiskit",
"IBM Quantum", and the device and backend names it references (Nairobi,
Lagos, Sherbrooke, Eagle, Heron, Falcon, and others) are trademarks or
product names of their respective owners. DevQ's use of these names is
**nominative** — it accurately identifies the open-source tooling it
builds on and the origin of the calibration data it uses — and does not
imply endorsement, affiliation, or partnership. The provider identifier
`ibm.simulated` names a data source, not a relationship.

The IBM-simulated provider uses historical device snapshots via the
open-source `qiskit-ibm-runtime` fake backends; its noisy results reflect
a **pinned past calibration snapshot**, not live hardware. DevQ depends on
Qiskit, Qiskit Aer, qiskit-ibm-runtime, NetworkX, and the OpenQASM 2.0
language, and draws benchmark circuits from QASMBench. Full citations,
data provenance, and the license-and-trademark obligations to verify
before publication or release are collected in
[`docs/REFERENCES.md`](docs/REFERENCES.md).