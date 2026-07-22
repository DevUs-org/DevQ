# DevQ Component Registry

How to extend DevQ with your own scheduler, allocator, router or
provider — without editing DevQ core.

This is the reference for the extensibility surface. The formulas the
built-in policies implement are in [`cost-model.md`](cost-model.md); the
tests that pin this behaviour are in
[`test_blocks.md`](test_blocks.md#registry-and-plugin-extension).

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
generator — see [Reproducibility & Seeding](configuration.md#reproducibility--seeding).

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

---

## Providers and declarative devices

Registering a provider makes it addressable **by name**, which is what
lets devices be described in data rather than constructed in code:

```json
{"provider": "ibm", "backend": {"name": "FakeNairobiV2"}}
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