# DevQ — A Microkernel & Job Orchestrator for the Quantum World

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
    .add_device(DevQSimulatedProvider().get_device("random", 7)) \
    .add_device(ibm.get_device("FakeNairobiV2")) \
    .add_device(ibm.get_device("FakeLagosV2"), "~/lagos.config.json") \
    .start()
```

Devices are indexed `d0..dn` in add order — stable for the session, shown by
`qdevices`, and referenced by `--exec`/`--no-exec` flags and device-scoped
commands. `add_devices([d1, d2, ...])` attaches several at once;
`start()` raises `DevQError` if no device is attached.

---

## Code Tags

Every source file carries a tag in its module docstring describing its role:

| Tag | Meaning |
|---|---|
| **Main** | Part of the core DevQ abstraction. Hardware-independent; should support most existing quantum infrastructure. |
| **Default** | The default implementation of a pluggable component (NoiseGraphAllocator, PackingScheduler, NoiseRouter). Part of the core distribution; swappable via config. |
| **Alt** | Configurable alternatives to the Default components (Static/Graph allocators, FCFS/SDF schedulers, RoundRobin router) usable for debugging, testing, baselines, and optimisation comparisons. |
| **Provider** | Hardware-provider code: everything that adapts a specific backend or framework to DevQ, including simulated/testing backends. Not part of the core abstraction; grows as more hardware support is added. |

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

## Development Phases

### ✅ Phase 0 — Hardware Abstraction (done)
`QuantumDevice` (pure data container), `load_device()` validation,
`TopologyGraph` (NetworkX), `BaseProvider` ABC. Two working providers:
- **DevQSimulatedProvider** — pure-Python backend factory, four topologies
  (fully_connected, linear, grid, random), generated error maps, mocked
  execution. Doubles as the reference implementation for provider authors.
- **IBMSimulatedProvider** — wraps Qiskit V2 fake backends (FakeSherbrooke,
  FakeNairobiV2, FakeLagosV2, …) with **real IBM calibration data** extracted
  from the Target API. The native 2-qubit gate is auto-discovered per backend
  (ECR on Eagle/Heron, CX on older Falcon devices), and execution runs on
  AerSimulator with the backend's noise model, honouring the allocator's
  physical qubit mapping via `initial_layout`.

Both providers accept an optional `seed` for reproducible runs — see
[Reproducibility & Seeding](#reproducibility--seeding).

### ✅ Phase 1 — QCB, Process Table & QShell (done)
Quantum Control Block (the quantum PCB): job_id, circuit, v2p_map, state,
device binding, future, result, job-level noise thresholds and device
constraints. Six-state lifecycle: READY → WAITING / REJECTED / RUNNING →
FINISHED / FAILED. READY covers a queued job that has not yet attempted
allocation; WAITING is transient (attempted, blocked on resources,
retried); REJECTED is the umbrella terminal state for any kernel-level
rejection, whatever stage produced it — device constraints excluding
every device, unsatisfiable thresholds on every allowed device, or
allocation classification inside a scheduler. Future-based execution
(`ExecutionFuture` / `AsyncExecutionFuture` behind one `done()`/`result()`
interface). QShell with full inspection command set (see below). Job IDs
are global across all devices.

### ✅ Phase 2 — Qubit Allocation (done)
Three interchangeable allocators behind `BaseAllocator`:
- **StaticAllocator** *(Alt)* — first available block, no topology awareness.
  Baseline; sensible for all-to-all devices (e.g. IonQ).
- **GraphAllocator** *(Alt)* — BFS over the topology graph; guarantees a
  connected subgraph.
- **NoiseGraphAllocator** *(default)* — BFS + weighted cost
  `S = α·Σ(qubit_error) + β·Σ(edge_error)`. α and β are the common-scope
  config keys `qubit_error_weight` / `edge_error_weight` (defaults 0.1 /
  0.9 — two-qubit gate fidelity dominates NISQ noise), so the cost
  balance is tunable per device through the full config cascade.

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

Plus the **configuration cascade** (see Configuration) and the **QShell job
parser** (JobSpec, bracket groups, per-job threshold and device flags —
fully wired end-to-end).

### ✅ Phase 4 — Distributed Scheduling (done)
Distributed execution across heterogeneous quantum backends, following the
classical cluster pattern (DevQ = the node kernel, the router = the cluster
scheduler):

- **DeviceContext** — the federation unit: one per attached device, bundling
  the device, its MemoryManager/QubitPool, its allocator instance, its
  scheduler instance, and its resolved per-device configuration. Per-device
  config is therefore real: d0 can pack with NoiseGraph while d1 runs FCFS
  over Static.
- **Device router** behind `BaseRouter` — a pluggable decision layer
  mirroring the allocator/scheduler contracts:
  - **NoiseRouter** *(default)* — scores every feasible candidate device by
    `w_queue · queue_pressure + w_noise · best_case_cost` (both min-max
    normalised across candidates) and routes to the lowest score. Queue
    pressure = queued + running jobs on the device. Best-case cost dry-runs
    the device's *own configured allocator* on an empty pool clone and
    scores the returned mapping with the S yardstick (α/β = the
    global-scope copy of `qubit_error_weight` / `edge_error_weight` — one
    uniform ruler across all candidates, deliberately not each device's
    own allocator weights, so cross-device scores stay comparable) — the
    score reflects the mapping quality the job would actually receive
    under that device's real policy. Ties break deterministically by lower device index (note:
    with exactly two candidates and equal weights, normalisation makes the
    terms mirror each other, so the index tiebreak frequently decides).
  - **RoundRobinRouter** *(Alt)* — cycles through feasible devices in index
    order; load- and noise-oblivious fairness baseline.
- **Sticky routing** — a job is routed exactly once, at its first scheduling
  cycle, and the binding is recorded on the QCB. Re-routing WAITING jobs to
  less-loaded devices (work migration) is deliberate future work and an open
  research knob.
- **Cross-device rejection semantics** — REJECTED now means unsatisfiable on
  *every device the job is allowed to run on*: the router calls each
  candidate's pool-state-independent `feasible()` and, if none passes,
  aggregates one reason per device. With sticky routing, rejection
  concentrates at the router — post-routing allocation failures classify
  WAITING, since routing already established feasibility on the chosen
  device.
- **Job-level device constraints** — `--exec` (allow-list; no fallback
  outside it) and `--no-exec` (deny-list); see JobSpec below.
- **Truly asynchronous execution** — `AsyncExecutionFuture` wraps a real
  thread-pool future behind the same `done()`/`result()` interface; both
  simulated providers now execute asynchronously, so circuits genuinely run
  concurrently across devices while the kernel keeps routing and scheduling.
  The kernel required no changes to its resolution loop — the future-based
  lifecycle was designed for this from the start. Worker threads only
  compute; all state mutation happens on the shell thread inside the
  kernel's resolution step, so the system needs no locks.

### 🔭 Phase 5 — Research Benchmarking Mode (planned)
A `qbench` command: run circuit workloads through every
router/scheduler/allocator combination and report comparative results. The
goal is for DevQ to serve as an **algorithm evaluation playground** for
quantum scheduling and allocation researchers — write an allocator against
`BaseAllocator` or a router against `BaseRouter`, plug it in, benchmark it
against the built-ins. Open research problems that live at the router
layer: cross-backend shot aggregation, coherence-window scheduling, and
work migration of WAITING jobs.

### 🔭 Phase 6 — Interchangeable Frontends (planned)
Today circuits enter DevQ as OpenQASM files. Phase 6 opens the top of the
stack the same way `BaseProvider` opens the bottom: a frontend adapter
contract that converts any source representation — **Silq, Q#, Qiskit
circuits**, and other quantum languages — into `CircuitRep`, DevQ's
hardware-independent internal format. Frontends need no knowledge of the
kernel, allocators, or schedulers; the existing QASM loader becomes the
reference frontend. Write in the language you prefer, and DevQ handles
routing, allocation, scheduling, and execution identically.

### 🔭 Phase 7 — Expanded Provider Ecosystem (planned)
More hardware providers behind the same two-method `BaseProvider` contract:
- **IBMRealProvider** — live IBM hardware via `QiskitRuntimeService`;
  `get_device()` pulls live calibration data, `execute()` submits to IBM's
  job queue. The `AsyncExecutionFuture` interface naturally absorbs real
  queue wait times.
- **CirqProvider** — Google's Cirq framework and its gate representation.
- **IonQProvider** — trapped-ion hardware with all-to-all connectivity and
  native gates (gpi, gpi2, ms); pairs naturally with the Static allocator,
  since the connectivity constraint is irrelevant.

Together, Phases 6 and 7 make both ends of the stack interchangeable: any
frontend in, any hardware out, with the DevQ kernel unchanged in between.

---

## QShell Command Reference

QShell commands deliberately mirror classical OS tools. Commands marked
`[dN]` take an optional device argument: with it, output covers that device
only; without it, output is sectioned per attached device (a single-device
session simply shows one `d0` section — the format is uniform).

| Command | Classical analogue | Purpose |
|---|---|---|
| `qrun` | — | Priority-execute a **single** job immediately, bypassing the queue |
| `qsubmit` | — | Enqueue one or more jobs without executing |
| `qrunpack` | — | Drain all queues via the router and per-device schedulers |
| `qdevices` | `lscpu` | List attached devices: index, name, provider, qubits, queued/running load |
| `qps` | `ps` | List all jobs with device binding and lifecycle state |
| `qmap <job_id>` | — | Show a job's device and virtual → physical qubit mapping |
| `qmem [dN]` | `free` | Show free `[]` vs allocated `[X]` qubits |
| `qtopology [dN] [q …]` | — | Show device coupling map(s) (qubit filtering requires a device) |
| `qerrors [q\|e\|b] [dN]` | `iostat` | Show qubit errors, edge errors, or both (default `b`) |
| `qconfig [dN]` | — | Show global router policy and each device's scheduler/allocator/shots with the source of every value |
| `!!` | `!!` | Repeat the last command |
| `exit` / Ctrl-D | — | Exit DevQ |

### Examples

```
devq> qdevices

  d0   random_backend       DevQSimulatedProvider     7 qubits   queued: 0  running: 0
  d1   fakenairobiv2        IBMSimulatedProvider      7 qubits   queued: 0  running: 0
  d2   fakelagosv2          IBMSimulatedProvider      7 qubits   queued: 0  running: 0

devq> qrun test_circuits/bell.qasm --exec=d1,d2
Job 1 submitted to queue.
[Kernel] Dispatching job 1 → d1 (fakenairobiv2) qubits {0: 1, 1: 2}
[Kernel] Job 1 FINISHED. Counts: {'00': 1007, '11': 989, '01': 26, '10': 26}
[+] Job 1 FINISHED.

devq> qrun test_circuits/bell.qasm --max-qubit-error=0.03 --exec=d2
Job 2 submitted to queue.
[x] Job 2 REJECTED: unsatisfiable on every allowed device — d2: no connected
    block of 2 qubits exists on this device under max_qubit_error=0.03,
    max_edge_error=None

devq> qps
1 | d1  | FINISHED
2 | -   | REJECTED

devq> qmap 1

Job 1 mapping

device: d1 (fakenairobiv2)

virtual → physical

  0 → 1
  1 → 2

devq> qerrors e d1

  d1 (fakenairobiv2):

  Edge Error Map:

    (0, 1) -> 0.0086
    (1, 2) -> 0.0070
    ...
```

`qrun` vs `qsubmit`/`qrunpack`: `qrun` is a priority path — it routes and
attempts allocation immediately, executes, blocks until its own result
resolves, and leaves all queued jobs untouched. If allocation fails but the
job is feasible on its routed device, it stays WAITING in that device's
queue for a later `qrunpack`; if it is unsatisfiable everywhere allowed, it
is REJECTED. `qrun` accepts exactly one job (all flags, including
`--exec`/`--no-exec`, are supported).

---

## JobSpec: Job-Level Noise Thresholds & Device Constraints

`qrun` and `qsubmit` arguments are parsed into **JobSpec** objects:

```python
JobSpec(file_path, max_qubit_error=None, max_edge_error=None,
        exec_on=None, no_exec_on=None)
```

**Noise thresholds** are **hard constraints**: any qubit whose readout error
exceeds `max_qubit_error`, or edge whose gate error exceeds
`max_edge_error`, is excluded from allocation for that job. `None` means no
filtering on that dimension. Thresholds are **job-level only** — a
deliberate design decision. Error filtering is a per-job user intent, not a
platform property, so it is expressed at submission time; bracket groups
cover applying one threshold across many jobs.
(StaticAllocator applies the qubit threshold only — it has no topology
concept, so the edge threshold is ignored there by design.)

**Device constraints** bind jobs to devices:
- `--exec=d0,d2` — allow-list: the job may **only** run on the listed
  devices. If it is infeasible on all of them, it is REJECTED — never
  re-routed elsewhere.
- `--no-exec=d1` — deny-list: the job is never routed to the listed devices.
- The two flags are mutually exclusive on the same job or group (an
  allow-list already implies exclusion of every other device).
- Device lists are comma-separated without brackets (brackets are reserved
  for job grouping). Device *existence* is validated at submission —
  referencing a device that is not attached rejects the whole batch.

If constraints or filtering make allocation *temporarily* impossible on the
routed device (resources busy), the job is set WAITING and retried. If they
make allocation *permanently* impossible on every allowed device, the job is
REJECTED with one router-aggregated reason per candidate device — detected
via each device's allocator `feasible()` check, which deliberately ignores
pool state.

### Syntax

```
# Bare jobs — no thresholds, any device
qsubmit bell.qasm
qsubmit bell.qasm ghz.qasm

# Trailing flags — bind ONLY to the job immediately before them
qsubmit bell.qasm --max-qubit-error=0.05
qsubmit bell.qasm --max-edge-error=0.1 --no-exec=d0
qsubmit bell.qasm --exec=d1,d2

# Bracket group — flags apply to ALL jobs in the group
qsubmit [a.qasm b.qasm --max-qubit-error=0.05 --no-exec=d0]
qsubmit [a.qasm b.qasm]                          # valid: group, no flags

# Mixed — groups and bare jobs combine; flags never leak across
qsubmit [a.qasm b.qasm --max-qubit-error=0.05] c.qasm d.qasm --exec=d2 e.qasm
#   a: qe=0.05 | b: qe=0.05 | c: defaults | d: exec=d2 | e: defaults
```

Threshold values must be floats in `[0, 1]`; device references must match
`d<int>`. Malformed input (unclosed brackets, unknown flags, out-of-range
values, flags with no preceding file, bracketed or malformed device lists,
`--exec` with `--no-exec`, references to unattached devices) is rejected
with a clear error and no job is created.

---

## Configuration

Configuration keys are split into three scopes.

**Device keys** (`scheduler`, `allocator`, `shots`) are resolved
independently for every attached device through a four-level cascade,
later levels overriding earlier ones:

1. **DevQ core defaults**
2. **That device's provider `preferred_config()`** (e.g. IBM prefers `shots: 2048`)
3. **Global user config file** — `DevQ(config_path=...)`, applies to all devices
4. **Per-device user config file** — `add_device(device, config_path)`, this device only

**Global keys** (`router`, `router_queue_weight`, `router_noise_weight`)
are resolved once for the whole system: core defaults ← global user file.
Providers deliberately **cannot** set global keys — a provider expressing
system routing policy would be a layer violation; such keys are warned
about and ignored. Like the common keys below, the router weight pair
accepts any non-negative numbers and is normalised to sum to 1 at
resolution time (scaling both weights never changes which device wins).

**Common keys** (`qubit_error_weight`, `edge_error_weight`) are the α / β
of the noise cost `S = α·Σ(qubit_error) + β·Σ(edge_error)` — one concept
with two consumers, so the pair is resolved in **both** scopes. The
global-scope copy feeds the NoiseRouter's scoring yardstick (one uniform
ruler across all candidate devices); each device's copy rides the full
four-level device cascade and feeds that device's allocator. Setting the
weights once in the global file therefore moves the yardstick and every
device's allocator together; a per-device file overrides only that
device's allocator copy. The keys accept any non-negative numbers and
each resolved pair is **normalised to sum to 1** — only the ratio
matters, so `1`/`9`, `0.1`/`0.9`, and `2`/`18` are all equivalent, and
normalising keeps S values on one comparable scale everywhere. Keys
cascade independently, *then* the pair is normalised (overriding only
one of the two mixes it with the other's inherited value). A both-zero
pair falls back to core defaults with a warning. `qconfig` shows the
effective normalised values in both the global section and each device
section. Allocators without a cost model (Static, Graph) ignore the
weights — the same precedent as Static ignoring edge thresholds.

One JSON file may freely mix both scopes; each loader reads only its own
keys:

```json
{
    "scheduler": "packing",
    "allocator": "noise_graph",
    "shots": 1024,
    "qubit_error_weight": 0.1,
    "edge_error_weight": 0.9,
    "router": "noise",
    "router_queue_weight": 0.5,
    "router_noise_weight": 0.5
}
```

`qconfig` shows the provenance of every active value: `DevQ Core`,
`<ProviderName>`, `User (global)`, or `User (dN)`.

Ready-made example config files — including the ones used by the sanity
test blocks (`router_only`, `round_robin`, per-device overrides, weight
variants) — live in `config/config_examples/`.

### Reproducibility & Seeding

Providers accept an optional `seed` at construction for reproducible runs.
It is a **constructor parameter, not a config key** — providers are built
before configuration is resolved and never consume the user config, so a
config key would be a layer violation.

```python
DevQSimulatedProvider(seed=42).get_device("random", 7)   # reproducible topology
IBMSimulatedProvider(seed=42)                            # reproducible counts
```

`seed=None` (the default) preserves fully unseeded behaviour, byte-identical
to a build without the parameter.

**What each provider does with it.** `DevQSimulatedProvider` holds a
provider-local `random.Random(seed)` used for topology and error map
generation, so a generated device is identical across launches. The global
`random` state is never touched — seeding DevQ will not perturb randomness
elsewhere in your program. `IBMSimulatedProvider` derives a per-run seed
`seed + k`, where `k` is a provider-local submission counter incremented on
the shell thread, and passes it to both the transpiler and the Aer
simulator. Because all job dispatch happens on the shell thread, submission
order is deterministic: an identical session replays identical counts
job-for-job, while two runs of the *same* circuit within one session still
produce distinct counts rather than cloned ones.

Providers with no stochastic behaviour inherit `seed` from `BaseProvider`
and ignore it — the same precedent as `StaticAllocator` ignoring cost
weights.

**Scope.** Seeding makes DevQ's *own* randomness reproducible. It does not
pin your dependency versions: fake-backend calibration data is tied to the
`qiskit-ibm-runtime` release, so reproducing counts across machines also
requires matching the pinned stack in `requirements.txt`.

### Scheduler, Allocator & Router Reference

| Config key | Class | Tag | Scope | Behaviour |
|---|---|---|---|---|
| `fcfs` | `FCFSScheduler` | Alt | device | Strict submission order; head-of-line blocking on WAITING jobs (REJECTED jobs are removed, never block) |
| `sdf` | `ShortestDepthScheduler` | Alt | device | Shallowest circuit first; better throughput under mixed-depth workloads |
| `packing` | `PackingScheduler` | **default** | device | SDF + greedy bin-packing (TempPool, two-phase commit); concurrent circuits on disjoint qubits |
| `static` | `StaticAllocator` | Alt | device | First available block; no topology/noise awareness; ignores edge thresholds by design |
| `graph` | `GraphAllocator` | Alt | device | BFS over topology graph; guarantees connected subgraph |
| `noise_graph` | `NoiseGraphAllocator` | **default** | device | BFS + weighted cost S = α·Σ(qubit_err) + β·Σ(edge_err); α/β via the common-scope `qubit_error_weight`/`edge_error_weight` (defaults 0.1/0.9, normalised to sum 1) |
| `round_robin` | `RoundRobinRouter` | Alt | global | Cycles through feasible devices in index order; load/noise-oblivious baseline |
| `noise` | `NoiseRouter` | **default** | global | Routes to lowest `w_q·queue_pressure + w_n·best_case_cost` over feasible devices; weights via `router_queue_weight` / `router_noise_weight` (any non-negative numbers, normalised to sum 1, default 0.5 each) |

Because per-device FCFS queues sit below the router, FCFS ordering is
per-device: global submission order is approximately preserved via routing
order — the standard two-level-scheduling tradeoff.

---

## Extending DevQ

**New provider** — subclass `BaseProvider`, implement `get_device()` and
`execute(circuit, v2p_map, shots, device)`. Return either a synchronous
`ExecutionFuture` or (preferred) an `AsyncExecutionFuture` via
`circuits.execution_result.submit_async(fn)` — the kernel polls
`done()`/`result()` and never knows the difference. No knowledge of the
kernel, allocators, schedulers, or routers required;
`DevQSimulatedProvider` is the reference template.

Two contract points matter for correctness. First, **one provider instance
may serve many devices** (`ibm.get_device("FakeNairobiV2")` and
`ibm.get_device("FakeLagosV2")` on the same object), so any per-device
state — backend handles, noise models, sessions — must be keyed by device
name, never stored flat on the instance; `execute()` receives the
`QuantumDevice` precisely so it can look that state up. Second, `v2p_map`
is the allocator's placement decision and **must be applied at execution**,
not ignored: `IBMSimulatedProvider` translates it into a transpiler
`initial_layout` so virtual qubit `v` runs on physical qubit `v2p_map[v]`.
A provider that drops it silently erases the allocator's effect on
fidelity.

If your provider is stochastic, accept `seed=None` in `__init__`, call
`super().__init__(seed)`, and derive all randomness from a provider-local
generator — see [Reproducibility & Seeding](#reproducibility--seeding).

**New allocator** — subclass `BaseAllocator`, implement `allocate()` per the
documented contract (reserve via `pool.allocate()` on success; raise on
failure; honour thresholds as hard constraints). Every allocator is
constructed with the device's resolved cost weights
(`self.qubit_error_weight` / `self.edge_error_weight`, normalised to sum
to 1) — use them for cost scoring or ignore them freely. Optionally override
`feasible(circuit, device, max_qubit_error, max_edge_error) → None | reason`
— the base default checks eligible-qubit count; override it if your
allocator has stricter existence requirements (see the graph allocators'
connected-block check). `feasible()` powers both scheduler-level
classification and router-level candidate filtering.

**New scheduler** — subclass `BaseScheduler`, implement `schedule()`.

**New router** — subclass `BaseRouter`, implement
`select(qcb, candidates) → DeviceContext`. Candidates arrive already
filtered by the job's device constraints and per-device feasibility; the
base class handles rejection-reason aggregation. Keep `select()`
deterministic (break ties by lower device index). `RoundRobinRouter` is the
minimal reference; `NoiseRouter` shows how to reuse the per-device allocator
machinery for scoring.

---

## Acknowledgements

The author thanks Prof. Yiming Zeng for guidance throughout CS 580Q:
Quantum Computing and Networks at Binghamton University, and Karan Patil
for his contributions to the baseline scheduling strategies (FCFS and
SDF) in an earlier phase of DevQ.

### Use of AI Tools

Large language models were used as development aids in this work:
Claude Fable 5 (Anthropic) for code generation, debugging, and figure
preparation; GPT-5.5 (OpenAI) for architecture refinement and design
brainstorming; and Gemini 3.5 Flash (Google) for literature search.
All designs, AI-assisted code, and text were reviewed, tested, and
validated by the author, who takes sole responsibility for the content
and correctness of this project.