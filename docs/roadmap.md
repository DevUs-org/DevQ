# DevQ Development Phases

What each phase delivered, and what the remaining phases are for.

Kept out of the README so that the README stays a description of what DevQ
IS rather than a history of how it got here. Phases 0-4 are complete and
this is their record; Phase 5 is in progress; 6 and 7 are planned.

This doubles as the closest thing the project has to a design-decision
log — each completed phase records the abstractions it introduced and why.

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
[Reproducibility & Seeding](configuration.md#reproducibility--seeding).

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

### 🔬 Phase 5 — Research Benchmarking Mode (in progress)
A `qbench` command: run circuit workloads through every
router/scheduler/allocator combination and report comparative results. The
goal is for DevQ to serve as an **algorithm evaluation playground** for
quantum scheduling and allocation researchers — write an allocator against
`BaseAllocator` or a router against `BaseRouter`, register it with
`devq.register_allocator(...)`, and benchmark it against the built-ins
without touching DevQ core. Open research problems that live at the router
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