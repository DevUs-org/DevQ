# DevQ — A Microkernel & Job Orchestrator for the Quantum World

DevQ is an open-source quantum execution middleware that applies classical
operating-system abstractions to quantum computing: a microkernel with a
process table, noise-aware qubit allocators, pluggable job schedulers, a
hardware-agnostic device abstraction, and an interactive inspection shell.

Quantum platforms today are fragmented, vendor-locked, and opaque — qubit
selection, scheduling, and topology decisions happen inside closed runtimes.
DevQ is the transparent layer beneath them: **Linux for quantum computing**.
It does not compete with Qiskit or Braket; it makes the execution decisions
they hide *inspectable, controllable, and extensible* — type `qerrors` to see
the device noise map, `qmap <id>` to see exactly which physical qubits your
circuit used, and read the source that made that decision.

The entire system initialises in three lines of user code:

```python
from devq import DevQ
from hardware.providers.devq.devq_simulated_provider import DevQSimulatedProvider

DevQ(DevQSimulatedProvider().get_device("random", 10)).start()
```

---

## Code Tags

Every source file carries a tag in its module docstring describing its role:

| Tag | Meaning |
|---|---|
| **Main** | Part of the core DevQ abstraction. Hardware-independent; should support most existing quantum infrastructure. |
| **Provider** | Hardware-provider code. Includes two sub-tags: **Fake** (not part of the DevQ abstraction — uses Qiskit fake backends for simulation and testing) and **Adapter** (code adapting DevQ to specific hardware providers; grows as more hardware support is added). |
| **Alt** | Configurable alternatives to the default components (e.g. Static/Graph allocators, FCFS/SDF schedulers) usable for debugging, testing, baselines, and optimisation comparisons. |

---

## Architecture

Six layers, strict separation of concerns — each layer talks only to its
immediate neighbours. The kernel never knows which provider backs the device;
the shell never touches the scheduler directly; providers know nothing about
job IDs or lifecycle states.

```
User layer          qrun · qsubmit · qrunpack · QShell CLI
Circuit rep         CircuitRep · QASM parser · get_depth() · [Silq, Q#, Qiskit …]
DevQ kernel         ProcessTable · QCB · MemoryManager · Scheduler
Qubit allocator     Static · Graph · Noise-Graph (default)
Device abstraction  BaseProvider · QuantumDevice · load_device()
Hardware provider   DevQSimulatedProvider · IBMSimulatedProvider · [Cirq, IonQ …]
```

Every pluggable layer has an enforced contract:
- `BaseProvider` — providers implement exactly `get_device()` + `execute()`
- `BaseAllocator` — allocators implement `allocate(circuit, device, pool, max_qubit_error=None, max_edge_error=None)`; optionally override `feasible()` (default provided) to classify unsatisfiable jobs
- `BaseScheduler` — schedulers implement `schedule()`, returning the jobs processed in a cycle — dispatched (RUNNING) and/or rejected (REJECTED)

---

## Development Phases

### ✅ Phase 0 — Hardware Abstraction (done)
`QuantumDevice` (pure data container), `load_device()` validation,
`TopologyGraph` (NetworkX), `BaseProvider` ABC. Two working providers:
- **DevQSimulatedProvider** — pure-Python backend factory, four topologies
  (fully_connected, linear, grid, random), generated error maps, mocked
  execution. Doubles as the reference implementation for provider authors.
- **IBMSimulatedProvider** — wraps Qiskit V2 fake backends (FakeSherbrooke,
  FakeNairobiV2, …) with **real IBM calibration data** extracted from the
  Target API. The native 2-qubit gate is auto-discovered per backend
  (ECR on Eagle/Heron, CX on older Falcon devices), and execution runs on
  AerSimulator with the backend's noise model.

### ✅ Phase 1 — QCB, Process Table & QShell (done)
Quantum Control Block (the quantum PCB): job_id, circuit, v2p_map, state,
future, result, job-level noise thresholds. Six-state lifecycle:
READY → WAITING / REJECTED / RUNNING → FINISHED / FAILED. WAITING is
transient (blocked on resources, retried); REJECTED is terminal — no
valid allocation can ever exist on the device under the job's
thresholds, as determined by the active allocator's `feasible()` check
at first allocation failure. Future-based execution (`ExecutionFuture`,
synchronous now, async-ready for Phase 4). QShell with full inspection
command set (see below).

### ✅ Phase 2 — Qubit Allocation (done)
Three interchangeable allocators behind `BaseAllocator`:
- **StaticAllocator** *(Alt)* — first available block, no topology awareness.
  Baseline; sensible for all-to-all devices (e.g. IonQ).
- **GraphAllocator** *(Alt)* — BFS over the topology graph; guarantees a
  connected subgraph.
- **NoiseGraphAllocator** *(default)* — BFS + weighted cost
  `S = α·Σ(qubit_error) + β·Σ(edge_error)`, α=0.1, β=0.9 (two-qubit gate
  fidelity dominates NISQ noise).

All allocators honour **hard noise thresholds**: qubits/edges whose error
exceeds the job's `max_qubit_error` / `max_edge_error` are excluded from
consideration entirely, before cost optimisation. Each allocator also
answers feasibility via `feasible()` — whether a job could *ever* be
allocated on the device under its thresholds, pool state aside. The base
default checks eligible-qubit count (exactly right for Static); the graph
allocators additionally require a connected block among eligible qubits.

### ✅ Phase 3 — Job Scheduling (done)
Three schedulers behind `BaseScheduler`:
- **FCFSScheduler** *(Alt)* — strict submission order; head-of-line blocking
  applies to WAITING (feasible-but-blocked) jobs only — unsatisfiable jobs
  are REJECTED, removed from the queue, and never block it.
- **ShortestDepthScheduler** *(Alt)* — shallowest circuit first.
- **PackingScheduler** *(default)* — SDF + greedy bin-packing via a temporary
  reservation pool (TempPool) with two-phase commit; multiple circuits run
  concurrently on disjoint qubit sets.

All three classify allocation failures through the allocator's
`feasible()` check: transient contention → WAITING (retried);
unsatisfiable on this device → REJECTED (terminal, removed from queue,
reason reported in QShell and recorded on the QCB).

Plus the **config cascade** (DevQ core defaults → provider
`preferred_config()` → user JSON, with provenance shown by `qconfig`) and the
**QShell job parser** (JobSpec, bracket groups, per-job threshold flags —
fully wired end-to-end).

### 🔧 Work in progress
- Verification pass: full QShell test plan over Phases 0–3
  (FakeNairobiV2, deterministic expected mappings, threshold and
  REJECTED/WAITING classification cases).

### 🔭 Phase 4 — Distributed Scheduling (planned)
Network-based distributed execution across heterogeneous quantum backends: a
**Device Registry** maintaining a live pool of backends with calibration data
and queue depths, a noise-aware topology-matching job router, and truly
asynchronous `ExecutionFuture` resolution. The kernel requires no changes —
the future-based lifecycle was designed for this from the start.

### 🔭 Phase 5 — Research Benchmarking Mode (planned)
A `qbench` command: run circuit workloads through every scheduler/allocator
combination and report comparative results. The goal is for DevQ to serve as
an **algorithm evaluation playground** for quantum scheduling and allocation
researchers — write an allocator against `BaseAllocator`, plug it in,
benchmark it against the built-ins.

### 🔭 Phase 6 — Interchangeable Frontends (planned)
Today circuits enter DevQ as OpenQASM files. Phase 6 opens the top of the
stack the same way `BaseProvider` opens the bottom: a frontend adapter
contract that converts any source representation — **Silq, Q#, Qiskit
circuits**, and other quantum languages — into `CircuitRep`, DevQ's
hardware-independent internal format. Frontends need no knowledge of the
kernel, allocators, or schedulers; the existing QASM loader becomes the
reference frontend. Write in the language you prefer, and DevQ handles
allocation, scheduling, and execution identically.

### 🔭 Phase 7 — Expanded Provider Ecosystem (planned)
More hardware providers behind the same two-method `BaseProvider` contract:
- **IBMRealProvider** — live IBM hardware via `QiskitRuntimeService`;
  `get_device()` pulls live calibration data, `execute()` submits to IBM's
  job queue. The `ExecutionFuture` interface naturally absorbs real queue
  wait times.
- **CirqProvider** — Google's Cirq framework and its gate representation.
- **IonQProvider** — trapped-ion hardware with all-to-all connectivity and
  native gates (gpi, gpi2, ms); pairs naturally with the Static allocator,
  since the connectivity constraint is irrelevant.

Together, Phases 6 and 7 make both ends of the stack interchangeable: any
frontend in, any hardware out, with the DevQ kernel unchanged in between.

---

## QShell Command Reference

QShell commands deliberately mirror classical OS tools.

| Command | Classical analogue | Purpose |
|---|---|---|
| `qrun` | — | Priority-execute a **single** job immediately, bypassing the queue |
| `qsubmit` | — | Enqueue one or more jobs without executing |
| `qrunpack` | — | Drain the queue via the configured scheduler |
| `qps` | `ps` | List all jobs with lifecycle state |
| `qmap <job_id>` | — | Show a job's virtual → physical qubit mapping |
| `qmem` | `free` | Show free `[]` vs allocated `[X]` qubits |
| `qtopology [q …]` | — | Show device coupling map (optionally filtered to listed qubits) |
| `qerrors [q\|e\|b]` | `iostat` | Show qubit errors, edge errors, or both (default `b`) |
| `qconfig` | — | Show active scheduler/allocator/shots and the source of each value |
| `!!` | `!!` | Repeat the last command |
| `exit` / Ctrl-D | — | Exit DevQ |

### Examples

```
devq> qsubmit test_circuits/bell.qasm test_circuits/ghz.qasm
Job 1 submitted to queue.
Job 2 submitted to queue.

devq> qrunpack
[Kernel] Dispatching job 1 → qubits {0: 1, 1: 2}
[Kernel] Job 1 FINISHED. Counts: {'00': 1007, '11': 989, '01': 26, '10': 26}
...

devq> qrun test_circuits/bell.qasm --max-edge-error=0.001
Job 3 submitted to queue.
[x] Job 3 REJECTED: no connected block of 2 qubits exists on this device
    under max_qubit_error=None, max_edge_error=0.001

devq> qmap 1

Job 1 mapping

virtual → physical

  0 → 1
  1 → 2

devq> qerrors e

Edge Error Map:

  (0, 1) -> 0.0086
  (1, 2) -> 0.0070
  ...

devq> qmem

  0 []
  1 [X]
  2 [X]
  ...
```

`qrun` vs `qsubmit`/`qrunpack`: `qrun` is a priority path — it attempts
allocation immediately, executes, resolves the result before returning, and
leaves all queued jobs untouched. If allocation fails but the job is
feasible, it stays WAITING in the queue for a later `qrunpack`; if it is
unsatisfiable, it is REJECTED and removed. `qrun` accepts exactly one job.

---

## JobSpec: Job-Level Noise Thresholds

`qrun` and `qsubmit` arguments are parsed into **JobSpec** objects:

```python
JobSpec(file_path, max_qubit_error=None, max_edge_error=None)
```

Thresholds are **hard constraints**: any qubit whose readout error exceeds
`max_qubit_error`, or edge whose gate error exceeds `max_edge_error`, is
excluded from allocation for that job. `None` means no filtering on that
dimension. If filtering makes allocation *temporarily* impossible (resources
busy), the job is set WAITING and retried. If it makes allocation
*permanently* impossible on the loaded device, the job is REJECTED with an
allocator-generated reason and removed from the queue — detected lazily at
first allocation failure via the allocator's `feasible()` check.
(StaticAllocator applies the qubit threshold only — it has no topology
concept, so the edge threshold is ignored there by design.)

Thresholds are **job-level only** — a deliberate design decision. Error
filtering is a per-job user intent, not a platform property, so it is
expressed at submission time; bracket groups (below) cover the case of
applying one threshold across many jobs.

### Syntax

```
# Bare jobs — no thresholds
qsubmit bell.qasm
qsubmit bell.qasm ghz.qasm

# Trailing flags — bind ONLY to the job immediately before them
qsubmit bell.qasm --max-qubit-error=0.05
qsubmit bell.qasm --max-edge-error=0.1
qsubmit bell.qasm --max-qubit-error=0.05 --max-edge-error=0.1

# Bracket group — flags apply to ALL jobs in the group
qsubmit [a.qasm b.qasm --max-qubit-error=0.05]
qsubmit [a.qasm b.qasm]                          # valid: group, no flags

# Mixed — groups and bare jobs combine; flags never leak across
qsubmit [a.qasm b.qasm --max-qubit-error=0.05] c.qasm d.qasm --max-edge-error=0.1 e.qasm
#   a: qe=0.05 | b: qe=0.05 | c: defaults | d: ee=0.1 | e: defaults
```

Flag values must be floats in `[0, 1]`. Malformed input (unclosed brackets,
unknown flags, out-of-range values, flags with no preceding file) is rejected
with a clear error and no job is created.

---

## Configuration

Three-level cascade, later levels override earlier ones:

1. **DevQ core defaults**
2. **Provider `preferred_config()`** (e.g. IBM prefers `shots: 2048`)
3. **User JSON file** passed to `DevQ(device, config_path)`

```json
{
    "scheduler": "packing",
    "allocator": "noise_graph",
    "shots": 1024
}
```

### Scheduler & Allocator Reference

| Config key | Class | Tag | Behaviour |
|---|---|---|---|
| `fcfs` | `FCFSScheduler` | Alt | Strict submission order; head job first; head-of-line blocking on WAITING jobs (REJECTED jobs are removed, never block) |
| `sdf` | `ShortestDepthScheduler` | Alt | Shallowest circuit first; better throughput under mixed-depth workloads |
| `packing` | `PackingScheduler` | **default** | SDF + greedy bin-packing (TempPool, two-phase commit); concurrent circuits on disjoint qubits |
| `static` | `StaticAllocator` | Alt | First available block; no topology/noise awareness; ignores edge thresholds by design |
| `graph` | `GraphAllocator` | Alt | BFS over topology graph; guarantees connected subgraph |
| `noise_graph` | `NoiseGraphAllocator` | **default** | BFS + weighted cost S = α·Σ(qubit_err) + β·Σ(edge_err), α=0.1, β=0.9 |

---

## Extending DevQ

**New provider** — subclass `BaseProvider`, implement `get_device()` and
`execute()`. No knowledge of the kernel, allocators, or schedulers required;
`DevQSimulatedProvider` is the reference template.

**New allocator** — subclass `BaseAllocator`, implement `allocate()` per the
documented contract (reserve via `pool.allocate()` on success; raise on
failure; honour thresholds as hard constraints). Optionally override
`feasible(circuit, device, max_qubit_error, max_edge_error) → None | reason`
— the base default checks eligible-qubit count; override it if your
allocator has stricter existence requirements (see the graph allocators'
connected-block check).

**New scheduler** — subclass `BaseScheduler`, implement `schedule()`.