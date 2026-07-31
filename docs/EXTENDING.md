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

## What to implement

Registration is how a component becomes addressable; this is what the
component itself must do. Each kind has a small contract, and a couple of
points below are load-bearing for correctness rather than style.

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

**New allocator** — subclass `BaseAllocator`, implement `allocate()` per the
documented contract (reserve via `pool.allocate()` on success; raise on
failure; honour thresholds as hard constraints). Every allocator is
constructed with the device's resolved cost weights
(`self.qubit_error_weight` / `self.edge_error_weight`, normalised to sum
to 1) — use them for cost scoring or ignore them freely. Optionally override
`feasible(circuit, device, max_qubit_error, max_edge_error, max_1q_gate_error)
→ None | reason`
— the base default checks eligible-qubit count; override it if your
allocator has stricter existence requirements (see the graph allocators'
connected-block check). `feasible()` powers both scheduler-level
classification and router-level candidate filtering.

**New scheduler** — subclass `BaseScheduler`, implement `schedule()`.

**New router** — subclass `BaseRouter`, implement
`select(qcb, candidates) → DeviceContext`. Candidates arrive already
filtered by the job's device constraints and per-device feasibility; the
**New frontend** — subclass `BaseFrontend`, implement
`parse(source) → CircuitRep`, and declare `EXTENSIONS` (lowercase, dotted)
for the source files it reads. A frontend takes **no constructor
arguments**: it is a stateless source-to-`CircuitRep` reader, so unlike
the other kinds DevQ injects nothing at construction. A knob, if one is
ever needed, is a namespaced `CONFIG_SCHEMA` key (the router precedent),
never a constructor argument.

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
them in one contract, `Sweepable` (`kernel/sweep.py`), which
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

---