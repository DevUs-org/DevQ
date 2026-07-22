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

### 💡 Phase 8 — Claims Validation Framework (idea, gated on Phase 5)
A published scheduling or allocation result is currently prose: *"our
approach reduces two-qubit gate error by 23%."* Nobody can check it
without rebuilding the author's harness, which is why the algorithms in
this space have never been compared on equal footing.

Phase 8 would let an algorithm ship **executable claims** alongside its
implementation — a declared workload, baseline, metric and expected
direction, checked by a `devq validate` command that fails when the claim
does not hold:

```python
class QOSAllocator(BaseAllocator):
    CLAIMS = [
        Claim("beats noise_graph on S-cost",
              workload = "ghz_batch",
              baseline = "noise_graph",
              metric   = "mean_S",
              assert_  = "lower",
              margin   = 0.05),
    ]
```

"Reproduce the paper" becomes one command.

**Almost all of this is Phase 5.** Plugging in a competing algorithm is
the component registry (done); running it on a reproducible workload is
5.2; measuring it is 5.3; comparing it against baselines is 5.5. Phase 8
is only the last layer — assert a claim and fail if it is false.

The test suite already does something close to this internally.
[`test_blocks.md`](test_blocks.md) does not merely check that things run;
it records *why each expected value is right*, down to noting that
`S(nairobi{1,2}) = 0.0102` beats the runner-up `{1,3} = 0.0103` by a
margin of 0.0001 and would flip under re-weighting. That is already a
falsifiable claim about an algorithm's behaviour on known hardware. Phase
8 generalises the idea from DevQ's own correctness to anyone else's
result.

**Prerequisite: version-pinned calibration.** A claim is worthless if it
is not reproducible, and the reference values above are tied to
`qiskit-ibm-runtime 0.45.1` — fake-backend calibration data changes with
the runtime version. Calibration snapshots would need pinning as data,
not as a dependency range. A claim whose meaning shifts silently under a
version bump is worse than no claim.

**Gated on Phase 5.6.** A validation framework with no demonstrated wins
to validate is infrastructure looking for a customer. 5.6 — implementing
published baselines as DevQ plugins and measuring against them — is what
makes the framework worth adopting. Phase 8 strengthens the work that
follows the first paper, not the first paper itself.

### 💡 Phase 9 — Component Distribution (idea, gated on adoption)
Once anyone can write a DevQ component, the next question is how a second
researcher gets hold of it. Phase 9 is the shared index that closes the
loop:

```
devq> qget researcher_a/noise_router
  installing devq-router-researcher-a 1.2.0
  registered: router 'researcher_a/noise_router'
  verifying declared claims against your devices...
    ✓ beats noise_router on mean_S (ghz_batch, margin 0.05)
    ✓ reduces queue latency vs round_robin (mixed_batch)
    ✗ FAILED: fidelity improvement — claimed >8%, measured 3.1%
```

This is three separable systems, and only one of them is hard.

**Distribution** is ordinary Python packaging. `pip install
devq-router-researcher-a`, plus entry-point discovery so the component
registers itself. No DevQ-specific infrastructure — `qget` is a thin
wrapper over an index lookup and an install.

**Discovery** is an index: names mapped to packages, versions and
metadata. It can be a JSON file in a repository long before it is a
service.

**Trust** is the real work, and the Docker analogy misleads here. `docker
pull` delivers a sandboxed artifact; `qget` would deliver **arbitrary
Python running in-process with full privileges** — with access to the
credentials of a user who, by construction, has paid quantum hardware
quota. A flat namespace where anyone may claim `qos_scheduler` is a
supply-chain problem, not a naming inconvenience. Namespacing, signing,
provenance, and an explicit "this executes code you have not read" posture
are design questions to settle before the command syntax, not after.

**The composition with Phase 8 is the part worth building.** For a
container image, *does it work* means *does it run*. For a scheduling
algorithm, it means *does it do what the paper claimed* — and Phase 8
gives DevQ the machinery to check exactly that, on the second
researcher's own hardware, against their own devices. A component index
where every entry's published claims are re-verified locally on install
is not a packaging convenience; it is a reproducibility mechanism. It
also softens the trust problem from one direction: you still cannot trust
unread code, but you no longer have to trust the paper's numbers, because
you re-derive them.

**Gated on adoption, not on readiness.** An index containing three
packages is worse than a README listing three repository links: the same
information, plus maintenance and security burden. The order is 5.6
produces results, results attract users, users write components,
components justify an index. Building the index first is
infrastructure-before-demand.

Phases 8 and 9 are ideas rather than plans, recorded because the
reasoning is worth keeping. Where 6 and 7 make both ends of the stack
interchangeable, 8 and 9 would make the *results* portable: a claim
anyone can re-run, and a component anyone can fetch. Both depend on
Phase 5 producing something worth reproducing first.

---