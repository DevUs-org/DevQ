# DevQ Feature List

A complete inventory of what DevQ provides today — every feature from
Phase 0 (hardware abstraction) through Phase 5.5c (the n-ary weight sweep) —
with, for each area, **what it is**, **how it works in the core**, and how
each of DevQ's three intended consumers benefits:

- 🔬 **Researchers** — people building and evaluating new scheduling,
  allocation, or routing policies who need a fair, reproducible testbed and
  a way to publish defensible comparisons.
- 🎓 **Learners** — students and newcomers using DevQ to *see* how quantum
  job execution actually works, with every hidden decision made inspectable.
- 🛠️ **Quantum developers** — people running real circuits who want
  transparent, controllable, multi-backend execution instead of an opaque
  vendor runtime.

This document is descriptive, not a tutorial. It points into the reference
docs (`docs/`) for the authoritative detail on any feature, and into the
[`ROADMAP.md`](ROADMAP.md) for the design rationale behind each phase.

> **Scope note.** Everything described here is implemented and covered by
> the test suite unless explicitly marked planned. Fidelity and other
> noise-dependent numbers come from a **pinned simulated calibration
> snapshot**, not live hardware — see [`REFERENCES.md`](REFERENCES.md).

---

## The shape of the platform

DevQ is quantum execution middleware that applies classical
operating-system abstractions to quantum computing: a microkernel with a
process table, noise-aware qubit allocators, pluggable job schedulers, a
noise-aware router for distributed execution across multiple backends, a
hardware-agnostic device abstraction, and an interactive inspection shell.
It does not compete with Qiskit or Braket — it is the transparent layer
*beneath* them, making the execution decisions they hide inspectable,
controllable, and extensible.

Seven layers, each talking only to its neighbours, with **two-level
scheduling** (the classical cluster pattern): the **router** decides *which*
device a job runs on; each device's local **scheduler** decides *when* it
runs there.

```
User layer          qrun · qsubmit · qrunpack · qdevices · QShell CLI
Circuit rep         CircuitRep · QASM parser · get_depth()
DevQ kernel         ProcessTable · QCB · federation host (step / futures)
Device router       NoiseRouter (default) · RoundRobin — binds job → device
Device context      per-device: MemoryManager · QubitPool · Scheduler
Qubit allocator     Static · Graph · Noise-Graph (default)
Device abstraction  BaseProvider · QuantumDevice · load_device()
Hardware provider   DevQSimulatedProvider · IBMSimulatedProvider
```

Every pluggable layer has an **enforced contract** checked at registration,
and DevQ's own components register through the very same path — so the
extension mechanism cannot rot while the shipped system keeps working. That
single fact is what makes DevQ usable as a research instrument: a
third-party policy is not a second-class citizen, it is built exactly the
way the defaults are.

---

## Phase 0 — Hardware abstraction

**What it is.** A hardware-agnostic device model that lets DevQ treat any
backend — simulated or real, IBM or otherwise — through one uniform
interface, carrying topology and calibration data.

**How it works in the core.** A `QuantumDevice` is a pure data container;
`load_device()` validates it; `TopologyGraph` (built on NetworkX) holds the
coupling map; `BaseProvider` is the ABC every backend adapter implements
with exactly `get_device()` + `execute()`. Two providers ship:

- **DevQSimulatedProvider** — a pure-Python backend factory with four
  topologies (fully-connected, linear, grid, random), generated error maps,
  and mocked execution. It doubles as the reference implementation for
  provider authors and has no heavy dependencies.
- **IBMSimulatedProvider** — wraps Qiskit V2 fake backends (FakeSherbrooke,
  FakeNairobiV2, FakeLagosV2, …) with **real IBM calibration data** pulled
  from the Target API. The native two-qubit gate is auto-discovered per
  backend (ECR on Eagle/Heron, CX on older Falcon devices), and execution
  runs on AerSimulator with the backend's noise model, honouring the
  allocator's physical-qubit mapping via `initial_layout`.

Both providers accept an optional `seed` for reproducible runs.

**🔬 Researchers** get a device abstraction that separates *policy* (what
you're studying) from *hardware* (what you're running on): the same
allocator or router runs unchanged against a synthetic topology or a real
IBM calibration snapshot, so you can develop against fast pure-Python
devices and validate against realistic noise without changing your code.

**🎓 Learners** get to see a real device as data — its coupling graph, its
per-qubit and per-edge error rates — instead of an opaque cloud endpoint.
The DevQSimulatedProvider's four topologies make "what does connectivity
mean" concrete and manipulable.

**🛠️ Quantum developers** get one interface across backends. Attaching a
device is one line; the calibration and topology come along automatically,
and the same job description runs against any of them.

---

## Phase 1 — Process model, kernel, and shell

**What it is.** The operating-system heart of DevQ: a job control block, a
process table, a well-defined job lifecycle, future-based execution, and an
interactive shell to inspect all of it.

**How it works in the core.** The **Quantum Control Block (QCB)** is the
quantum analogue of a process control block: it carries the job_id, the
circuit, the virtual→physical qubit map, the lifecycle state, the device
binding, the execution future, the result, and the job's own noise
thresholds and device constraints. Job IDs are global across all devices.

The **six-state lifecycle** is precise and total: `READY → WAITING /
REJECTED / RUNNING → FINISHED / FAILED`. READY is a queued job that has not
yet attempted allocation; WAITING is transient (attempted, blocked on
resources, will retry); **REJECTED** is the umbrella terminal state for any
kernel-level rejection — device constraints excluding every device,
unsatisfiable thresholds everywhere, a provider capability that no attached
device offers (a dynamic circuit needing classical feedback, when no
device's provider supports it), or allocation classification inside a
scheduler. Execution is **future-based** (`ExecutionFuture` /
`AsyncExecutionFuture` behind one `done()`/`result()` interface), a design
that later made truly asynchronous multi-device execution a drop-in.

The **QShell** provides a full inspection command set (see
[`SHELL.md`](SHELL.md)):

| Command | What it shows / does |
|---|---|
| `qrun` | Priority-execute a single job immediately, bypassing the queue |
| `qsubmit` | Enqueue one or more jobs without executing |
| `qrunpack` | Drain all queues via the router and per-device schedulers |
| `qdevices` | List devices: index, name, provider, qubits, queued/running load |
| `qmap <job_id>` | A job's device and virtual → physical qubit mapping |
| `qtopology [dN] [q …]` | Device coupling map(s) |
| `qerrors [q\|e\|b] [dN]` | Qubit errors, edge errors, or both |
| `qconfig [dN]` | Router policy and each device's config with the source of every value |
| `qregistry [p r s a f]` | Registered components — providers, routers, schedulers, allocators, frontends (built-in and externally registered); flags filter by kind |

**🔬 Researchers** get a lifecycle with *honest terminal states* — a
rejected job is classified with a reason, never silently dropped — which
means a benchmark's rejection numbers are trustworthy and a policy that
over-rejects is visible rather than hidden.

**🎓 Learners** get the single most valuable teaching feature in DevQ: you
can watch a job move through its states, ask `qmap` exactly which physical
qubits it landed on and why, and `qconfig` to see *where each configuration
value came from*. The abstractions are named after their classical
counterparts (control block, process table, scheduler) so prior OS
intuition transfers directly.

**🛠️ Quantum developers** get an interactive control surface over
execution: submit, inspect, and run jobs with real feedback, and a mapping
inspector (`qmap`) that answers "which qubits did my circuit actually use"
— a question most vendor runtimes make hard to answer.

---

## Phase 2 — Qubit allocation

**What it is.** Three interchangeable strategies for choosing *which
physical qubits* a circuit is placed on, all honouring hard noise
thresholds.

**How it works in the core.** All allocators implement `BaseAllocator`
(`allocate(circuit, device, pool, max_qubit_error=None,
max_edge_error=None)`) and answer feasibility via `feasible()`:

- **StaticAllocator** *(Alt)* — first available block, no topology
  awareness. A baseline; sensible for all-to-all devices.
- **GraphAllocator** *(Alt)* — BFS over the topology, guaranteeing a
  connected subgraph.
- **NoiseGraphAllocator** *(default)* — BFS plus a weighted cost
  `S = α·Σ(qubit_error) + β·Σ(edge_error)`, where α/β are the config keys
  `qubit_error_weight` / `edge_error_weight` (defaults 0.1 / 0.9, since
  two-qubit gate fidelity dominates NISQ noise). The balance is tunable per
  device through the full config cascade.

Every allocator enforces **hard noise thresholds** *before* cost
optimisation: qubits or edges whose error exceeds the job's
`max_qubit_error` / `max_edge_error` are excluded from consideration
entirely. `feasible()` answers whether a job could *ever* be placed on the
device under its thresholds, independent of current pool state.

**🔬 Researchers** get a clean `BaseAllocator` seam and a *scored* default
(NoiseGraph) whose cost model is a published formula with tunable weights —
the ideal structural twin to compare a new allocator against. The α/β cost
`S` is documented formally in [`COST_MODEL.md`](COST_MODEL.md).

**🎓 Learners** see the qubit-mapping problem made tangible: threshold
filtering first (which qubits are even allowed), then cost optimisation
(which allowed placement is cheapest). The three allocators form a natural
teaching ladder from naïve to topology-aware to noise-aware.

**🛠️ Quantum developers** get noise-aware placement for free, with
per-job control: set `--max-qubit-error` / `--max-edge-error` to refuse
placements that are too noisy for your circuit, and the allocator finds the
lowest-noise connected block that satisfies them.

---

## Phase 3 — Job scheduling

**What it is.** Three interchangeable strategies for deciding *when* queued
jobs run on a device, including concurrent multi-circuit packing.

**How it works in the core.** All schedulers implement `BaseScheduler`
(`schedule()` returns the jobs processed in a cycle — dispatched and/or
rejected):

- **FCFSScheduler** *(Alt)* — strict submission order. Head-of-line blocking
  applies only to WAITING (feasible-but-blocked) jobs; unsatisfiable jobs
  are REJECTED and removed, so they never block the queue.
- **ShortestDepthScheduler** *(Alt)* — shallowest circuit first.
- **PackingScheduler** *(default)* — shortest-depth-first plus greedy
  bin-packing via a temporary reservation pool (TempPool) with two-phase
  commit, so multiple circuits run concurrently on disjoint qubit sets.

This phase also delivered the **configuration cascade** and the **QShell
job parser** (JobSpec — bracket groups, per-job threshold and device flags,
wired end-to-end).

**🔬 Researchers** get a scheduler seam that already proves the hard cases
(concurrent packing, honest rejection, head-of-line semantics), so a new
scheduling policy has a rigorous baseline set to beat — and the packing
scheduler's TempPool two-phase-commit is a worked example of doing
concurrency correctly.

**🎓 Learners** meet classic scheduling policies (FCFS, shortest-job-first,
bin-packing) in a quantum setting, and can watch the difference between them
on the same workload — including *why* packing achieves higher qubit
utilisation.

**🛠️ Quantum developers** get concurrent execution on a single device out
of the box: independent circuits that fit on disjoint qubits run together
rather than serially, improving throughput without any manual effort.

---

## Phase 4 — Distributed scheduling and routing

**What it is.** Execution across multiple heterogeneous backends at once,
following the classical cluster pattern — DevQ is the node kernel, the
router is the cluster scheduler.

**How it works in the core.**

- **DeviceContext** — the federation unit, one per attached device,
  bundling the device, its MemoryManager/QubitPool, its allocator instance,
  its scheduler instance, and its resolved per-device config. Per-device
  policy is therefore real: d0 can pack with NoiseGraph while d1 runs FCFS
  over Static.
- **Device router** behind `BaseRouter` (`select(qcb, candidates)`):
  - **NoiseRouter** *(default)* scores each feasible candidate by
    `w_queue · queue_pressure + w_noise · best_case_cost` (both min-max
    normalised across candidates) and routes to the lowest score. Queue
    pressure is queued + running jobs; best-case cost dry-runs the device's
    *own configured allocator* on an empty-pool clone and scores the result
    with the shared `S` yardstick — so the score reflects the placement the
    job would actually receive under that device's real policy. Ties break
    by lower device index.
  - **RoundRobinRouter** *(Alt)* — cycles through feasible devices; a
    load- and noise-oblivious fairness baseline.
- **Sticky routing** — a job is routed once, at its first scheduling cycle,
  and the binding is recorded on the QCB. (Work migration of WAITING jobs is
  a deliberate open research knob, not yet implemented.)
- **Cross-device rejection semantics** — REJECTED means unsatisfiable on
  *every device the job may run on*; the router calls each candidate's
  `feasible()` and aggregates one reason per device.
- **Job-level device constraints** — `--exec` (allow-list, no fallback
  outside it) and `--no-exec` (deny-list).
- **Truly asynchronous execution** — `AsyncExecutionFuture` wraps a real
  thread-pool future behind the same interface; both simulated providers
  execute asynchronously, so circuits genuinely run concurrently across
  devices while the kernel keeps routing and scheduling. Worker threads only
  compute; all state mutation happens on the shell thread in the kernel's
  resolution step, so the system needs **no locks**.

**One circuit, one device.** There are no quantum links between backends, so
a circuit never spans devices; DevQ federates rather than merges, and
physical qubit indices stay local to their device everywhere.

**🔬 Researchers** get an entire additional decision layer to study — the
router — with a scored default and a naïve baseline, plus named open
problems (cross-backend shot aggregation, coherence-window scheduling, work
migration). The two-level split (router chooses device, scheduler chooses
time) is itself a research surface: a policy can be a router *or* a
scheduler, and DevQ makes that distinction architecturally explicit.

**🎓 Learners** see distributed-systems concepts (cluster scheduling, load
balancing, sticky binding, lock-free state management) transplanted into
quantum execution, and can run one workload across several simulated devices
to watch routing decisions unfold.

**🛠️ Quantum developers** get real multi-backend execution: attach several
devices, submit a batch, and DevQ routes each job to the best available
device and runs them concurrently — with `--exec`/`--no-exec` to pin or
exclude devices per job when you need control.

---

## Cross-cutting core systems

These are not single-phase features; they are the machinery that makes the
whole platform coherent, extensible, and reproducible.

### The component registry and extension model

**How it works.** Every pluggable part — scheduler, allocator, router,
provider, frontend — is attached to a `DevQ` instance through the registry
with **no edits to core**:

```python
devq.register_scheduler("mine", MyScheduler)
devq.register_allocator("mine", MyAllocator)
devq.register_router("mine",    MyRouter)
devq.register_provider("ionq",  IonQProvider)
```

Registering a component makes its name a legal config value immediately
(the legal set is read from the registry, not a fixed list), and a component
may declare its own **namespaced config keys** (`mine.batch_window`) that
cascade, validate, and appear in `qconfig` exactly like core keys. Contracts
are checked **at registration** — the ABC, the constructor signature DevQ
will call, the methods the kernel invokes, and any declared configuration.
DevQ's own components register through this same path. Full detail in
[`REGISTRY.md`](REGISTRY.md) and [`EXTENDING.md`](EXTENDING.md).

**🔬 Researchers**: your policy is a first-class citizen, validated the same
way the defaults are, benchmarkable against them with no core edits — the
foundation of DevQ-as-testbed. **🎓 Learners**: the registry is a clean,
readable example of contract-checked plugin architecture. **🛠️ Developers**:
add a new backend or a custom policy without forking DevQ.

### The four-level configuration cascade

**How it works.** Configuration is resolved independently per device through
a four-level cascade (later levels override earlier): **core defaults ←
provider defaults ← global user file ← per-device file**. Keys fall into
three scopes — **global** (system-wide, e.g. the router policy; providers
deliberately cannot set these), **common** (per-device, e.g. the α/β
allocator weights), and a **per-job** tier above the cascade for `shots`.
`qconfig` reports the resolved value *and its source* for every key. See
[`CONFIGURATION.md`](CONFIGURATION.md).

**🔬 Researchers**: precise, provenance-tracked control over every knob a
comparison depends on. **🎓 Learners**: `qconfig` makes configuration
precedence — usually invisible and confusing — completely legible.
**🛠️ Developers**: tune per device without touching global policy, and
always know where a value came from.

### Determinism and reproducibility

**How it works.** Seeded runs guarantee **decision determinism**: the same
seed produces the same routing, allocation, and decision counts. (DevQ does
*not* guarantee completion-order determinism — async executions may finish
in different real-time orders — a distinction it states explicitly because
it matters for metric design.) See
[Reproducibility & Seeding](CONFIGURATION.md#reproducibility--seeding).

**🔬 Researchers**: reproducible experiments are the whole game — a seeded
DevQ run replays its decisions exactly, so a reported result can be
regenerated and audited. **🎓 Learners**: rerun the same scenario and get
the same decisions, so cause and effect are stable while you experiment.
**🛠️ Developers**: reproduce a routing/allocation outcome exactly when
debugging.

### The structured event log

**How it works.** A run emits a structured event log with a **two-clock
model** — a deterministic `seq` counter and wall-clock `*_at` timestamps —
and records, per decision, the scoring terms a component reported (`route`
and `allocate` events carry per-candidate `scores`). A `${}` placeholder
mechanism in workload specs keeps credentials out of logged artifacts. See
[`EVENT_LOG.md`](EVENT_LOG.md).

**🔬 Researchers**: the log is the raw evidence layer — every decision and
its inputs recorded, so an analysis (including the weight sweep below) can be
answered from one recorded run rather than by re-executing. **🎓 Learners**:
a readable trace of exactly what happened, in order. **🛠️ Developers**: an
audit trail for every run, with a credential-safe placeholder mechanism for
spec files.

---

## Phase 5 — Research benchmarking mode

Phase 5 turns DevQ from a working orchestrator into a **research
instrument**: a platform for evaluating scheduling, allocation, and routing
policies fairly and publishing defensible comparisons. It is shipping in
sub-phases.

### 5.1 — Determinism, registry, event log
Seeded per-device determinism, the component registry, and the structured
two-clock event log (all described above under cross-cutting systems).

### 5.2 — Workload specs and the runner
A strict spec parser, a headless benchmark runner, a run directory layout,
shipped workload specs, and the `${}` credential-safe placeholder mechanism.
The runner executes a workload without the interactive shell, so benchmarks
run unattended. See [`WORKLOADS.md`](WORKLOADS.md).

### 5.3 — The metrics layer
Metrics computed **offline from a finished run** — throughput, queue
latency, utilisation, rejection rate, and load imbalance — with fidelity
added once the noiseless-reference machinery exists. Definitions and the
offline/reproducibility rules are in [`METRICS.md`](METRICS.md).

**🔬 Researchers**: a standard, documented metric set so two policies are
compared on the same measuring surface — and computed offline from the log,
so the measurement never perturbs the run. **🎓 Learners**: concrete
definitions of what "good scheduling" even means (throughput vs latency vs
utilisation vs fairness). **🛠️ Developers**: quantitative feedback on how a
configuration actually performed.

### 5.4 — Fidelity and the QASMBench suite
Fidelity against a noiseless reference run (Hellinger fidelity, matching the
QOS definition for like-for-like cross-system comparison; total variation
distance as a hand-checkable companion), and the full QASMBench small suite
as a workload set. See [`METRICS.md`](METRICS.md) and
[`REFERENCES.md`](REFERENCES.md).

**🔬 Researchers**: an execution-quality metric defined identically to the
nearest comparable system (QOS), plus a recognised benchmark circuit suite —
the ingredients of a comparison a reviewer will accept. **🎓 Learners**:
fidelity made concrete as a distance between count distributions.
**🛠️ Developers**: a measure of how much noise actually cost you on a given
placement.

### 5.5a/b — Comparison engine and modes
`assemble_matrix` bundles every session's config, metrics, and sweepable
axes into `comparison.json` (the inter-component surface); `rank_sessions`
orders sessions by any metric; `present_sweep` reads out an intra-component
weight sweep. One `render_text` view serves both, and can write a `.txt`.

### 5.5c — The n-ary weight sweep
**What it is.** A way to ask, from a *single recorded run*, how a scoring
component's decisions change as its weights vary across the whole weight
space — without re-executing anything.

**How it works in the core.** A scoring component exposes its per-candidate
scoring terms via the `Sweepable` contract. Because the score is a linear
combination compared by arg-min, its ranking is **scale-invariant** (only
the direction of the weight vector matters), so the faithful search space is
the normalised **simplex**. DevQ enumerates it as the **Scheffé {n, m}
simplex-lattice** ([`REFERENCES.md`](REFERENCES.md), Scheffé 1958): every
normalised weight n-tuple whose entries are multiples of 1/m. At n=2 this is
exactly the historical (α, 1−α) grid. The winner a weight point induces is
piecewise-constant, so the sweep enumerates the lattice and localises flips
along its **edge graph** by bisection (valid only along edges, never
interior chords). The faithful claim is bounded to **first-flip
sensitivity**: replay is exact only up to the first decision that reads
state a prior decision mutated, and DevQ states that bound rather than
over-claiming. See [`COST_MODEL.md`](COST_MODEL.md).

**🔬 Researchers**: this is a methodology feature. It answers "how sensitive
is my policy to its weights, and where do its decisions flip" exactly and
cheaply, and it makes explicit a subtlety the field usually glosses — that a
weight sweep is only faithfully replayable up to the first state-mutating
decision. It is the instrument for the argument that *comparing quantum
policies is harder and more error-prone than the field treats it.*
**🎓 Learners**: a vivid, visual way to see how a scoring weight changes a
decision — and that a "best weight" is workload- and metric-relative, not
absolute. **🛠️ Developers**: understand how robust your chosen allocator or
router weights are before you rely on them.

### 5.6 — Baseline plugins (NAQJS)
**What it is.** The first published baseline from the literature, built as a
DevQ plugin so a comparison reads as "DevQ vs the literature" rather than
"DevQ vs a strawman". NAQJS ([`REFERENCES.md`](REFERENCES.md)) is a scored
*scheduler* — it ranks the queue by a weighted sum of circuit width, shot
count and submission order, then packs up to an η·N cap.

**How it works in the core.** NAQJS lives under `research/`, built through
the documented plugin API: `BaseScheduler` + the `Sweepable` hooks + a
namespaced `CONFIG_SCHEMA` (`naqjs.width_weight`, `naqjs.shots_weight`,
`naqjs.seq_weight`, `naqjs.eta`, `naqjs.default_shots`). Landing it exercised
the `schedule` scoring seam for the first time and completed the sweep's
scheduler axis. It also drove the one deliberate core edit of the phase:
`dq.build` now injects a scheduler's dotted config keys into its constructor
generically (previously scheduler config never reached the constructor — only
the sweep set the weights), so future scheduler plugins wire through with no
core change. Two comparison scripts benchmark NAQJS against the default
Packing scheduler — one on a minimal workload, one on the full QASMBench
small suite across four IBM fake backends — each ranking the two on the
metrics and sweeping NAQJS's three-weight simplex.

**🔬 Researchers**: the comparison **mode** itself was validated with a
low-vs-high-contention contrast — the same harness reports a tie when
scheduling cannot matter (little contention, order barely affects
completion) and a scheduler-attributable divergence when it can (wide jobs
serialised on one device). The honest corollary, surfaced rather than hidden:
single-run wall-clock throughput is noise-dominated, so a defensible
performance number needs mean ± noise floor over N runs. **🛠️ Developers**:
NAQJS is the worked example for writing your own scored scheduler plugin —
see [`EXTENDING.md`](EXTENDING.md). **🎓 Learners**: a concrete instance of a
"noise-aware" scheduling policy you can read, run, and sweep.

### 5.6 — Baseline plugins (Mapomatic)
**What it is.** The second published baseline, and the first for the
*allocator* axis. Mapomatic ([`REFERENCES.md`](REFERENCES.md); Nation &
Treinish, PRX Quantum 2023) is a calibration-aware layout chooser: it scores
each placeable block by the product of its per-operation fidelities —
$S = 1 - \prod(1-e)$ over readout, single-qubit-gate and two-qubit-gate
errors — and picks the lowest. Where NAQJS is a scored *scheduler*, Mapomatic
is a scored *allocator*, so together they cover two of DevQ's three plugin
axes (QOS, the router baseline, is the third).

**How it works in the core.** Mapomatic lives under `research/`, built
through the documented plugin API — `BaseAllocator` + the shared filtering
helpers + the device calibration accessors — with **zero core edits**. That
zero is the point: it is the first plugin to land *after* the
schema→constructor wiring was unified across all three build paths, so it
confirms the allocator path needs no bespoke wiring, only the plugin class.
Unlike NAQJS, Mapomatic is a *non-scoring* policy in the `Sweepable` sense:
its product-of-fidelities cost is parameter-free, so it exposes no weight
simplex and honestly implements none of the sweep hooks — the deliberate
fixed-vs-tunable contrast with DevQ's own `NoiseGraphAllocator`, whose cost
is a *tunable* $\alpha \cdot \sum q + \beta \cdot \sum e$.

**🔬 Researchers**: `research/mapomatic_comparison.py` benchmarks the two
allocators on the QASMBench small suite ranked on **fidelity** — the metric
an allocator's qubit choice actually moves, since two allocators that both
place every job produce near-identical timing on an uncontended batch. The
comparison isolates the effect of the aggregation rule (multiplicative
fidelity vs. additive weighted error) on the same calibration inputs, and
the honest result is a split decision — one policy wins the median, the
other the mean and the tail — surfaced rather than collapsed into a single
misleading "X beats Y". **🛠️ Developers**: Mapomatic is the worked example
for a *non-scoring* allocator plugin — the counterpart to NAQJS's scored
one, showing what a policy owes (an `allocate()` contract) and what it may
honestly leave alone (the sweep hooks). **🎓 Learners**: a concrete,
runnable instance of the qubit-selection heuristic real Qiskit ships.

### 5.6 — Baseline plugins (QOS)
**What it is.** The third published baseline, and the first for the *router*
axis — completing the scored-axis set (scheduler, allocator, router). QOS
([`REFERENCES.md`](REFERENCES.md); Giortamis et al., OSDI '25) is a whole
quantum operating system; its spatial *which-QPU* decision — a fidelity
estimate per candidate device, traded against waiting time and utilisation —
is a router decision, and that is the slice DevQ ports. Where NAQJS scores a
*scheduler* and Mapomatic an *allocator*, QOS scores a *router*, the third
and last scored axis.

**How it works in the core.** QOS lives under `research/`, built through the
documented plugin API — `BaseRouter` + the `Sweepable` hooks + the
`DeviceContext`/`QubitPool` read surface + the device calibration accessors
— with **zero core edits to the plugin path**. Unlike Mapomatic, QOS *is* a
scored, sweepable policy: it exposes `qos.fidelity_weight` ($c$) and
`qos.util_weight` ($\beta$) and implements all three sweep hooks, scoring
each device by QOS's Sec. 6 fidelity estimate and selecting with Sec. 8's
relative-delta trade-off against the candidate field. Building it surfaced —
and drove the fix for — a real gap: plugin-weight sweeping had been
generalised for the scheduler axis only, so a plugin router with its own
weights could not be swept; all three axes now derive their swept keys from
`live_params()` uniformly (core, mutation-tested). QOS carries three recorded
faithfulness caveats: a dropped crosstalk term (DevQ has no crosstalk
calibration), a device-representative rather than per-mapping fidelity
estimate (placement is the allocator's job, below the router), and an
inverted utilisation sign (QOS rewards utilisation to serve its
multi-programmer, which DevQ's router lacks, so the faithful port spreads
load).

**🔬 Researchers**: `research/qos_comparison.py` benchmarks QOS against the
default NoiseRouter on the QASMBench small suite ranked on **fidelity**, then
sweeps QOS's `(c, β)` weight space; the honest result is again a split — one
router wins the median and mean, the other the worst-job fidelity and the
load balance — surfaced, not collapsed. `research/qos_composition.py` then
runs QOS, NAQJS and Mapomatic *together* as one stack, the concrete proof
that three baselines authored against three papers compose across DevQ's axes
with no core edit. **🛠️ Developers**: QOS is the worked example for a scored
*router* plugin, and for porting a whole-system policy as a single axis — the
counterpart to the "policies that span more than one component" note in
[`EXTENDING.md`](EXTENDING.md). **🎓 Learners**: a runnable instance of how a
cloud quantum OS decides which QPU runs your job.

### Phase 5 in one sentence
Write an allocator against `BaseAllocator`, a router against `BaseRouter`,
or a scheduler against `BaseScheduler`; register it with one line; benchmark
it against the built-ins on shared metrics and reproducible workloads —
without touching DevQ core.

---

## Quality machinery (why you can trust the above)

**Sanity test suite.** `run_tests.py` drives the whole plugin matrix through
the real shell wiring, block by block, with coarse assertions that catch
crashes, hangs, and silent regressions. `--list` shows the blocks, `-v`
shows full transcripts. See [`TEST_BLOCKS.md`](TEST_BLOCKS.md).

**Mutation testing.** DevQ tracks whether its tests would actually *catch* a
regression by deliberately breaking the code and confirming a test fails —
and when a mutant survives, the test is strengthened, not the mutant
excused. See [`MUTATION_TESTING.md`](MUTATION_TESTING.md).

**🔬 Researchers**: results you build on rest on tests whose adequacy is
itself measured — the same discipline your own baseline comparisons need.
**🎓 Learners**: a worked example of testing done seriously, including
mutation testing most projects skip. **🛠️ Developers**: confidence that the
orchestration layer beneath your circuits is verified, not merely written.

---

## Planned (not yet available)

Documented here so the boundary between what exists and what is coming is
never ambiguous:

- **Phase 5.6 — baseline plugins**: NAQJS (scored scheduler), Mapomatic
  (scored allocator), and QOS (scored router) have all shipped, completing
  the "DevQ vs the literature" comparison set across all three scored axes.
  They ride the generic schema→constructor wiring — unified across the
  scheduler, allocator, and router build paths — that the scheduler axis
  introduced. This closed Phase 5.
- **`qbench` (deferred, unnumbered)**: an interactive benchmarking sub-shell
  over the metrics and comparison layers — a researcher convenience over an
  already-working API, pulled from the numbered roadmap. It wants in-session
  device-fleet mutation (an unscoped capability) to be worth building; see
  [`ROADMAP.md`](ROADMAP.md).
- **Phase 6 — interchangeable frontends**: Silq, Q#, Qiskit circuits behind
  the circuit-representation layer.
- **Phase 7 — expanded providers**: real IBM hardware, Cirq, IonQ.
- **Phases 8–9 (ideas, not committed)**: executable claims a reviewer can
  run, and a shared component index with claims re-verified on install.

See [`ROADMAP.md`](ROADMAP.md) for the full rationale of each.

---

## Where to go next

| If you want to… | Read |
|---|---|
| Run and inspect jobs | [`SHELL.md`](SHELL.md) |
| Configure devices and seeding | [`CONFIGURATION.md`](CONFIGURATION.md) |
| Register a plugin | [`REGISTRY.md`](REGISTRY.md) |
| Build a plugin (contracts, `Sweepable`) | [`EXTENDING.md`](EXTENDING.md) |
| Understand what a run emits | [`EVENT_LOG.md`](EVENT_LOG.md) |
| See the cost/score formulas | [`COST_MODEL.md`](COST_MODEL.md) |
| See the metric definitions | [`METRICS.md`](METRICS.md) |
| Run and read the tests | [`TEST_BLOCKS.md`](TEST_BLOCKS.md) · [`MUTATION_TESTING.md`](MUTATION_TESTING.md) |
| Understand the design history | [`ROADMAP.md`](ROADMAP.md) |
| Check citations and provenance | [`REFERENCES.md`](REFERENCES.md) |