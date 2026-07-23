# AGENTS.md — Working in DevQ

Orientation for an LLM assistant helping someone use, extend, test or
develop algorithms in DevQ.

This file is a **router**, not a reference. Each section states the rules
that matter and points at the document holding the detail. Read the
linked document before writing code against a subsystem — the rules here
are deliberately compact and omit the reasoning.

Every section is self-contained. Nothing refers to "the section above".

---

## What DevQ is

An operating system layer for quantum execution. It sits between a
circuit and the hardware and makes two decisions:

1. **Which device should this job run on?** — the *router*, one per system.
2. **Which physical qubits should it use?** — the *allocator*, one per device.

A *scheduler* (one per device) decides queue order. Together these form
two-level scheduling: a pluggable router above per-device pluggable
schedulers and allocators, over a federation of heterogeneous backends.

Qubits on real hardware differ by up to an order of magnitude in error
rate, and only physically adjacent qubits can interact. Choosing well
matters, and DevQ makes that choice inspectable and replaceable.

The research purpose: competing scheduling and allocation algorithms have
never been comparable, because every paper builds its own harness. DevQ
lets them run as plugins in one system on identical workloads.

---

## Repository map

| Path | Contains |
|---|---|
| `devq.py` | The `DevQ` facade — attach devices, register components, `build()` / `start()` |
| `kernel/` | Kernel, device contexts, process table, schedulers, allocators, routers, memory |
| `providers/` | `BaseProvider` and the DevQ / IBM simulated providers |
| `registry/` | Component registry and the plugin-facing `KeySpec` declarations |
| `config/` | `ConfigLoader` — the four-level configuration cascade |
| `shell/` | QShell and the JobSpec parser |
| `circuits/` | Circuit representation, QASM loading, execution futures |
| `run_tests.py` | The whole test suite — 42 blocks, no pytest |
| `verify_local.py` | Run on YOUR machine: interactive shell, readline backend, real concurrency, pinned values |
| `docs/` | All reference documentation |

Documentation, all under `docs/`:

| Document | Read it when |
|---|---|
| `SHELL.md` | Driving a session; every command and the JobSpec grammar |
| `CONFIGURATION.md` | Config keys, the cascade, scopes, seeding |
| `REGISTRY.md` | Writing a plugin — contracts, `KeySpec`, validation |
| `COST_MODEL.md` | The maths behind routing and allocation scores |
| `TEST_BLOCKS.md` | What each test block proves and why |
| `MUTATION_TESTING.md` | Whether the tests would notice a regression — mutants run, killed, and the gaps they exposed |
| `ROADMAP.md` | What each phase delivered; where the project is going |

---

## Environment

Check `requirements.txt` for the authoritative pinned versions before
assuming anything. At time of writing the stack is pinned to
`qiskit` / `qiskit-aer` / `qiskit-ibm-runtime`, and **fake-backend
calibration data is tied to the `qiskit-ibm-runtime` version** — every
reference value in `docs/TEST_BLOCKS.md` assumes the pinned one. A
version bump silently changes expected noise numbers.

Two traps that waste time if unknown:

- **IBM fake backend names require the `V2` suffix.** `FakeNairobiV2`,
  not `FakeNairobi`. Small 5–7 qubit V2 backends: Athens, Belem, Bogota,
  Burlington, Casablanca (7), Essex, Jakarta (7), Lagos (7), Lima,
  London, Manila, Nairobi (7).
- **On Debian-family systems** `pip install` needs
  `--break-system-packages`, and PyJWT may additionally need
  `--ignore-installed`.

---

## Task: run a session

```python
from devq import DevQ
from providers.devq.devq_simulated_provider import DevQSimulatedProvider

DevQ(DevQSimulatedProvider().get_device("random", 10)).start()
```

`start()` opens the interactive shell. For programmatic control use
`build()`, which returns the shell without entering its loop:

```python
shell = DevQ(config_path="my.json").add_device(device, name="nairobi").build()
shell.onecmd("qsubmit test_circuits/bell.qasm")
shell.onecmd("qrunpack")
```

**`build()` must be left non-interactive.** `build(interactive=False)` is
the default and is correct for any non-interactive front end; passing
`interactive=True` enables shell history, which is only appropriate for a
real terminal session. Call `shutdown_executor()` between sessions in the
same process.

Commands, briefly — full reference in [`docs/SHELL.md`](docs/SHELL.md):

| Command | Does |
|---|---|
| `qsubmit` | Enqueue jobs (accepts per-job flags, see below) |
| `qrun` | Execute one job immediately, bypassing the queue |
| `qrunpack` | Drain all queues via the router and per-device schedulers |
| `qps` | All jobs with device binding and lifecycle state |
| `qmap <job_id>` | A job's device and virtual → physical qubit mapping |
| `qdevices` | Attached devices, load, qubit counts |
| `qmem`, `qtopology`, `qerrors` | Free/allocated qubits, coupling map, error rates |
| `qconfig` | Every active config value **with the source it came from** |

`qconfig` is the first thing to check when behaviour is surprising: it
shows which cascade level supplied each value.

Job lifecycle states: `READY`, `RUNNING`, `WAITING`, `FINISHED`,
`FAILED`, `REJECTED`. `WAITING` is transient (resources unavailable now);
`REJECTED` is terminal (no device can ever satisfy the request).

---

## Task: constrain a job

Per-job noise thresholds and device constraints are set with flags on
`qsubmit`. Full grammar in [`docs/SHELL.md`](docs/SHELL.md).

```
qsubmit bell.qasm --max-qubit-error=0.05
qsubmit bell.qasm --exec=d1,d2
qsubmit [a.qasm b.qasm --max-qubit-error=0.05 --no-exec=d0] c.qasm
```

Two rules that are easy to get wrong:

- **Trailing flags bind only to the job immediately before them.**
- **Bracketed groups apply flags to every job in the group**, and flags
  never leak across a group boundary.

Thresholds are floats in `[0, 1]`. Device references match `d<int>` or a
name assigned at attach time. `--exec` and `--no-exec` are mutually
exclusive. Malformed input is rejected with an error and no job is
created.

---

## Task: configure DevQ

Configuration is JSON, resolved through a cascade. Detail in
[`docs/CONFIGURATION.md`](docs/CONFIGURATION.md).

Three scopes:

| Scope | Resolved | Examples |
|---|---|---|
| `device` | Independently per device, four levels | `scheduler`, `allocator`, `shots` |
| `global` | Once for the whole system, two levels | `router`, router weights |
| `common` | In **both** scopes independently | `qubit_error_weight`, `edge_error_weight` |

Device cascade, later levels winning: core defaults → the device's
provider `preferred_config()` → global user file → per-device user file.

Rules worth stating outright:

- **An unknown config key is a warning and is ignored**, never a silent
  acceptance.
- **A provider may never set a `global` key.** A provider expressing
  system-wide policy is a layer violation; it is warned about and
  dropped, including for a key that provider declared itself.
- **Weight pairs are normalised to sum to 1 after the cascade.** Only
  their ratio matters, so `3`/`1` and `0.75`/`0.25` are equivalent. If
  every member of a group resolves to `0`, the group reverts to defaults
  with a warning.
- **Legal values for `scheduler` / `allocator` / `router` are whatever is
  registered**, not a fixed list. Registering a component makes its name
  legal immediately.

---

## Task: write a plugin

Full contract in [`docs/REGISTRY.md`](docs/REGISTRY.md) — read it before
writing a component. The essentials:

```python
from registry.keyspec import KeySpec, NormaliseGroup, positive_int

class MyAllocator(BaseAllocator):
    LABEL = "My Allocator"
    CONFIG_SCHEMA = {
        "mine.window": KeySpec("device", 5, positive_int, "Window"),
    }

    def allocate(self, circuit, device, pool,
                 max_qubit_error=None, max_edge_error=None):
        ...

devq = DevQ(config_path="my.json")
devq.register_allocator("mine", MyAllocator)
devq.add_device(device)
devq.start()
```

With `{"allocator": "mine", "mine.window": 12}` in the config file, the
allocator is constructed per device and its key rides the full cascade.

**Do not edit DevQ core to add a component.** There is no map to append
to. If a change to `registry/registry.py` seems necessary to register
something, the design has been misunderstood — re-read
[`docs/REGISTRY.md`](docs/REGISTRY.md).

Rules the registry enforces at registration time, before anything is
constructed:

- **Schedulers and allocators must be registered as CLASSES**, never
  instances. DevQ constructs one per device, bound to that device's own
  memory manager and queue; a shared instance would merge state across
  devices. Routers and providers may be either, since there is one router
  per system and a provider may need credentials DevQ cannot supply.
- **Plugin config keys must be namespaced** — `mine.window`, not
  `window`. Un-namespaced keys are reserved for DevQ core.
- **Scope is restricted by kind.** Schedulers and allocators may declare
  `device` or `common` keys; routers and providers may declare `global`
  or `common`.
- **A key's default must satisfy its own validator.**
- **Validators return `None` when the value is acceptable**, or a string
  saying what was expected. Not a boolean.
- **Register before `build()` or `start()`.** Afterwards the registry is
  frozen and registration raises.

**Implement the hook, not the template method.** Three of the four kinds
have a concrete method that delegates to the one a plugin overrides:

| Kind | DevQ calls | You implement |
|---|---|---|
| router | `route()` | `select()` |
| allocator | `allocate()` | `allocate()`, optionally override `feasible()` |
| scheduler | `schedule()` | `schedule()`, using the base's `_attempt_allocation()` |

`_attempt_allocation()` performs the shared allocate-and-classify step —
it sets the mapping and `RUNNING` on success and classifies failure as
`WAITING` or `REJECTED`. A scheduler that reimplements it instead of
calling it will silently skip lifecycle transitions.

---

## Task: compare algorithms

This is what DevQ is for. The current workflow:

1. Implement the competing policies as plugins and register them.
2. Write one config file per combination.
3. Run identical circuits through each and compare.

`run_tests.py`'s `plugin_matrix` block does exactly this across every
registered combination and is the best worked example in the repository.

Two facts that determine whether a comparison is meaningful:

- **Seed the providers.** `DevQSimulatedProvider(seed=42)` makes a run
  reproducible; without a seed, results differ between runs and nothing
  is comparable. See [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md).
- **Reference noise values are tied to the pinned
  `qiskit-ibm-runtime`.** Comparing numbers produced under different
  versions is invalid.

The cost model behind routing and allocation decisions — what the weights
mean and how scores are computed — is in
[`docs/COST_MODEL.md`](docs/COST_MODEL.md). Read it before interpreting
any result.

A headless benchmark runner with a declarative workload spec and a
structured event log is the next phase of work and does not exist yet;
check `docs/ROADMAP.md` for current status before assuming a `qbench`
command is available.

---

## Task: run or extend the tests

```bash
python run_tests.py              # all blocks; exit 0 only if all pass
python run_tests.py --list       # names and descriptions
python run_tests.py -k PATTERN   # a subset
python run_tests.py -c           # every assertion with its reasoning
python run_tests.py -v           # commands plus full session transcripts
```

There is no pytest. `run_tests.py` is self-contained: each block is a
function that drives a real shell session and raises on failure.

Detail on every block: [`docs/TEST_BLOCKS.md`](docs/TEST_BLOCKS.md).

`run_tests.py` runs headless and always uses `build(interactive=False)`,
so it never exercises the interactive shell or readline. Run
`python verify_local.py` on the target machine for that, plus real
concurrency and the pinned calibration values — three past bugs were
invisible in a 1-CPU sandbox and only appeared under macOS/libedit.

Before trusting a green suite, see
[`docs/MUTATION_TESTING.md`](docs/MUTATION_TESTING.md): three mutants
survived a fully green run, and each exposed a test that was asserting
nothing.

Rules for adding a block — all four are load-bearing:

- **Mutation-test any new block.** Break the code deliberately and
  confirm the suite goes red; a green test that cannot fail is worse
  than no test. Record the result in `docs/MUTATION_TESTING.md`.
- **Keep `docs/TEST_BLOCKS.md` 1:1 with the block list.** One documented
  section per block, matching names exactly. Verify programmatically; the
  invariant has caught drift before.
- **Mutation-test every new block before trusting it.** Break the code
  the block is meant to protect and confirm the block fails. A test that
  cannot fail is worse than no test, because it creates false confidence.
  This has repeatedly caught assertions that passed regardless of the
  behaviour they claimed to check.
- **Assert against resolved state, not rendered output**, when checking
  whether something is absent. Output renders only what it iterates over,
  so "absent from the display" can be true even when the thing leaked.
- **Make test doubles observably different from the built-ins.** A mock
  scheduler that behaves like the default proves nothing: the block will
  pass even if the plugin was ignored entirely. Give it a distinctive,
  assertable behaviour.

Blocks record *what they proved*, not merely that they passed. Use
`check(condition, description)` so `-c` output reads as evidence.

---

## Conventions

- **Docstrings use `'''`** and open with a `Tags:` line. Existing tag
  values: `Main`, `Provider`, `Alt`, `Default`. Do not invent new ones.
- **Comments explain *why*, not *what*.** The codebase documents
  reasoning and rejected alternatives; match that.
- **`LABEL` on a component** is its display name in `qconfig`. Without
  one the class name is used.
- **`docs/` is authoritative alongside the README.** If code and a
  document disagree, that is a bug in one of them — say so rather than
  guessing which.

---

## Things that are easy to get wrong

| Symptom | Cause |
|---|---|
| Config value ignored, warning about an unknown key | Plugin key not namespaced, or its component not registered yet |
| `register_*` raises | Called after `build()` / `start()`; the registry is frozen |
| Scheduler or allocator rejected at registration | Passed an instance; these must be classes |
| Plugin appears registered but never runs | Config file does not name it, or names a different key |
| Job stuck in `WAITING` | Resources unavailable now — distinct from `REJECTED`, which is terminal |
| Noise numbers differ from documented reference values | `qiskit-ibm-runtime` version differs from the pinned one |
| Backend not found | Missing the `V2` suffix on an IBM fake backend name |
| Results differ between identical runs | Provider constructed without a seed |
| Event logs differ between identical seeded runs | Expected — completion order belongs to the executor. DevQ guarantees decision determinism, not completion-order determinism. Compare on `seq`, exclude `*_at` |
| Spec's seed appears to be ignored | A provider *instance* was registered with its own seed — instances win over specs, and the run warns. Register the class instead |
| Log flooded with `cycle_end` records | The drain loop is stepping while futures are merely in flight. Step only when a cycle can make progress |
| A metrics pass crashes on a rejected job | Reading `queue_latency`/`execution_time`/`turnaround_time` without checking for `None` — unfinished jobs have no timestamps |
| Two same-kind devices behave as one | Provider keyed per-device state by `kind` (shared) instead of `device.index` (unique) — see `on_attach` in docs/REGISTRY.md |
| `device.index` is `None` | Device not attached yet; `get_device()` runs before the kernel assigns identity |
| Device shows `-` where hardware should be | `kind` unresolved — provider has not called `set_kind()` |