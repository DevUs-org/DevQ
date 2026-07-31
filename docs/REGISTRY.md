# DevQ Component Registry

How to **register** a scheduler, allocator, router, provider or frontend
with DevQ, and how the registry validates it — without editing DevQ core.

This is the registration reference: naming a component, what is checked
when you register it, and declaring its configuration keys. It is **not**
where you learn to *build* a component — the contract each kind must
satisfy (what to implement, what is optional, what `Sweepable` means)
lives in [`EXTENDING.md`](EXTENDING.md). The event log a run produces is
in [`EVENT_LOG.md`](EVENT_LOG.md). The formulas the built-in policies
implement are in [`COST_MODEL.md`](COST_MODEL.md); the tests that pin this
behaviour are in
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

Five methods on the `DevQ` object, all chainable:

```python
devq.register_scheduler("mine", MyScheduler)
devq.register_allocator("mine", MyAllocator)
devq.register_router("mine",    MyRouter)
devq.register_provider("ionq",  IonQProvider)
devq.register_frontend("qasm3", QASM3Frontend)
```

**Registration is instance-scoped.** Each `DevQ` object owns its own
registry. Two `DevQ` objects in one process do not share registrations,
and nothing leaks between them. There is no global state and no import-
time magic.

**Register before `build()` or `start()`.** Configuration is read at
build time; registering afterwards could not affect the system that was
built, so it raises `DevQError` rather than being silently ignored.

**Register a provider before attaching a device it built.**
`add_device()` refuses a device whose provider class is not registered,
so for providers the ordering is fixed. Schedulers, allocators and
routers are unconstrained relative to `add_device()` — they are named in
config and resolved at build time, never carried in by a device.

### Everything is a class

| Kind | Why class-only |
|---|---|
| scheduler | one is constructed **per device**, bound to that device's memory manager and queue |
| allocator | same |
| router | constructed from the **config cascade**, so an instance would silently ignore it |
| provider | registration names a type; constructing it is the caller's business |
| frontend | DevQ constructs one per name and holds it as data for per-job dispatch; it is stateless and takes no constructor arguments |

One rule, no exceptions: **register the class, construct what you
attach.** Routers and providers once accepted a ready-made instance;
both turned out to be mistakes, and the reasons generalise.

**Schedulers and allocators** are class-only as a correctness
constraint, not a style rule. A shared scheduler object would merge the
per-device queues that the multi-device federation exists to keep
separate — a system that appears to work and is quietly wrong.

**A router instance silently defeated the config cascade.** DevQ builds
the router from the resolved global config, but a registered instance
was returned as-is, keeping whatever weights it was constructed with.
`qconfig` reports the cascade's values — so it would show one set of
weights while a different set was actually routing. That is exactly the
discrepancy `qconfig` exists to rule out, and it would have made Phase
5.5's weight sweep produce identical results at every weight while
appearing to vary. A router with knobs of its own declares **namespaced
config keys** (`mine.window`), which cascade, validate and appear in
`qconfig` — strictly better than constructor arguments.

**Providers are class-only because registering and constructing are
separate acts.** A registration establishes only that a name is legal
and what type it denotes. Building the provider — with an API key, an
endpoint, a seed, anything DevQ knows nothing about — is yours, and the
object you build is passed to `add_device()` directly:

```python
devq.register_provider("ionq", IonQProvider)
devq.add_device(IonQProvider(api_key=KEY, region="us").get_device(qpu="aria-1"))
```

So class-only costs nothing: **no credential ever has to reach the
registry.** What it buys is that a registered name denotes exactly one
thing. While providers could be registered as instances, an instance
carried its own seed, and a workload spec asking for a different one had
to be reconciled against it — a conflict with no correct answer, only a
documented winner.

The common thread is worth stating on its own: **a registered instance
is state that escaped the system's own resolution machinery.** Whatever
that machinery is for — per-device isolation, the config cascade, spec
seeding — an instance sits outside it and wins silently.

Matching is on the **exact type**. Registering a base class does not
bless its subclasses: a subclass is a different component with different
behaviour, so it registers under its own name.

---

## What is checked at registration

A component that violates its contract is rejected **when you register
it**, not when it is eventually constructed several layers down. This is
deliberate: DevQ has been bitten twice by contract violations that
surfaced far from their cause.

| Level | Check |
|---|---|
| 1. Type | a CLASS, and a subclass of the ABC for its kind |
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
your own, accept the same parameters — DevQ constructs every component
itself, so there is no instance escape hatch. Extra knobs of your own go
in namespaced config keys, which cascade and appear in `qconfig`.

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

scheduler 'qos' was registered as an instance, but every DevQ component
must be registered as a CLASS. Pass QOSScheduler itself, not
QOSScheduler(...).
DevQ constructs one scheduler per attached device, each bound to that
device's own memory manager and queue; a shared instance would merge
state across devices.
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
yourself — this is required before attaching any device it builds, not
merely to make it addressable by name in a spec:

```python
from providers.ibm.ibm_simulated_provider import IBMSimulatedProvider

devq.register_provider("ibm.simulated", IBMSimulatedProvider)
devq.add_device(IBMSimulatedProvider(seed=42).get_device("FakeNairobiV2"))