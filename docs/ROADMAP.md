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
[Reproducibility & Seeding](CONFIGURATION.md#reproducibility--seeding).

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

Phase 5 is tracked in sub-phases while it is in flight. This breakdown is
a working tracker and will be collapsed back into the single description
above once Phase 5 ships:

- **5.1 — determinism, registry, event log** ✅ seeded per-device
  determinism, the component registry, and the structured event log with
  two-clock timestamps.
- **5.2 — spec and runner** ✅ workload specs, the run directory, and
  `${}` spec placeholders that keep credentials out of logged artifacts.
- **5.3 — metrics layer** ✅ throughput, queue latency, utilisation,
  rejection rate and load imbalance, computed offline from a finished run
  (see [`METRICS.md`](METRICS.md)). This closes 5.3: the metric layer is
  the measuring surface, and the two pieces once tracked under it have
  moved to where they actually belong — **fidelity** to 5.4 (it needs the
  noiseless reference run, which is 5.4 machinery, so it cannot land
  before that suite exists) and the two **comparison modes** that diff or
  sweep to 5.5b (they present cross-session results, so they follow the
  matrix and sweep engine they read from). The *absolute* view — one
  session's own metric bundle — is not a separate mode: it is exactly the
  metric bundle this sub-phase produces, so it shipped here in 5.3. Only
  the inter-component (diff) and intra-component (sweep) modes remain,
  and those are 5.5b.
- **5.4 — noiseless reference run and fidelity** ◐ The reference-run
  machinery and the **fidelity metric** are **done**: a reference-capable
  provider produces each circuit's exact noiseless ideal
  (`BaseProvider.reference_ideal`, implemented by `IBMSimulatedProvider`
  via a density-matrix Aer path), the runner records ideals keyed by
  circuit hash, and `fidelity()` computes Hellinger fidelity (matching
  QOS's Qiskit definition) and TVD per job and per session — offline,
  pure, and covered by the `fidelity` test block and eleven mutation
  tests (see [`METRICS.md`](METRICS.md), [`TEST_BLOCKS.md`](TEST_BLOCKS.md),
  [`MUTATION_TESTING.md`](MUTATION_TESTING.md)). The frontend blocker was
  resolved earlier: the `qasm2` parser keeps gate parameters and both
  providers honour real measurement, so parameterised circuits parse, run,
  and yield the measured-bit distributions fidelity compares. **Remaining
  before 5.5**: assembling the QASMBench workload set and running it
  through the finished metric to gather numbers — a validation activity on
  DevQ's machinery, not a change to it (QASMBench is workload data, cited
  in [`REFERENCES.md`](REFERENCES.md), not a DevQ component).
- **5.5a — comparison matrix and α/β sweep** ✅ the cross-config
  analysis engine. Two parts. **The sweep** answers an α/β weight sweep
  from one recorded run rather than by re-executing every job, and it was
  built as a *general* capability rather than a router-only one: the
  `Sweepable` contract (`kernel/sweep.py`) unifies `explain()` (the log's
  score report) and the sweep into one set of hooks a scoring component
  supplies once, inherited by `BaseRouter`, `BaseAllocator` and
  `BaseScheduler` alike. Deciding this seam was general — rather than
  wiring `explain()`/decomposition into the router alone and retrofitting
  the other components later — was deliberate: the same "re-weight the
  recorded raw terms" operation serves any scoring component, and building
  it router-shaped would have meant tearing it up at the first allocator.
  `NoiseRouter` and `NoiseGraphAllocator` sit on the contract, each logging
  the α/β-free cost decomposition (`route` and the new `allocate` event)
  and sweepable end-to-end from a recorded log with a faithfulness anchor;
  the scheduler base carries the contract at router-parity though shipped
  schedulers have no scoring parameter yet and report not-sweepable
  honestly (the first scored scheduler is the NAQJS baseline in 5.6 — QOS,
  earlier expected here, resolved to a spatial which-QPU decision and so is
  a *router* baseline, not a scheduler).
  Selectable matrix components (`matrix_configs(select=)` and CLI flags)
  landed here too. **The engine** is `benchmark/comparison.py`:
  `assemble_matrix` bundles every session's config, metrics and sweepable
  axes into `comparison.json` (the inter-component surface), and `sweep`
  re-derives one session's router or allocator decisions across an α/β grid
  from the recorded scores into `sweep_comp.<axis>.json` — grid sampling
  with opt-in bisection to localise flips, guarded by the faithfulness
  anchor and skipping a non-scoring component with a reason. Both artifacts
  are what the 5.5b modes read.
- **5.5b — comparison modes** ✅ the reading surface over 5.5a, two
  modes in `benchmark/comparison_modes.py`: inter-component
  (`rank_sessions` orders the matrix's sessions by a metric) and
  intra-component (`present_sweep` reads out one session's α/β sweep — its
  flips, or a refusal with its reason). Each returns structured data with
  a `render_text` view over it that can write a `.txt`, so the qbench
  shell (5.7) renders the same modes its own way rather than re-deriving
  them. The *absolute* view — one session's own bundle — is not among
  them: it is the 5.3 metric bundle, already shipped, so 5.5b is the two
  genuine comparisons that need 5.5a's engine underneath.
- **5.5c — n-ary weight sweep** ✅ generalises the sweep from the two-term
  α/β grid to a component's full weight group of n terms, over the
  **Scheffé {n, m} simplex-lattice** (`[Scheffe-Mixtures]`). Scale-invariance
  makes the normalised simplex the faithful search space (weights matter
  only up to direction), and the winner surface is piecewise-constant, so
  the sweep enumerates the lattice and localises flips along its **edge
  graph** by bisection (valid only along edges, never interior chords). At
  n=2 the lattice is exactly the historical α/β grid and the edge graph the
  consecutive chain, so the two-term sweep is unchanged — the regression
  anchor. `sweep(coarse_m=…)` replaces the scalar grid; the schema moves to
  weight-vector points (`{point, winner}` primitives, weight-vector flip
  edges). The faithful claim is bounded to **first-flip sensitivity**: replay
  is exact only up to the first decision that reads state a prior decision
  mutated (a load-aware router or pool-depleting allocator couples through
  evolving state), so DevQ treats all components uniformly under that bound
  rather than over-claiming a full trajectory at arbitrary weights. Touches
  `comparison.py`, `comparison_modes.py`, and the `comparison`/
  `comparison_modes` blocks; two mutation survivors (a reversed weight
  mapping, an inverted bisection) sharpened the tests.
- **5.6 — baseline plugins** 🔭 published baselines to compare against;
  this is what turns the platform into a result.
- **5.7 — `qbench` command** 🔭 the shell surface over the metrics layer,
  folding in the comparison modes.
- **5.8 — real hardware** 🔭 gated on credits and access.

### 🚧 Phase 6 — Interchangeable Frontends (foundation landed)
Circuits enter DevQ through a **frontend**: a reader that lowers some
source representation into `CircuitRep`, DevQ's hardware-independent
internal format. Phase 6 opens the top of the stack the same way
`BaseProvider` opens the bottom.

The **contract and a full OpenQASM 2.0 reader now exist**, brought
forward to unblock 5.4. `frontend` is a registrable component kind
alongside providers, routers, schedulers and allocators: subclass
`BaseFrontend`, implement `parse(source) → CircuitRep`, declare
`EXTENSIONS`, register it, and its sources are dispatchable — no core
edit. Unlike the other kinds a frontend is *dispatched per job* by the
source's extension rather than selected by config, so one session can
read several source languages at once; an ambiguous extension is
disambiguated with `--frontend` (shell) or a `"frontend"` spec key. The
built-in `qasm2` ships registered with no third-party dependency, so DevQ
reads `.qasm` out of the box. See [`REGISTRY.md`](REGISTRY.md).

The `qasm2` frontend is a **complete 2.0 parser** (`frontends/qasm2/`): a
real tokenizer, an expression evaluator that keeps gate parameters
(`rx(pi/2)` now carries its angle — the bug that stopped parameterised
QASMBench circuits from running), recursive custom-`gate` inlining with
parameter and qubit substitution, and first-class `measure`/`reset`.
`CircuitRep` is one ordered, op-tagged instruction stream, so a `reset`
keeps its source position relative to the gates around it. Two well-formed
but unsupported constructs — `if (creg==N)` classical control and
mid-circuit measurement (a gate or reset on a qubit after it was measured)
— are DETECTED at the frontend and marked on the circuit
(`unrunnable_reason`), not raised: the circuit still parses and becomes a
job, and the KERNEL rejects that job (REJECTED, with the reason) at
routing time. This keeps every "DevQ will not run this" verdict as one
uniform outcome — a REJECTED job with a reason — rather than a parse
exception that would abort a whole workload over one circuit. Both need
mid-circuit measurement feedback the execution model does not provide, and
running them anyway (silently dropping the condition, or hoisting the
measure) would change the circuit's meaning; rejecting with a reason is
the honest behaviour.

Measure and reset are now **executed**, not just recorded. Both providers
honour a circuit's explicit measures (falling back to measure-all only
when there are none), and `reset` is placed at its source position;
`ibm.simulated` applies a real `reset` mid-circuit. Measurement, however,
is **terminal**: the lowering reads out at the end of the circuit, so a
circuit that measures a qubit and then operates on it again (mid-circuit
measurement) cannot be run faithfully — it is detected and REJECTED (see
above) rather than silently mis-run. Results are reported over the
declared classical register (bitstring width is `num_clbits`, falling back
to `num_qubits` when no `creg` is declared), so a measured bit sits at its
own index and an unmeasured bit reads 0 — the convention a fidelity
comparison needs. Full mid-circuit measurement (and the classical feedback
built on it) is a later execution-model capability, not yet present.

What remains for Phase 6: additional-language frontends — **OpenQASM
3.0, Silq, Q#, Qiskit circuits** — each built against the same
version-agnostic contract. Frontends need no knowledge of the kernel,
allocators, or schedulers; write in the language you prefer, and DevQ
handles routing, allocation, scheduling, and execution identically.

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
[`TEST_BLOCKS.md`](TEST_BLOCKS.md) does not merely check that things run;
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