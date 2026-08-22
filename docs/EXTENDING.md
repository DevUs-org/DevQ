# Extending DevQ

How to **build** a component that satisfies its contract — a scheduler,
allocator, router, provider or frontend. Registering it (making the name
addressable, declaring its config keys) is a separate step, in
[`REGISTRY.md`](REGISTRY.md); this file is what the component itself must
do once registered.

## The shape of a contract

Every kind has a small contract with three tiers, and knowing which tier a
method is in tells you whether you must write it:

- **Required.** The one or two methods DevQ actually calls to do the
  component's job. Abstract on the base class, so a subclass that omits
  them will not construct. A provider implements `get_device()` and
  `execute()`; an allocator `allocate()`; a router `select()`; a scheduler
  `schedule()`.
- **Optional with a default.** Methods the base class already implements
  sensibly, which you override only to change behaviour. An allocator's
  `feasible()` classifies unsatisfiable jobs and has a working default; a
  provider's `reference_ideal()` returns `None` (declines) unless your
  provider can simulate noiselessly. Overriding is a choice, not an
  obligation.
- **Opt-in capabilities.** Hooks that grant a whole *feature* if you
  implement them and are silently absent if you do not. `LABEL` gives a
  human-readable name (else the class name is used). The three `Sweepable`
  hooks make a scoring component explainable and weight-sweepable; a
  component that does not score leaves them alone and is honestly reported
  as not-sweepable — the feature is opt-in precisely so a non-scoring
  policy (round-robin routing, FCFS scheduling, a cost-oblivious
  allocator) owes nothing and fakes nothing.

The reason optionality is designed in rather than enforced away: forcing
every component to implement every hook would tax the common, simple case
to serve the rare one. A round-robin router has no scores; making it
invent them to satisfy an interface would produce a log that lies. So the
default is always "not provided", and providing a hook is how you turn a
capability on.

The `Sweepable` contract below is the fullest example of an opt-in
capability — worth reading even if you are not writing a scoring
component, because it explains why the sweep is answerable from one
recorded run and what "the component owns its own scoring" buys.

---

## Imports: what a component may depend on

Two rules govern what a component is allowed to import, and they are what
keep the plugin seam a seam rather than a suggestion:

- **Absolute imports only.** No relative imports (`from .sibling import …`,
  `from ..core import …`) anywhere. Every import is spelled from the repo
  root (`from plugin_bases.base_router import BaseRouter`). A file may still
  import a genuine *sibling* helper that ships alongside it — a provider and
  its own `backend_factory`, a frontend and its own tokenizer — because that
  is the component's own implementation, not a reach across the seam.

- **Reach into DevQ only through `plugin_bases`.** A component imports from
  `plugin_bases` and nowhere else in DevQ. It must never import `kernel.*`,
  `hardware.*`, `circuits.*`, `registry.*` or any other core package
  directly. Third-party dependencies (numpy, qiskit, your own libraries)
  are unrestricted — the rule is about DevQ-internal coupling only.

Concretely, a component imports from exactly two places inside DevQ:

- **its contract, from its base** — `BaseProvider` from
  `plugin_bases.base_provider`, `BaseRouter` from `plugin_bases.base_router`,
  and so on (plus the base's own contract types, like `AllocationError` and
  `RouterContractError`);
- **every other core type it needs, from `plugin_bases.common`** — the
  single plugin-facing surface. `common` imports the plugin-relevant slice
  of core and re-exports it in one place:

| From `plugin_bases.common` | For |
| --- | --- |
| `QuantumDevice`, `ExecutionResult`, `ExecutionFuture`, `submit_async` | providers |
| `CircuitRep` | frontends |
| `JobStates` | schedulers |
| `QubitPool` | routers |
| `eligible_qubits`, `edge_allowed`, `has_connected_block` | allocators |
| `KeySpec`, `unit_interval`, `non_negative` | any component declaring config keys |

So a scheduler that needs the job-state enum writes `from
plugin_bases.common import JobStates`, not `from
kernel.process.lifecycle import JobStates`. A provider writes `from
plugin_bases.base_provider import BaseProvider` and `from
plugin_bases.common import QuantumDevice, submit_async`.

Why one shared module rather than re-exporting from each base: a type like
`KeySpec` belongs to no single seam (routers and schedulers both declare
config keys), and a component's needs grow over time — collecting the
plugin-facing surface in one place means there is never a "which base does
this live under?" decision, and a maintainer exposing a new core type to
authors adds one line to `common` and nowhere else. The bases stay pure
contract logic; `common` is the one file that names core paths.

`Sweepable` is the exception: it physically lives in `plugin_bases`
itself (`plugin_bases.sweepable`), is inherited by the scoring bases, and
is never imported by a component directly — so it is not in `common`.

If you need a core type that `common` does not yet expose, add it to
`common` rather than importing across the seam. (Keep the dependency
one-way: `common` imports *from* core, never the reverse at module load —
the handful of core modules that reference the plugin layer do so with
deferred, function-local imports, which is what keeps the import graph
acyclic.)

---

## What to implement

Registration is how a component becomes addressable; this is what the
component itself must do. Each kind has a small contract, and a couple of
points below are load-bearing for correctness rather than style.

**New provider** — subclass `BaseProvider`, implement `get_device()` and
`execute(circuit, v2p_map, shots, device)`. Return either a synchronous
`ExecutionFuture` or (preferred) an `AsyncExecutionFuture` via
`submit_async(fn)` (imported from `plugin_bases.common`) — the
kernel polls `done()`/`result()` and never knows the difference. No knowledge of the
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

The placement `v2p_map` names only the qubits the circuit uses, which is a
*partial* layout. On a small backend a transpiler tolerates that, but Aer
on a large device (a 156-qubit Heron fake, say) rejects it outright —
`"The 'layout' must be full (with ancilla)."` — because it wants the
layout to enumerate **every** physical qubit, the used ones plus every
unused one as an ancilla. `IBMProvider.full_layout(qc, v2p_map, device)`
builds exactly that: it adds an ancilla register to the circuit and
returns a full-device-width `Layout` with the circuit's qubits at their
allocated positions and each remaining physical qubit filled by an
ancilla. Both IBM providers inherit it and call it in place of
hand-building `initial_layout`, so the padding lives in one place rather
than being copy-pasted (and mis-copied) per provider — the same
single-source-of-truth discipline as `_counts_width` and `flatten_key`.

It lives on `IBMProvider`, the **Qiskit-family base** (see below), not on
`BaseProvider`: it returns a qiskit `Layout` and is meaningful only when a
provider's placement API is a Qiskit `initial_layout`. A non-Qiskit
provider (Braket, a photonic backend) subclasses `BaseProvider` directly
and never inherits it, so `BaseProvider` carries no qiskit dependency —
not even a deferred, function-local import. One consequence to respect:
the transpile target must know the device's true width for the padded
layout to validate — transpile against the backend (or an Aer simulator
built from it), not a bare noise-model simulator that carries no coupling
map. The ancilla widen the *layout* only; they never touch the classical
register, so the counts width is unchanged.

Third, **the counts a provider returns must obey a shared shape**, because
cross-provider comparison (the fidelity metric, baseline sweeps) reads the
bitstrings directly and a disagreement is silent. The result's `counts`
maps measured bitstrings to shot counts; the bitstring width is the
declared classical register (`num_clbits`, falling back to `num_qubits`
when no `creg` is declared) — call `self._counts_width(circuit)` rather
than re-deriving it, so the rule cannot drift between providers. A
measured bit sits at its own classical-bit index, an unmeasured bit reads
0, explicit measures are honoured (fall back to measure-all only when the
circuit has none), and `reset` is applied at its source position. A
provider that ignores gate semantics (a uniform mock) still owes the width
and index conventions — only the distribution is its own business. The
full statement lives on `BaseProvider.execute()`'s docstring.

If your provider is stochastic, accept `seed=None` in `__init__`, call
`super().__init__(seed)`, and derive all randomness from a provider-local
generator — see [Reproducibility & Seeding](CONFIGURATION.md#reproducibility--seeding).

If your provider needs a **credential** (an API token, an endpoint) to
reach real hardware or a remote service, add a `secrets=None` parameter to
`__init__` and read your own keys out of it:
`def __init__(self, seed=None, secrets=None)`, then
`token = (secrets or {}).get("token")`. A spec-driven run supplies these
through its top-level `secrets` block, which DevQ resolves from the
environment and delivers here while keeping the resolved value out of every
log (see [`WORKLOADS.md`](WORKLOADS.md)). You choose the key names — DevQ
never inspects them — and a provider that names no `secrets` parameter is
simply constructed without one. Put credentials here, never in a
`get_device()` argument or a config key: both are logged in full, and this
channel exists precisely so a secret is not.

**Optionally, `reference_ideal(circuit)`** — the noiseless IDEAL
distribution for a circuit, `{bitstring: probability}` at the same
Option-B width `execute()` reports, or `None` if this provider cannot
produce one. This is what the fidelity metric compares a noisy run
against, and it is a property of the CIRCUIT, not a device — so one
reference-capable provider computes a run's ideals, keyed by circuit hash,
and two jobs running the same circuit on different backends share one
ideal. The default on `BaseProvider` returns `None` (declines): a provider
whose execution is a uniform mock has no meaningful ideal and correctly
inherits it, so it is never used as a reference. Override it if your
provider can simulate a circuit faithfully NOISELESSLY —
`IBMSimulatedProvider` does, running the same lowered circuit `execute()`
uses through a noiseless Aer **density-matrix** simulation and reading
exact probabilities (density-matrix, not statevector, so a mid-circuit
`reset` on an entangled qubit — a genuinely mixed state — is represented
correctly). The shipped, vendor-neutral orchestrator in
`benchmark/reference.py` discovers a capable provider through this method,
so DevQ obtains ideals without core depending on any provider; a run with
no capable provider simply reports fidelity as `None`. See
[`METRICS.md`](METRICS.md) (fidelity) for how the ideal is used.

**Optionally, `supports_dynamic(circuit)`** — the sibling capability to
`reference_ideal`, and the same OPTIONAL-with-a-default shape: a predicate
that returns whether this provider's runtime can EXECUTE a **dynamic
circuit** — one whose later gates depend on the classical outcome of an
earlier measurement (`if (creg==N)` classical control, the feedback loop
mid-circuit measurement is the primitive for). Where `reference_ideal`
asks "can you give me an ideal?", this asks "can you honour this circuit's
classical feedback?". The default on `BaseProvider` DECLINES by returning
`False` — DevQ's own execution model is terminal-measurement with no
classical feedback, so `DevQSimulatedProvider` correctly inherits the
decline. Override it to `True` if your runtime supports feedback; the IBM
providers do, overriding once on the shared `IBMProvider` base so both the
Aer-backed and real-hardware subclasses affirm from a single point. The
`circuit` is passed (not just a bare flag) so a provider may later answer
with finer granularity without a contract change. The kernel reads this at
routing time through the memory manager's feasibility verdict: a job
needing feedback is kept off a device whose provider declines, routed to a
capable device when one is attached, and REJECTED with a per-device reason
only when none is. Expressed entirely in DevQ's terms — the kernel never
learns *how* a provider runs feedback, only whether it can.

**New allocator** — subclass `BaseAllocator`, implement `allocate()` per the
documented contract (reserve via `pool.allocate()` on success; signal "no
placement possible" by raising `AllocationError`; honour thresholds as hard
constraints). Every allocator is constructed with the device's resolved cost
weights (`self.qubit_error_weight` / `self.edge_error_weight`, normalised to
sum to 1) — use them for cost scoring or ignore them freely. Optionally
override
`feasible(circuit, device, max_qubit_error, max_edge_error, max_1q_gate_error)
→ None | reason`
— the base default checks eligible-qubit count; override it if your
allocator has stricter existence requirements (see the graph allocators'
connected-block check). `feasible()` powers both scheduler-level
classification and router-level candidate filtering.

Two parts of this contract are **enforced at run time**, so a bug in a
third-party allocator surfaces as a clear, named error rather than silent
misbehaviour:

- *Signal infeasibility with `AllocationError`, not a bare exception.* The
  scheduler and router catch **only** `AllocationError` as "cannot place";
  any other exception is treated as a bug in your allocator and propagates
  with its name attached. This is deliberate — a broad catch previously
  meant a wrong-signature or crashing allocator was mistaken for an
  infeasible job and retried forever. Returning `None` is also not allowed;
  raise `AllocationError`.
- *Actually reserve what you map.* If `allocate()` returns a mapping whose
  physical qubits it did not reserve via `pool.allocate()`, the
  `MemoryManager` raises `AllocatorContractError` rather than letting the
  next job be handed the same qubits (a silent double-booking).

**New scheduler** — subclass `BaseScheduler`, implement `schedule()`.
Tunable knobs of your own go in a namespaced `CONFIG_SCHEMA` (see
[`REGISTRY.md`](REGISTRY.md)); for a scheduler, allocator, or router DevQ
additionally *injects* each schema key whose parameter name matches an
`__init__` parameter, passing the resolved cascaded value in. The dotted
key becomes the parameter name by rewriting the namespace dot to `___`,
prefix kept (`naqjs.eta` → `naqjs___eta=`). Name the parameter to receive
the value at construction, or read the resolved config at runtime instead
— a declared key is cascaded, validated, and shown in `qconfig` either
way. If you mean to read it at runtime, declare it `runtime_read=True`:
otherwise, a declared key whose parameter the constructor does not name is
assumed to be a **misspelled parameter** and `build()` warns that the
value will not be injected. The warning is the common footgun made
visible — a key that validates and cascades but reaches nothing because
`naqjs___eta` was typed `naqjs__eta` would, without it, silently fall back
to the constructor default. Marking the key `runtime_read=True` says "I
consume this myself" and silences the warning; a matching parameter needs
no marker (naming it is proof enough that injection was intended).
Keeping the prefix lets a plugin key safely reuse a core name
for its own quantity (`myalloc.qubit_error_weight` →
`myalloc___qubit_error_weight`, distinct from core `qubit_error_weight`).
The NAQJS baseline (`research/baselines/naqjs_scheduler.py`) is a worked
example: five dotted keys, the three swept weights reported through
`live_params()`, the fixed inputs (`naqjs.eta`, `naqjs.default_shots`)
kept out of it.

**New router** — subclass `BaseRouter`, implement
`select(qcb, candidates) → DeviceContext`. Candidates arrive already
filtered by the job's device constraints and per-device feasibility; your
`select()` **must return one of them** — returning a device it was not
offered (which would run the job somewhere the user's `--exec`/`--no-exec`
constraints excluded) or any non-candidate value raises `RouterContractError`.

**New frontend** — subclass `BaseFrontend`, implement
`parse(source) → CircuitRep`, and declare `EXTENSIONS` (lowercase, dotted)
for the source files it reads. A frontend takes **no constructor
arguments**: it is a stateless source-to-`CircuitRep` reader, so unlike
scheduler, allocator, and router — into which DevQ injects matching
`CONFIG_SCHEMA` keys — a frontend receives nothing at construction. A
knob, if one is ever needed, is a namespaced `CONFIG_SCHEMA` key read at
runtime, never a constructor argument.

A frontend is **dispatched, not selected**. There is no `frontend` config
key naming one winner the way `router` or `scheduler` does. Every
registered frontend is available at once, and DevQ picks one *per job*
from the source's extension. This is deliberate — it lets a single
session read several source languages in one queue. Registering a
frontend makes its extensions dispatchable immediately, with no core
edit, exactly like registering a scheduler makes its name a legal
`scheduler` value.

Two frontends may legally claim the same extension — `qasm2` and a future
`qasm3` both read `.qasm`. That is **not** a registration conflict; it is
resolved per job. A job whose extension is claimed by more than one
frontend must name its frontend explicitly, with `--frontend=<name>` in
the shell or a `"frontend"` key in a workload spec, and is rejected with
a precise error otherwise. An extension no frontend claims is likewise
rejected, before the file is read. The built-in `qasm2` ships registered
with no third-party dependency, so DevQ reads `.qasm` out of the box.

The built-in `qasm2` is a complete OpenQASM 2.0 parser
(`frontends/qasm2/`): a tokenizer, an expression evaluator that keeps
gate parameters, recursive custom-gate inlining, and first-class
`measure`/`reset`. `CircuitRep` is one ordered, op-tagged instruction
stream — every entry carries an `op` of `"gate"`, `"measure"`, or
`"reset"`, in source order — so a `reset` sits in its true position
relative to the gates around it. A consumer that wants only unitary
gates filters for `op == "gate"`; `get_depth()` and both providers do
exactly that. `measurements` (a list of `(qubit, clbit)` pairs) and
`resets` (qubit indices) are read-only views derived from the stream,
and `num_clbits` carries the declared classical-register width.

---

## Policies that span more than one component

A published policy often couples decisions DevQ keeps in separate
components. A whole-system scheduler from the literature may decide *which
QPU* a job runs on (a **router** decision, spatial), *when and in what
order* jobs run (a **scheduler** decision, temporal), and *where on the
device* a circuit is placed (an **allocator** decision) — all as one
integrated policy, because its source system draws no line between them.

This is not a reason to conclude DevQ cannot express the policy, or to
collapse it into a single oversized component. DevQ makes the scheduler,
allocator, and router **independently pluggable**, and a session names one
of each. So a policy that spans axes ports as **cooperating components** —
a custom router for its spatial half, a custom scheduler for its temporal
half — developed together and selected together in the same run. Each is a
normal plugin under the contracts above; nothing new is required.

DevQ does **not** enforce that such components be used as a set: a user
remains free to pair your router with any scheduler, or run it alone. The
coordination is yours to design and the user's to choose — but the axes are
open, so "my policy also needs job scheduling" (or placement) is a reason to
implement a scheduler (or allocator) *alongside* your router, not a reason
to leave DevQ. The QOS baseline (`research/baselines/qos_router.py`) is a
live example: its which-QPU spatial decision ports as a router, while the
waiting-time/ordering half of the same paper is naturally a scheduler — two
DevQ components expressing one published system.

---

## Runtime objects a component reads

The sections above document what a component must **write** — the method it
implements and the contract that method owes. A scoring or decision
component also **reads**: DevQ hands it live runtime objects, and to score
a candidate it must pull queue depth, calibration, or circuit shape off
them. Those objects' read surfaces are collected here, because a plugin
author writing `select()` or a scoring `schedule()` needs them and they are
not otherwise discoverable without reading core. Only the
**decision-relevant** surface is documented — each object carries more
internal state, but what a plugin may rely on is listed below.

**`DeviceContext`** — the federation unit, one per attached device
(see [`ROADMAP.md`](ROADMAP.md) Phase 4). A router's `select(qcb,
candidates)` receives a list of these as its candidates; each has already
passed the job's `--exec`/`--no-exec` constraints and per-device
feasibility.

| Read | Is | Use |
|---|---|---|
| `.device` | `QuantumDevice` | the calibration accessors (`qubit_error`, `edge_error`, `t2`, …) — see the calibration table below |
| `.queue_depth()` | → int | jobs waiting on this device (a method) |
| `.running_jobs` | int | jobs currently executing; queue pressure is `queue_depth() + running_jobs` (see [`COST_MODEL.md`](COST_MODEL.md)) |
| `.memory_manager` | `MemoryManager` | the device's pool and allocator binding (below) |
| `.scheduler` | `BaseScheduler` | the device's scheduler instance |
| `.name`, `.ref` | str | the user-assigned name and the stable device reference |

**`QCB`** — the job control block, DevQ's per-job record. A router's
`select(qcb, …)` and a scheduler's `enqueue(qcb)` both receive one.

| Read | Is | Use |
|---|---|---|
| `.circuit` | `CircuitRep` | the job's circuit — walk it for a fidelity or duration estimate (its op-stream surface is documented above) |
| `.shots` | int \| None | the job's own shot count, if it named one |
| `.circuit_hash` | str | content identity, shared by identical circuits across devices |
| `.max_qubit_error`, `.max_edge_error`, `.max_1q_gate_error` | float \| None | the job's placement thresholds, if any |
| `.exec_on`, `.no_exec_on` | list \| None | the device constraints already applied to the candidate set |
| `.v2p_map` | dict \| None | the allocator's placement, once made |
| `.state` | str | the job's lifecycle state |

**`MemoryManager`** — one per device, bundling the qubit pool and the
device's allocator. A scoring component reads it through
`context.memory_manager`; an allocator is bound to one.

| Read | Is | Use |
|---|---|---|
| `.pool` | `QubitPool` | live qubit occupancy (below) |
| `.allocator` | `BaseAllocator` | the device's configured allocator instance |
| `.device` | `QuantumDevice` | the device it manages |

**`QubitPool`** — `memory_manager.pool`, the live free/used state of a
device's physical qubits. This is the surface a component reads for
**instantaneous spatial occupancy** — for example a utilisation estimate
at decision time, which the offline utilisation *metric*
([`METRICS.md`](METRICS.md)) cannot supply because it is computed from
completed-run intervals that do not yet exist when `select()` fires.

| Read | Is | Use |
|---|---|---|
| `.free_qubits` | set | physical qubits currently free |
| `.available()` | → list | the free physical qubits as a list; `len(pool.available())` is the free count |
| `.allocate(...)`, `.free(...)` | — | reserve/release — the allocator's write surface (see the allocator contract above) |

Live spatial occupancy is `(device.num_qubits - len(pool.available())) /
device.num_qubits` — the fraction of the device's qubits in use right now,
DevQ's decision-time analogue of a QPU spatial-utilisation term.

### Inspecting a decision: `explain` and the recorded-terms surface

Every **scoring** component — router, allocator, or scheduler with the
`Sweepable` hooks implemented (below) — exposes the same three
recorded-terms methods, derived by the base from its hooks so they cannot
drift from the live decision:

- `explain_decision(decision)` — the per-candidate score report at the
  **live** weights, for the decision just made.
- `explain_recorded(recorded_terms)` — the same report rebuilt from
  **already-recorded** terms, for a component whose state has since moved
  (an allocator that has reserved its block, so its pool no longer matches
  the decision).
- `sweep_decision(recorded_terms, params)` — re-decide from recorded terms
  at **different** weights, the sweep's replay primitive.

These are uniform across the three scoring kinds. A non-scoring policy
leaves the hooks unimplemented and is honestly reported as not-explainable
and not-sweepable.

One method is **router-only**, by design rather than omission:

- `explain(qcb, candidates)` — an on-demand, **pre-decision** score report:
  given a job and a candidate set, show how the router *would* score them,
  without making or recording a decision.

A public pre-decision `explain()` is well-defined only when the decision's
inputs are externally suppliable and the choice is queryable independent of
live, mutating state — and only routing satisfies both. A router chooses
among a candidate **set a caller can hand it**, and its choice is
**sticky** (reused for later jobs of the same binding), so "how would you
route this?" is a meaningful question to ask ahead of, or apart from, any
one dispatch. An allocator chooses blocks against a **live pool** that
moves as jobs are placed, so a pre-decision report would describe a pool
state the caller cannot faithfully reconstruct — its honest analogue is
`explain_recorded`, against the terms as they actually were. A scheduler's
`schedule()` takes **no candidate argument at all** — it pulls from its
bound queue — so there is no suppliable input a pre-decision `explain()`
could receive. The asymmetry is therefore correct: all three explain
decisions *after the fact* from recorded terms; only the router can
honestly explain a hypothetical one *before* it.


### Reporting scores and sweeping weights: the `Sweepable` contract

`select()` (router) and `allocate()` (allocator) each return a winner; the
margin behind it is discarded. Phase 5.5 sweeps the cost weights and asks
how the decision responds, and the winner alone cannot answer that — every
point where the choice did not flip looks identical to one where it nearly
did. Worse, a naive "re-run at new weights" would re-execute the whole
workload once per weight point.

`explain()` (the log's per-decision score report) and a weight sweep are
the *same operation* seen from two angles: explain reports the raw terms
behind the decision just made at the live weights; a sweep replays that
decision from those same raw terms under different weights. DevQ unifies
them in one contract, `Sweepable` (`plugin_bases/sweepable.py`), which
`BaseRouter`, `BaseAllocator` and `BaseScheduler` all inherit. A scoring
component supplies three small hooks and gets both explain and sweep
support, derived so they cannot drift:

```python
class MyRouter(BaseRouter):
    def live_params(self):                 # the weights it scores with now
        return {"router_queue_weight": self.router_queue_weight, ...}
    def _sweep_terms(self, decision):      # per-candidate RAW terms, tagged
        return [(key, {...raw, weight-free inputs...}), ...]
    def _sweep_score(self, terms, params): # one candidate's score at params
        return ...
    def _sweep_rank(self, scored, params): # across-candidate: normalise,
        return [(key, final_score, enriched_terms), ...]   # combine, rank
```

The base derives the rest: `explain_decision` (the log report at live
params), `sweep_decision` (re-decide from recorded terms at any params),
`explain_recorded` (the report from already-recorded terms, for a
component whose state has since changed — an allocator that reserved its
block), and the argmin selection with the deterministic lower-key
tie-break. `select()`/`allocate()` route their live choice through the
same hooks, so the logged scores are exactly the ones that caused the
decision.

Three rules the contract enforces:

- **Record raw, weight-free terms, not just totals.** `NoiseRouter`
  min-max normalises across the candidate set, so a score is meaningful
  only relative to its peers in that one decision; the raw queue pressure
  and the α/β-free cost decomposition (`qubit_error_sum`,
  `edge_error_sum`) are what let a different weighting be re-derived from
  a recorded run instead of by re-executing. `NoiseGraphAllocator` records
  the same decomposition per block.
- **Purity.** The hooks must not mutate state and must be a pure function
  of `(terms, params)` — the sweep's validity rests on it. A component
  whose decision depends on anything not in its terms (a sampled or
  stateful ML policy) is not faithfully sweepable and must leave the
  hooks unimplemented, so it is skipped honestly rather than swept into
  fiction. The default hooks report "not scored", so a non-scoring policy
  (round-robin routing, FCFS scheduling, a cost-oblivious allocator)
  needs no implementation and is neither explained nor swept.
- **Faithfulness.** Replaying the recorded terms at the recorded params
  must reproduce the recorded decision; a sweep driver checks this as an
  anchor and refuses a session that fails it. This is the same
  decision-determinism contract the rest of the benchmark layer requires
  (seed the providers, or nothing is comparable).

`RoundRobinRouter` is the minimal non-scoring reference; `NoiseRouter`
and `NoiseGraphAllocator` show the scoring hooks, the latter with no
across-candidate normalisation (block cost S is directly comparable, so
`_sweep_rank` ranks on raw S).

---

---

## Device calibration model

A `QuantumDevice` carries the five calibration terms that characterise a
NISQ backend — the standard set an IBM Target publishes. A component reads
them through accessors, never by reaching into the raw maps, so the storage
can change without touching consumers:

| Term                    | Accessor              | Unit | Granularity     |
|-------------------------|-----------------------|------|-----------------|
| readout error           | `qubit_error(q)`      | prob | per qubit       |
| single-qubit gate error | `gate_error(q)`       | prob | per qubit       |
| two-qubit gate error    | `edge_error(u, v)`    | prob | per edge        |
| T2 coherence            | `t2(q)`               | µs   | per qubit       |
| gate duration           | `gate_duration(arity)`| ns   | per arity (1/2) |

To **enumerate** the coupling edges (rather than look up one), use
`edges()`, which returns the canonical `(u, v)` tuples (`u < v`); pair each
with `edge_error(u, v)` for its rate. This is the accessor a cost-scoring
component uses to walk the edges — the noise router and noise-graph
allocator both go through it, so neither reaches into the raw
`edge_error_map`.

Each accessor returns a **typical fallback** when its backing map is
unpopulated (a device built by older code, or a provider that could not
resolve a term), so every device answers every accessor and no consumer
has to special-case a missing term. Durations are **per-arity** scalars,
not per-qubit maps: execution-time estimation sums along a circuit's
critical path by gate arity, which is the granularity consumers actually
use. `gate_duration` accepts only arity 1 or 2 and raises otherwise —
higher-arity gates decompose to these before scheduling.

Providers populate these terms at construction: the IBM-simulated provider
extracts them from the Qiskit Target; the DevQ-simulated provider
synthesises them from real-world superconducting ranges, seeded for
determinism. **⚠ The extracted IBM values are bound to the pinned
qiskit-ibm-runtime calibration** — a version bump changes them, exactly as
it does the fidelity references.

### Adding a calibration term

This model is deliberately extensible along one uniform path. When a new
component needs a term DevQ does not yet carry (crosstalk, temporal drift,
leakage), add it the same way the existing five were added — do **not**
reach into device internals from the plugin:

1. Add a `<term>_map` field to `QuantumDevice.__init__` (defaulted, so
   existing construction is untouched).
2. Add a `<term>(...)` accessor mirroring `qubit_error`/`edge_error`, with
   a typical fallback.
3. Extract it in the IBM provider (from the Target) and synthesise it in
   the DevQ backend factory (from real-world ranges).
4. If it is a placement constraint a user should be able to impose, thread
   a `--max-<term>` filter through `eligible_qubits`/`edge_allowed` and the
   JobSpec path; if it is only a scoring/estimation input, leave it
   accessor-only (durations and T2 are accessor-only for this reason).

The five terms above are the ones the platform needed; the seam is what
makes the next one a bounded, additive change rather than a redesign.

## Device identity: index, name, kind

Three fields, three concepts. Conflating them is the source of a bug
class DevQ has hit twice, so they are kept strictly separate:

| Field   | Assigned by | Unique?           | Means                          |
|---------|-------------|-------------------|--------------------------------|
| `index` | kernel      | always            | *which* device — `d0`, `d1`    |
| `name`  | user        | yes, when present | what the user *calls* it       |
| `kind`  | provider    | **no**            | *what hardware* it is          |

```python
DevQ().add_devices([
    (IBMSimulatedProvider().get_device(backend_name="FakeNairobiV2"), "CustomName"),
    (IBMSimulatedProvider().get_device(backend_name="FakeNairobiV2"), "CustomName2"),
    IBMSimulatedProvider().get_device(backend_name="FakeNairobiV2"),
])
```

resolves to:

```
d0   customname    FakeNairobiV2
d1   customname2   FakeNairobiV2
d2   -             FakeNairobiV2
```

`kind` is **not** an identifier — all three devices above share one. Names
are lowercased, must be unique, and may not look like `dN` or shadow a
shell keyword. An unnamed device is addressed by index alone and renders
as `-`. A device that cannot report its hardware until a connection
resolves passes `kind=None` and calls `set_kind()` later; it renders as
`-` until then. **Never put a credential in `kind`** — it is displayed by
`qdevices` and written to every event-log record.

### Per-device state must be keyed by index

`get_device()` runs *before* the kernel exists, so a device has no index
at construction time. Providers holding per-device state therefore
implement `on_attach(device)`, which the kernel calls once, immediately
after stamping identity:

```python
def on_attach(self, device):
    self._sessions[device.index] = {...}   # index: unique
```

Keying on `kind` instead silently collapses every same-kind device onto
one shared slot, and the last device built wins. This is invisible until
two devices share a kind *and* differ in config — see the
`same_kind_isolation` block. Immutable, expensive resources (a loaded
backend) may still be cached by kind and shared; only mutable per-device
state needs the index.

`on_attach` defaults to a no-op, so providers with no per-device state
need not implement it.

---

## Providers and declarative devices

Registering a provider makes it addressable **by name**, which is what
lets devices be described in data rather than constructed in code:

```json
{"provider": "ibm.simulated", "backend": {"backend_name": "FakeNairobiV2"}}
```

**Provider names follow `vendor.variant` — a DevQ convention, not a
rule.** Schedulers, allocators and routers are named for what they *do*
(`packing`, `noise_graph`), so a bare name is already unambiguous. A
provider is named for whose hardware it speaks to, and a bare vendor
name quietly claims the whole vendor: once `ibm` means a simulator,
there is no honest name left for real hardware, and a published workload
spec reading `"provider": "ibm"` cannot tell a reader whether the
numbers came off a machine or off Aer. DevQ therefore ships
`devq.simulated` and documents `ibm.simulated` / `ibm.real`.

The registry does not enforce this — any non-empty string is a legal
name, and the dot carries no structural meaning (it is not parsed, split
or namespaced; provider names never appear as config keys, where a dot
*does* mean namespacing). A third party may name a provider whatever
suits them. The convention is offered because the reproducibility
problem it avoids is a real one, not because DevQ polices it.

The `backend` object is handed to `get_device_from_spec(spec)`, whose
default implementation splats it into `get_device(**spec)`. Override it
if your provider wants a different spec vocabulary or better errors than
a bare `TypeError`:

```python
class IonQProvider(BaseProvider):
    def get_device_from_spec(self, spec):
        if "qpu" not in spec:
            raise ValueError("IonQ device spec needs a 'qpu' key")
        return self.get_device(qpu=spec["qpu"])
```

It is deliberately not abstract — it has a working default, and making
it abstract would break providers written before it existed.

### The Qiskit-family base: `IBMProvider`

`BaseProvider` is vendor-neutral: it carries no qiskit dependency, not
even a deferred one. But DevQ's two IBM providers —
`IBMSimulatedProvider` (Qiskit V2 fake backends + an AerSimulator
noise-model run) and `IBMRealProvider` (live hardware via
`QiskitRuntimeService`) — share a lot precisely *because* they are
qiskit-family: both read a Qiskit `BackendV2` `Target`, and both place
circuits with a Qiskit `initial_layout`. That shared, qiskit-specific work
lives on an intermediate base, `IBMProvider(BaseProvider)`, which both
subclass:

- **`full_layout(qc, v2p_map, device)`** — the full-device-width layout
  builder described above. It returns a qiskit `Layout`, so it belongs on
  the qiskit-family base, not on `BaseProvider`.
- **The `_extract_*` calibration readers** — coupling map, readout error,
  2q-edge error, 1q-gate error, T2, gate durations, all read out of a
  `Target`. The work is identical whether the `Target` came from a fake
  backend or a real one, so it lives once on `IBMProvider` rather than
  being copied into each provider. Warning messages prefix
  `type(self).__name__`, so a fake-backend warning still reads
  `[IBMSimulatedProvider]` and a live one `[IBMRealProvider]`.

`IBMProvider` deliberately leaves `__init__`, `_load_backend`,
`get_device`/`on_attach`, `execute`, and `preferred_config` to the
subclass — those are where the sim and the real provider genuinely differ
(a fake-backend class vs `QiskitRuntimeService`, an AerSimulator run vs
`SamplerV2`). Qiskit is imported lazily inside the methods that need it,
so importing `ibm_provider` — or subclassing it — does not require qiskit
to be installed; a subclass that never calls those methods never triggers
the import.

The rule of thumb: a **non-qiskit** provider (IonQ, Braket, a photonic
backend) subclasses `BaseProvider` directly and never sees any of this. A
**new qiskit-family** provider subclasses `IBMProvider` and inherits the
`Target` readers and `full_layout` for free.

---