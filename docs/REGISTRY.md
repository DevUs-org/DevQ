# DevQ Component Registry

How to extend DevQ with your own scheduler, allocator, router or
provider — without editing DevQ core.

This is the reference for the extensibility surface. The formulas the
built-in policies implement are in [`COST_MODEL.md`](COST_MODEL.md); the
tests that pin this behaviour are in
[`TEST_BLOCKS.md`](TEST_BLOCKS.md#registry-and-plugin-extension).

---

## The short version

```python
from devq import DevQ
from registry.keyspec import KeySpec, NormaliseGroup, positive_int

class MyScheduler(BaseScheduler):
    LABEL = "My Scheduler"

    CONFIG_SCHEMA = {
        "mine.batch_window": KeySpec("device", 5, positive_int,
                                     "Batch window"),
    }

    def schedule(self):
        ...

devq = DevQ(config_path="my.config.json")
devq.register_scheduler("mine", MyScheduler)
devq.add_device(device)
devq.start()
```

With `{"scheduler": "mine", "mine.batch_window": 12}` in the config
file, your scheduler is constructed per device, `mine.batch_window`
rides the full configuration cascade, and both appear in `qconfig` with
their provenance. Nothing in DevQ core is edited or aware of your class.

---

## Why a registry

Before this existed, adding a scheduler meant editing three things in
two core files: the `_SCHEDULER_MAP` in `devq.py`, the `VALID_VALUES`
list in `config/config_loader.py`, and the label table beside it. Miss
the second and your scheduler is rejected as an invalid config value by
a file you never looked at.

The deeper problem was that the list of legal policy names was a
**hand-maintained duplicate** of what actually existed. The registry
collapses construction, validation and display into one fact: the set of
legal values for the `scheduler` key *is* the set of registered
schedulers, read live.

---

## Registration

Four methods on the `DevQ` object, all chainable:

```python
devq.register_scheduler("mine", MyScheduler)
devq.register_allocator("mine", MyAllocator)
devq.register_router("mine",    MyRouter)
devq.register_provider("ionq",  IonQProvider(api_key=KEY))
```

**Registration is instance-scoped.** Each `DevQ` object owns its own
registry. Two `DevQ` objects in one process do not share registrations,
and nothing leaks between them. There is no global state and no import-
time magic.

**Register before `build()` or `start()`.** Configuration is read at
build time; registering afterwards could not affect the system that was
built, so it raises `DevQError` rather than being silently ignored.
There is no constraint relative to `add_device()` — attach devices and
register components in whichever order suits you.

### Classes vs instances

| Kind | Accepts | Why |
|---|---|---|
| scheduler | class only | one is constructed **per device**, bound to that device's memory manager and queue |
| allocator | class only | same |
| router | class or instance | exactly one router per system, so sharing is safe |
| provider | class or instance | a provider may need credentials or a seed DevQ knows nothing about |

Registering a scheduler *instance* is refused, and this is a correctness
constraint rather than a style rule. A shared scheduler object would
merge the per-device queues that the multi-device federation exists to
keep separate — a system that appears to work and is quietly wrong.

Register a provider or router **instance** when it needs construction
arguments DevQ cannot supply:

```python
devq.register_provider("ionq", IonQProvider(api_key=KEY, region="us"))
```

A router registered as an instance keeps the weights you gave it; DevQ
does not overwrite them from config.

---

## What is checked at registration

A component that violates its contract is rejected **when you register
it**, not when it is eventually constructed several layers down. This is
deliberate: DevQ has been bitten twice by contract violations that
surfaced far from their cause.

| Level | Check |
|---|---|
| 1. Type | subclass (or instance) of the ABC for its kind |
| 2. Bind | `__init__` accepts exactly what DevQ will pass |
| 3. Methods | the methods DevQ calls exist and accept what DevQ passes |
| 4. Schema | declared keys are namespaced, legally scoped, and their defaults pass their own validators |
| 5. Groups | declared groups reference real keys, have ≥2 members, and agree with each member's `normalise_group` |

Level 2 uses `inspect.signature().bind()` rather than trying to
construct the object, so no user code runs and no side effects fire.

**Level 3 checks both halves of each template-method pair.** The kernel
calls `router.route()`, which is concrete on `BaseRouter` and delegates
to your abstract `select()`. Checking only `route()` would pass a plugin
whose `select()` has the wrong signature, because it inherits a valid
`route()`. The same applies to `allocate()`/`feasible()` and
`schedule()`/`enqueue()`.

### What DevQ passes your constructor

| Kind | `__init__` receives |
|---|---|
| scheduler | `(memory_manager, process_table)` positionally |
| allocator | `qubit_error_weight=`, `edge_error_weight=` |
| router | `router_queue_weight=`, `router_noise_weight=`, `qubit_error_weight=`, `edge_error_weight=` |
| provider | `seed=` |

Inheriting the base `__init__` satisfies all of these. If you define
your own, accept the same parameters — or register an instance, for the
kinds that allow it.

---

## Declaring configuration

A component contributes tunables by declaring `CONFIG_SCHEMA` as a class
attribute. Each entry is a `KeySpec`:

```python
KeySpec(scope, default, validate, label, normalise_group=None)
```

| Field | Meaning |
|---|---|
| `scope` | `"device"`, `"global"` or `"common"` — which cascade resolves it |
| `default` | the DevQ Core value; must pass your own validator |
| `validate` | callable returning `None` if acceptable, else a message |
| `label` | human name shown by `qconfig` |
| `normalise_group` | optional group name, see below |

One declaration buys the key everything: a place in the cascade,
validation, provenance tracking, and a `qconfig` line. There is no
second table to keep in step.

### Namespacing is mandatory

Plugin keys must be `prefix.key` — `qos.batch_window`, not
`batch_window`. Un-namespaced keys are reserved for DevQ core. This
stops two independent plugins colliding on a name like `window`, keeps
`qconfig` readable, and makes the plugin boundary visible in published
benchmark artifacts.

A namespaced key is not privileged for being namespaced: it is a legal
config key only once its owner is registered. Before that, it is an
unknown key like any other.

### Scopes

| Scope | Resolved |
|---|---|
| `device` | independently per device, through the full four-level cascade |
| `global` | once for the whole system |
| `common` | in **both** scopes independently |

Which scopes you may declare depends on your component kind, enforced at
registration:

| Kind | May declare |
|---|---|
| scheduler, allocator | `device`, `common` |
| router, provider | `global`, `common` |

A per-device scheduler declaring a system-wide key would be a scheduler
dictating global policy; a router declaring a per-device key would be
meaningless, since there is one router.

**Providers may never *set* a global key**, including one they declared
themselves. `preferred_config()` returning a global key is warned about
and ignored. Declaring a key and being entitled to set it are different
things: a provider expressing system-level policy is a layer violation.

### Validators

A validator is a plain callable returning `None` when the value is
acceptable, or a **string describing what was expected**:

```python
def even_int(value):
    if not isinstance(value, int) or isinstance(value, bool):
        return "expected an integer"
    if value % 2:
        return "expected an even integer"
    return None
```

The string completes the sentence
`... for 'key' from <source> — <message>. Ignoring.`

Message-on-failure rather than a bare `False` so that a validator which
can fail for several reasons reports the right one. A validator that
forgets to return `None` would reject every value a user ever supplied
while the default silently stood in — so the registry checks each key's
own default against its own validator and rejects the pair if they
disagree.

Stock validators in `registry.keyspec`: `positive_int`, `non_negative`,
`unit_interval`, `non_empty_string`, and `one_of(...)` for a fixed set
of literals.

Do **not** use `one_of` for policy names. A key whose legal values
depend on what is registered gets a registry-backed validator from the
loader, so registering a component makes its name legal immediately.

---

## Normalisation groups

When several keys only carry meaning as a **ratio** — cost weights,
blend factors — declare them as a group. The members are scaled to sum
to 1 after the cascade completes, so a user may write them on any scale:
`3/1`, `0.75/0.25` and `30/10` are equivalent.

```python
class MyScheduler(BaseScheduler):
    CONFIG_SCHEMA = {
        "mine.wait": KeySpec("device", 0.4, non_negative, "Wait weight",
                             normalise_group="mine.blend"),
        "mine.fid":  KeySpec("device", 0.6, non_negative, "Fidelity weight",
                             normalise_group="mine.blend"),
    }
    CONFIG_GROUPS = {
        "mine.blend": NormaliseGroup(["mine.wait", "mine.fid"]),
    }
```

Two declarations: each member names its group, and the group lists its
members. The registry checks they agree, that every member exists, and
that a group has at least two members — a one-member group would be
normalised to `1.0` whatever the user configured, and the only symptom
would be a quietly wrong benchmark number.

Groups are N-ary; three or more members work the same way. All members
must share one scope, since members in different scopes are resolved by
different passes and could never be scaled against each other.

**If every member resolves to 0**, the ratio is undefined and every
candidate would score identically — silently degrading the consuming
policy to "first candidate found". DevQ warns and reverts the whole
group to its declared defaults.

---

## Labels

Any component may define a `LABEL` class attribute, shown by `qconfig`
alongside the name:

```
scheduler          =  mine            [My Scheduler]  source: User (global)
```

Without one, the class name is used.

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

If your provider is stochastic, accept `seed=None` in `__init__`, call
`super().__init__(seed)`, and derive all randomness from a provider-local
generator — see [Reproducibility & Seeding](CONFIGURATION.md#reproducibility--seeding).

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
## The event log

The kernel emits structured records; **sinks** decide what to do with
them (`kernel/events.py`). The default is `PrintSink`, which renders the
console output DevQ has always produced — so an interactive session is
unchanged by the existence of events, and a new event kind is invisible
on the console until someone deliberately renders it.

```python
from kernel.events import PrintSink, RecordSink, MultiSink

records = RecordSink()
shell.kernel.sink = MultiSink(PrintSink(), records)   # print AND capture
```

A sink is anything with `emit(record)`. Sink calls are wrapped at two
levels — in `Kernel._emit` and in `MultiSink` — because observability
must never kill a job: a raising sink is reported once on stderr and
then ignored.

### Records

Six kinds: `submit`, `route`, `reject`, `dispatch`, `resolve`,
`cycle_end`. Every record carries `event`, `cycle` and `seq`, stamped
centrally in `_emit` so no call site can forget them or disagree about
the current cycle.

`route` records **all candidate scores**, not just the winner's, via the
router's `explain()`. The winner alone cannot answer how close the
decision was, so a weight sweep would need re-running; with scores, it
is answerable from one recorded run.

`cycle_end` is emitted even when a cycle did nothing, so a consumer can
distinguish an idle cycle from a cycle missing from the log.

### Two clocks

| Field | Deterministic? | Answers |
|---|---|---|
| `seq`, `*_seq` | yes | *what* happened, and in what order |
| `*_at` | no | *how long* it took |

`seq` is a monotonic event counter. Identical seeded runs make identical
decisions in identical order, so `seq`-keyed comparison is stable.
`*_at` is wall clock on the QCB (`submitted_at`, `dispatched_at`,
`resolved_at`), with `queue_latency`, `execution_time` and
`turnaround_time` derived from it.

**DevQ guarantees decision determinism, not completion-order
determinism.** Same seed gives the same routing, allocation and counts.
It does *not* give the same completion order: that belongs to the
executor, and on real hardware to the provider's queue, where jobs
submitted earlier routinely finish later. A log that hid this would be
misrepresenting what ran. Compare runs on `seq` with `*_at` excluded.

Two consequences for metrics. Cycle position is not a valid denominator
— a cycle is an artifact of polling frequency, not a physical quantity —
so throughput and utilisation come from timestamps and job counts.
And every derived timing returns `None` on a job that never dispatched,
so a metrics pass must skip rather than average in a fake zero.

Under simulation these measure Aer on the host CPU, not quantum runtime.
They are valid for comparing policies under identical conditions, and
must not be reported as device timings.

---

### Reporting scores: `explain()`

`select()` returns a winner; the margin behind it is discarded. Phase
5.5 sweeps the cost weights and asks how routing responds, and decisions
alone cannot answer that — every point where routing did not flip looks
identical to one where it nearly did. `explain(qcb, candidates)` is the
optional reporting hook the event log calls:

```python
def explain(self, qcb, candidates):
    scored = self._score_all(qcb, candidates, with_terms=True)
    return [{"device": ctx.index, "score": s, "terms": t}
            for s, _, ctx, t in scored]
```

It returns `None` by default, so a router with nothing to report — a
round-robin policy has no scores — needs no implementation, and
inventing numbers would be worse than reporting none.

Two requirements. **Share one scoring path with `select()`.** Two
parallel implementations drift, and a log that reports scores which did
not cause the decision is worse than no log. **Record raw terms, not
just totals.** `NoiseRouter` min-max normalises across the candidate
set, so a score is meaningful only relative to its peers in that one
decision; the raw pressure and cost are what allow a different weighting
to be re-derived from a recorded run instead of by re-executing.

Recomputation is cheap — roughly 0.05 ms per candidate, one allocator
dry-run — so `explain()` recomputes rather than caching. Caching would
buy nothing measurable and add a staleness failure mode where a
rejection logs the previous job's scores.

`explain()` must not mutate state: it runs only when logging is enabled,
so anything it changed would make logged runs diverge from unlogged ones.

---

base class handles rejection-reason aggregation. Keep `select()`
deterministic (break ties by lower device index). `RoundRobinRouter` is the
minimal reference; `NoiseRouter` shows how to reuse the per-device allocator
machinery for scoring.

---

---

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
{"provider": "ibm", "backend": {"backend_name": "FakeNairobiV2"}}
```

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

## Errors

Every violation raises `DevQError` from the `register_*` methods, with a
message naming the component, the specific rule broken, and where
applicable the offending signature. A few examples:

```
scheduler 'qos' (QOSScheduler) cannot be constructed by DevQ: __init__
must accept memory_manager, process_table, but binding them failed (got
an unexpected keyword argument 'memory_manager'). Its signature is
QOSScheduler(self).

scheduler 'qos' (QOSScheduler): config key 'window' must be namespaced
as '<prefix>.<key>' (for example 'qos.window'). Un-namespaced keys are
reserved for DevQ core.

scheduler 'qos' was registered as an instance, but schedulers must be
registered as a CLASS. DevQ constructs one scheduler per attached
device, each bound to that device's own memory manager and queue; a
shared instance would merge state across devices.
```

---

## Built-ins use this path

DevQ's own schedulers, allocators, routers and the DevQ simulated
provider are seeded into every new instance's registry through the same
public `register()` call a third party uses, with the same validation.
Nothing is privileged.

This is not stylistic. If the extension path breaks, every built-in
breaks at once and loudly, rather than the plugin path quietly rotting
while the shipped system keeps working.

The IBM provider is deliberately **not** seeded, since importing it
pulls in `qiskit-ibm-runtime`, an optional dependency. Register it
yourself if you need it addressable by name:

```python
from providers.ibm.ibm_simulated_provider import IBMSimulatedProvider
devq.register_provider("ibm", IBMSimulatedProvider(seed=42))
```