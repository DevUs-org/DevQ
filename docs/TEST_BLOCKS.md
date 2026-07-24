# DevQ Sanity Test Plan

Specification for the 46 sanity blocks in `run_tests.py`, covering
Phases 0–5.1 plus the component registry.

`run_tests.py` asserts **what** each block expects. This document
records **why** those values are correct — the S-cost arithmetic behind
a mapping, the reason a device is rejected, the physics behind an error
rate. When a block fails, the assertion tells you something changed;
this tells you whether the change was a regression or an improvement.

## Running

```bash
python run_tests.py              # all 46 blocks, one line each
python run_tests.py --list       # block names and descriptions
python run_tests.py -k single    # only blocks matching a pattern
python run_tests.py -c           # every assertion each block verified
python run_tests.py -v           # commands + full session transcript
```

Default output is one line per block. `-c` / `--checks` expands that
into the individual assertions, each stating the fact it proved:

```
noise_routing
  Noise-aware routing picks Nairobi; Lagos mappings are correct

  checks
    [PASS] job 1 routed to nairobi (S 0.0102 < lagos 0.0249), got nairobi
    [PASS] job 1 mapped to nairobi's best bell block {0: 1, 1: 2}, ...
    ...
  → PASS (5/5 checks)
```

`-v` additionally prints the commands sent and the raw session
transcript — the same output you would see typing them into
`example.py` by hand. Use it when a block fails and you want to read
what the shell actually said, or when adding a block and you want to
confirm it exercises what you think it does.

Run from the project root — circuit paths are relative. Exit status is
0 only when every block passes, so `python run_tests.py && git push`
works as a pre-push gate.

The full suite takes a few minutes, most of it `plugin_matrix` running
36 Aer simulations. While iterating, `-k <block>` is much faster.

The runner calls `shutdown_executor()` between blocks and once more
before returning. `submit_async`'s shared `ThreadPoolExecutor` uses
non-daemon workers, so idle threads left behind by ~30 sessions would
otherwise be joined at interpreter exit — the process appears to hang
after printing its final line. Anything else that builds many sessions
in one process should do the same.

Each block builds its own session through `DevQ.build()`, which returns
a wired `QShell` without entering the command loop, and drives it with
`shell.onecmd(...)`. Nothing needs editing to run a block, and blocks do
not share state.

## Reference session

Most multi-device blocks use this federation, mirroring `example.py`:

| Device | Backend | Provider | Qubits |
|---|---|---|---|
| `d0` *(unnamed)* | `random_backend` | `DevQSimulatedProvider` | 7 |
| `nairobi` (`d1`) | `fakenairobiv2` | `IBMSimulatedProvider` | 7 |
| `lagos` (`d2`) | `fakelagosv2` | `IBMSimulatedProvider` | 7 |

Providers are seeded (`SEED = 42`) so `d0`'s generated topology never
flaps between runs.

**Calibration reference.** Every mapping assertion below derives from
these values, which come from `qiskit-ibm-runtime` 0.45.1. If they
change, the pinned stack changed — check `requirements.txt` before
suspecting the code.

Nairobi qubit errors:

| Qubit | 0 | 1 | 2 | 3 | 4 | 5 | 6 |
|---|---|---|---|---|---|---|---|
| | 0.0580 | 0.0199 | 0.0193 | 0.0223 | 0.0183 | 0.0225 | 0.0258 |

Nairobi edge errors:

| Edge | (0,1) | (1,2) | (1,3) | (3,5) | (4,5) | (5,6) |
|---|---|---|---|---|---|---|
| | 0.0086 | 0.0070 | 0.0068 | 0.0126 | 0.0070 | 0.0107 |

Lagos qubit errors:

| Qubit | 0 | 1 | 2 | 3 | 4 | 5 | 6 |
|---|---|---|---|---|---|---|---|
| | 0.1690 | 0.1362 | 0.4638 | 0.0167 | 0.0292 | 0.2619 | 0.3480 |

Lagos edge errors:

| Edge | (0,1) | (1,2) | (1,3) | (3,5) | (4,5) | (5,6) |
|---|---|---|---|---|---|---|
| | 0.0094 | 0.0103 | 0.0107 | 0.0290 | 0.0083 | 0.0202 |

**Device references.** Named devices are addressable both ways — `d1`
and `nairobi` are the same device in `--exec`/`--no-exec` and in every
device-scoped command. Blocks mix the forms deliberately. Named devices
display as `nairobi (d1)`; unnamed ones as `d0`.

The cost function `S` used throughout is defined in
[`COST_MODEL.md`](COST_MODEL.md).

---

## Federation blocks

### `devices_and_config`

*Devices, alias column, calibration data and config provenance.*

```
qdevices    qconfig    qerrors q d2    qerrors e d2    qtopology d1 1
```

Checks the session reports itself correctly before any job runs. Three
devices attach in order; `qdevices` grows an **alias column** because at
least one device is named (`d0` shows `-`).

`qconfig` must show provenance for every value, not just the value:
`router = noise` from User (global), scheduler and allocator from DevQ
Core, and `shots` from `IBMSimulatedProvider` on `d1`/`d2` but DevQ Core
on `d0` — evidence the four-level cascade resolves per device rather
than globally.

The `qerrors` assertions pin all 13 Lagos calibration figures. This is
the canary for the whole suite: if these drift, every mapping assertion
downstream is measuring a different machine.

`qtopology d1 1` must show only `0--1`, `1--2`, `1--3` — qubit 1's
edges — and *not* `4--5` or `5--6`. Filtering is by incidence, not
proximity.

### `noise_routing`

*Noise-aware routing picks Nairobi; Lagos mappings are correct.*

```
qrun bell --exec=nairobi,lagos    qrun bell --exec=d2    qrun ghz --exec=d2
```

The core routing claim. Job 1 may use either device, and the router
picks Nairobi because its best bell block scores lower:

| Device | Best block | S |
|---|---|---|
| nairobi | `{1,2}` | **0.0102** |
| lagos | `{1,3}` | 0.0249 |

Jobs 2 and 3 are pinned to Lagos to assert its allocator independently:
bell → `{0:1, 1:3}`, ghz → `{0:3, 1:4, 2:5}`.

**Margin worth knowing.** Nairobi's `{1,2}` at 0.0102 beats `{1,3}` at
0.0103 by 0.0001. The choice is correct but nearly tied, which is
exactly why `weight_normalisation` can flip it by re-weighting. A future
change that flips this mapping is not necessarily a bug — check the
weights first.

### `name_index_equivalence`

*A device name and its index are interchangeable everywhere.*

Runs `qerrors q nairobi` / `qtopology nairobi 1` and asserts the output
is **byte-identical** to the `d1` forms, then submits the same circuit
under `--exec=nairobi` and `--exec=d1` and asserts identical routing and
mapping.

A name is an alias, never a replacement. This block exists because the
shell resolves names in two structurally different places — positional
device tokens and comma-separated flag lists — and either could regress
alone.

### `name_validation`

*Ambiguous or duplicate device names are rejected at attach time.*

Table-driven; each name must raise `DevQError` during `add_device`,
never at use time:

| Rejected | Reason |
|---|---|
| `d0`, `d7` | would be ambiguous with a device index |
| `q`, `e`, `b` | shadow `qerrors` subcommand arguments |
| `""`, `"   "` | empty after stripping |
| `has space` | whitespace breaks token splitting |
| `has,comma` | commas separate `--exec` lists |
| `alpha` + `ALPHA` | duplicate, compared case-insensitively |

Failing at attach time matters: a name accepted here but ambiguous later
would produce a confusing mid-session error, or silently resolve to the
wrong device.

### `rejection_semantics`

*Thresholds reject across devices with aggregated reasons.*

```
qrun bell --max-qubit-error=0.03 --exec=lagos
qrun bell --max-qubit-error=0.03 --exec=d1,d2
qrun bell --max-qubit-error=0.0185 --exec=nairobi,lagos
```

Three outcomes that must stay distinct:

- **Job 1 REJECTED.** On Lagos only qubits 3 (0.0167) and 4 (0.0292)
  pass 0.03, and they are **not adjacent** — the reason must say
  *no connected block*, a topology failure, not a count failure.
- **Job 2 runs on Nairobi.** Same threshold, but Nairobi has many
  qualifying adjacent qubits. Proves rejection is per-device, and a job
  infeasible somewhere is not globally rejected.
- **Job 3 REJECTED with both devices named.** At 0.0185 only Nairobi
  qubit 4 (0.0183) and Lagos qubit 3 (0.0167) qualify. The reason must
  aggregate `d1: ...` *and* `d2: ...`, so a user sees why every allowed
  device failed rather than just the first.

### `edge_threshold_semantics`

*`--max-edge-error` filters by coupling quality, independently of qubits.*

The qubit-side threshold gets exercised by `rejection_semantics`; this is
its sibling, and the two filters are genuinely independent code paths in
`filtering.py`.

Nairobi's edges make the assertion sharp: `(1,3)` at 0.0068 is the only
one at or below a 0.0069 threshold, so the allocator is forced off its
default `{1,2}` (edge 0.0070) onto `{0:1, 1:3}`. A mapping change driven
purely by edge quality is the proof the filter is applied.

A 0.005 threshold sits below every edge on both devices, producing a
rejection whose reason reads `max_qubit_error=None, max_edge_error=0.005`
— an edge-only failure with no qubit threshold involved.

### `combined_thresholds`

*Qubit and edge thresholds compose as independent hard filters.*

Thresholds are **ANDed, never traded off**. Two cases:

- `--max-qubit-error=0.03` with `--max-edge-error=0.0069` on Nairobi is
  jointly satisfiable — qubits 1 and 3 pass, and so does edge `(1,3)` —
  giving `{0:1, 1:3}`.
- `--max-qubit-error=0.0185` with a generous `--max-edge-error=0.05` is
  rejected, and the reason cites the *qubit* threshold. A satisfiable
  edge constraint must not rescue an impossible qubit one.

### `packing_across_devices`

*Bracket groups, batch packing and cross-device concurrency.*

```
qsubmit [bell bell ghz --no-exec=d0] ghz --exec=lagos
qrunpack    qps    qmap 1    qmem
```

Group flags apply to all three bracketed jobs; the trailing job is
pinned separately.

Jobs 1 and 2 pack into the **same cycle** on disjoint blocks — `{1,2}`
and `{4,5}` — which is the packing scheduler's whole purpose. Job 3
needs three connected qubits and cannot fit alongside them, so it waits
a cycle and allocates once qubits free up. Job 4 goes to Lagos.

**This block deliberately does not assert job 3's exact mapping.** Which
qubits are free when it retries depends on async completion order, so
the assertion is the invariant: it lands on Nairobi with three qubits.
An earlier version hardcoded `{0,1,2}` and passed alone but failed in
full-suite runs — a flaky assertion, not a flaky scheduler.

`qmem` at the end must show no `[X]` markers: every qubit returned to
its pool.

### `parser_errors`

*Malformed commands are rejected atomically, creating no jobs.*

Five malformed submissions, each producing a specific message:

| Input | Error |
|---|---|
| `--exec=d5` | device out of range (3 attached) |
| `--exec=d0 --no-exec=d1` | flags mutually exclusive |
| `--exec=[d0,d1]` | brackets reserved for grouping |
| `--exec=sherbrooke` | unknown name, message lists attached names |
| `nofile.qasm bell.qasm` | bad path kills the **whole** batch |

The closing `qps` must report `No jobs.` — the atomicity claim. A
partially-applied batch is worse than a rejected one, since the user
would have to work out which half landed.

### `round_robin_router`

*Round-robin router cycles devices in index order.*

Uses `round_robin.config.json`. Three identical bells must land on `d0`,
`d1`, `d2` in that order — the rotation is noise- and load-oblivious by
design, so identical jobs still spread.

Contrast with `noise_routing`, where all three would go to Nairobi. That
difference is the point: it proves the router is genuinely pluggable
rather than the noise policy being hardcoded.

### `per_device_config`

*A per-device config overrides only that device.*

Attaches `d1.static.config.json` to Nairobi alone. `qconfig d1` must
show `allocator = static` and `shots = 512` sourced from User (d1),
while `scheduler` stays packing from DevQ Core — level 4 overriding
levels 1–3 key by key, not wholesale.

The behavioural proof is the mapping: Static picks the **first free
block** `{0:0, 1:1}` (S = 0.0155) rather than noise_graph's `{0:1, 1:2}`
(S = 0.0102). Static ignores noise by design, so a *worse* mapping here
is the correct result.

### `weight_normalisation`

*Cost weights normalise, and edge-only weighting changes the mapping.*

Global `weights_1_9.config.json` (raw `1`/`9`) plus
`d1.edge_only.config.json` on Nairobi (`0`/`1`).

`qconfig d2` must report 0.1 / 0.9: the raw pair is **normalised to sum
to 1** at resolution, so `1`/`9`, `0.1`/`0.9` and `2`/`18` are
equivalent. Only the ratio matters.

The behavioural half — with edge-only weighting on Nairobi, ranking by
edge error alone:

| Block | Edge error | Rank |
|---|---|---|
| `{1,3}` | 0.0068 | **1st** |
| `{1,2}` | 0.0070 | 2nd |

So job 1 maps to `{0:1, 1:3}`, flipping the default choice. Lagos is
unchanged at `{0:1, 1:3}` because 1/9 has the same ratio as the default
— the same weights expressed differently must not change behaviour.

### `zero_weight_fallback`

*Both weights zero warns and falls back to core defaults.*

A both-zero pair cannot be normalised (division by zero) and would make
every block score identically. Config resolution emits a
`[Config] Warning` and falls back to 0.1 / 0.9.

The warning is printed during `build()`, before any command runs, so
this block captures construction as well as command output — worth
knowing if you add similar blocks.


### `wedged_provider_timeout`

*A future that never resolves fails cleanly instead of hanging.*

`Kernel._wait_for` polls until a job's future resolves. Before Phase 5.1
it had no exit condition, so a wedged provider or a dead executor would
spin forever — the shell appeared to hang with no diagnosis. It now
carries a 300s deadline.

The block substitutes a future whose `done()` is permanently `False` and
drives the `qrun` path directly, so the timeout can be set to 1s rather
than the production deadline. It asserts the job ends `FAILED` with a
message naming the cause, and — the part that matters — that the same
cleanup invariants hold as for an ordinary failure: qubits returned,
`running_jobs` decremented. A wedged provider must not permanently shrink
the device it wedged on.

### `config_validation`

*Malformed configs warn and fall back rather than crashing.*

Seven table-driven cases, each asserting a specific warning **and** that
a usable session was still built:

| Case | Expected warning |
|---|---|
| file does not exist | `not found` |
| invalid JSON | `is not valid JSON` |
| JSON array, not object | `is not a JSON object` |
| unknown key | `unknown config key` |
| `shots: "many"` | `expected a positive integer` |
| `scheduler: "nonexistent"` | `expected one of` |
| negative weight | `expected a non-negative number` |

This is the path a **new user** hits first when writing their own config,
and the design commitment is that a bad config degrades to defaults
rather than killing the session. Each case also asserts `DevQ Core`
appears in `qconfig`, proving the fallback actually took effect rather
than the bad value being silently adopted.

Warnings are emitted during `build()`, so the block captures construction
as well as command output.

### `provider_global_key`

*A provider may not set global-scope config keys.*

A subclassed provider returns `{"shots": 2048, "router": "round_robin"}`
from `preferred_config()`. `shots` is a device key it owns; `router` is
global scope and off-limits.

The block asserts all three consequences: the warning names the illegal
key, the legitimate device key is still honoured (2048 shots), and the
router stays `noise`. Without the last assertion a provider could
silently override global policy — a scope violation that would be very
hard to trace from a benchmark result.


### `lifecycle_waiting`

*WAITING is a distinct, reachable state for transient contention.*

`WAITING` was deliberately kept separate from `READY` in Phase 3, and the
distinction only matters if something asserts it. The block occupies the
pool directly, leaving one free qubit, then submits a two-qubit job via
`qrun`.

Routing still succeeds — `feasible()` ignores pool state — so the job is
**contended, not unsatisfiable**, and must land in `WAITING` rather than
`REJECTED`. That difference is the whole point: `REJECTED` is terminal,
`WAITING` is retried.

The block then frees the pool and runs `qrunpack`, asserting the same job
completes. A `WAITING` job that could never resume would be a
`REJECTED` job wearing the wrong label.

### `lifecycle_failed`

*A provider error yields FAILED and still returns the qubits.*

The provider's `execute` is swapped for one returning a failed
`ExecutionResult`. `FAILED` is otherwise unreachable — every real
provider in the tree succeeds — so this is the only coverage of the
error path in `_resolve_pending`.

Beyond the state itself, two invariants matter more:

- **all qubits return to the pool** — a failed job that strands its
  allocation silently shrinks the device for the rest of the session
- **`running_jobs` is decremented** — otherwise the router's queue
  pressure term drifts upward forever, quietly biasing every future
  routing decision

Both are asserted directly against the pool and context, not through
printed output.


### `mock_topologies`

*Every mock topology kind builds a usable device.*

`create_backend` supports four kinds; before this block only `random` and
`fully_connected` were ever constructed. Edge counts are asserted
structurally: linear on 7 qubits gives 6 edges, fully-connected gives 21
(C(7,2)), and a 3×3 grid gives 12. Each kind also runs a job end to end,
so a topology that builds but cannot host a circuit still fails.

The block additionally checks that error maps cover every qubit and every
edge — a topology whose error map disagrees with its coupling map would
make allocator scoring silently wrong.

### `backend_factory_errors`

*Invalid backend requests fail loudly at construction.*

Four cases that must raise rather than produce a degenerate device:
fewer than 2 qubits, an unknown kind, a non-square qubit count for
`grid`, and an unknown IBM fake backend name. Each asserts on the message
fragment, since a `ValueError` with an unhelpful message is nearly as bad
as no error — these are the messages a researcher sees when they typo a
backend name.


### `shell_input_handling`

*Malformed or empty commands are handled without crashing.*

Seven bad inputs in one session: `qrunpack` with nothing queued, `qmap`
on a nonexistent job, `qmap` with a non-numeric id, `qmem` on an
out-of-range device, `qtopology` with an out-of-range qubit, `qerrors`
with an invalid flag, and bare `qrun` with no argument.

Each must produce its specific message. The block then asserts **no jobs
were created** and that a normal `qrun` still succeeds afterwards — the
real risk is not a bad message but a session left in a broken state.

### `many_device_federation`

*Routing and indexing hold beyond the usual three devices.*

Every other block uses one or three devices. This one attaches five —
four named, one deliberately unnamed — to exercise index and name
resolution over a longer list.

Two assertions: `--exec=jakarta` resolves the fourth named device, and a
four-way deny-list (`--no-exec=nairobi,lagos,casablanca,jakarta`) leaves
only the unnamed `d4` as a candidate. The second case is the interesting
one — it mixes name-based exclusion with index-based fallback, which is
where an off-by-one in resolution would surface.

---

## Single-device blocks

No routing decisions exist with one device, so these cover the path
where the router is effectively a pass-through. They also guard a
specific trap: **the only device is `d0`**, so anything assuming `d1`
exists breaks.

### `single_device_ibm`

*A one-device session works with no routing decisions to make.*

Full command sweep — `qdevices`, `qconfig`, `qerrors`, `qtopology`,
`qrun`, `qmap`, `qps`, `qmem` — against a lone Nairobi. Asserts no
output mentions `d1` or `d2`, and that noise_graph still selects
`{0:1, 1:2}`: allocation is per-device and does not depend on having
peers.

### `single_device_named`

*Naming works with one device, and the index still resolves.*

The sole device is named `solo` and must display as `solo (d0)`. Runs
`--exec=solo` and `--exec=d0` in separate sessions and asserts identical
mappings — naming has no special case at federation size 1.

### `single_device_batch`

*Batch submission and packing on a single device.*

Two bells submitted together must pack onto **disjoint** qubit blocks in
one cycle. The assertion is that their mappings differ; identical
mappings would mean the pool handed out the same qubits twice, a
correctness failure rather than a performance one.

### `single_device_rejection`

*Rejection on a single device names that device in the reason.*

A Lagos-only session with a 0.03 qubit threshold. The job must be
REJECTED — with no alternative device the router cannot fall back, and
the terminal state must still be reached cleanly rather than leaving the
job queued forever.

### `single_device_devq`

*The mock provider alone — no Qiskit involved in execution.*

A fully-connected 5-qubit mock device named `mock`. This is the only
block whose execution path never touches Qiskit, so it verifies DevQ's
core runs without a quantum framework — the claim that
`DevQSimulatedProvider` is a genuine zero-dependency reference
implementation.

---

## Matrix and determinism

### `plugin_matrix`

*Every scheduler × allocator × router combination runs to completion.*

Enumerates every registered scheduler × allocator × router — 18
combinations today — writes a temporary config for each, and runs a bell
and a ghz through a two-device federation. A combination passes only if
both jobs reach FINISHED.

The combinations are read from the registry rather than from a fixed
list, so anything registered is covered automatically. A plugin
registered before this block runs would widen the matrix without the
block changing.

**This is the most valuable block in the suite.** The default
combination (`packing`/`noise_graph`/`noise`) is exercised constantly;
every other combination previously ran only when someone hand-edited an
entry point. Two real bugs lived in that gap:

- `RoundRobinRouter.__init__` took no arguments while `_build_router`
  passed four weight kwargs — **all 9** `*/round_robin` combinations
  died at startup.
- `PackingScheduler`'s inner `TempPool` implemented only 2 of
  `QubitPool`'s 3 methods. `StaticAllocator` alone calls the missing
  `available()`, so it raised `AttributeError`, which a bare
  `except Exception` turned into "allocation didn't fit" — the job was
  neither allocated nor rejected and `qrunpack` **hung forever**.

Each combination runs under a `SIGALRM` watchdog so a hang fails the
block instead of blocking the suite. Failures are reported per
combination with the exception, e.g.
`packing/static/noise: 0/2 jobs finished`.

When adding a scheduler, allocator or router, this block covers it
automatically — it reads the registry maps rather than a fixed list.

### `determinism_seeded`

*Identical seeds reproduce devices and counts exactly.*

Builds three sessions and compares full transcripts: `seed=42` twice
must be identical, `seed=43` must differ. This covers both randomness
sources at once — `d0`'s generated topology and error maps, and Aer's
sampling.

It also asserts that two runs of the **same circuit within one session**
produce **different** counts. That is the check on derived per-run seeds
(`seed + k`): a single reused seed would clone results, which looks like
determinism but is wrong.

### `determinism_unseeded`

*Without a seed, sessions stay non-deterministic.*

The negative control. Two unseeded sessions must differ. Without this, a
bug that seeded everything unconditionally would pass
`determinism_seeded` and silently break the default path.

### `bug_fix_witnesses`

*Per-device noise models and allocator mappings reach the simulator.*

Asserts error rates rather than mappings, because two Phase 5.1 bugs
were invisible at the mapping level — the allocator computed the right
answer and execution ignored it.

| Observed bell error | Meaning |
|---|---|
| **~5%** on Nairobi | correct |
| ~27% | executing under **Lagos's** noise model — shared-state leak |
| ~10% | `v2p_map` dropped, job ran on physical qubits 0–1 |

Lagos's ~15% is likewise real: physical qubit 1 carries 13.6% readout
error, and the mapping `{0:1, 1:3}` uses it.

Bands are asserted rather than exact counts, so the block survives minor
Aer changes while still catching either regression.

---

## Registry and plugin extension

These blocks cover DevQ's extensibility surface: the registry that maps
config names to components, and the contract a third-party component
must satisfy. They matter more than their line coverage suggests —
Phase 5.6 implements published scheduling policies *as DevQ plugins*, so
a silent break in this path would not surface until the results that the
paper depends on were already being generated.

**The mocks are deliberately observable.** `MockScheduler` is LIFO and
`MockAllocator` is first-fit, not because either is a sensible policy
but because every built-in behaves differently. A mock indistinguishable
from a built-in cannot demonstrate that the registry wired anything up:
the block would pass just as happily with the plugin ignored. This was
not hypothetical — the first version of `MockScheduler` was FIFO, and
mutation-testing showed that replacing the registry lookup with a
hardcoded `PackingScheduler` left the block green.

### `registry_plugin_components`

*Third-party scheduler, allocator and router run a job end to end.*

Registers all three mocks on one `DevQ` instance, names them in a config
file, and runs two circuits. Asserts three separate things:

| Claim | Evidence |
|---|---|
| resolved by name | `scheduler = mock` in `qconfig` |
| reported by declared label | `[Mock Scheduler]`, not `MockScheduler` |
| actually in the execution path | dispatch order and mapping, below |

The last is the one that carries weight. `MockScheduler` is LIFO, so
job 2 must be dispatched before job 1 — every built-in dispatches 1
first. And `MockAllocator` is first-fit, so job 1 maps to `{0:0, 1:1}`;
the default `noise_graph` allocator would pick the lowest-noise pair
instead. Both assertions fail if the registry lookup is bypassed.

### `registry_validation`

*Malformed components are rejected at registration, not at run time.*

Fifteen cases, each a component violating the contract in exactly one
way, paired with a phrase its rejection must contain. Defined inline
rather than at module scope so that a violation and its expected message
read together.

| Case | Level | Rejected because |
|---|---|---|
| `NotAScheduler` | type | not a `BaseScheduler` subclass |
| `NoInitArgs` | bind | `__init__` takes nothing; DevQ passes two args |
| `BadSelectSignature` | methods | `select()` takes one arg; the router contract passes two |
| `BadEnqueueSignature` | methods | `enqueue()` takes none; the kernel passes a QCB |
| `UnNamespacedKey` | schema | `window` is not namespaced |
| `IllegalScope` | schema | a scheduler declaring a `global` key |
| `DefaultFailsValidator` | schema | default `-5` fails its own `positive_int` |
| `ValidatorNeverAccepts` | schema | validator returns a message for every value |
| `DanglingGroupMember` | groups | group names a key no schema declares |
| `SingleMemberGroup` | groups | one member normalises to 1.0 regardless of config |
| `GroupNeverDeclared` | groups | key names a group nothing declares |
| scheduler instance | instance | per-device components must be classes |
| router instance | instance | every kind is class-only; an instance bypasses the cascade |
| duplicate name | naming | would change what existing config files mean |

**The router-instance case reversed.** It previously asserted that a
router instance was *accepted*, on the grounds that one-per-system made
sharing safe. That was wrong: `_build_router` returned a registered
instance as-is, so it kept whatever weights it was constructed with and
never saw the config cascade — `qconfig` would report one set of weights
while another set was routing, and Phase 5.5's weight sweep would have
produced identical results at every weight while appearing to vary. The
block now asserts the rejection, that the error names the cascade as
what an instance bypasses, and — the positive half — that a
class-registered router really is constructed from resolved config,
asserted against the **live router object** rather than `qconfig`
output, since the bug was precisely that the two could disagree.

**Two of these are past bugs.** `NoInitArgs` is Phase 4's
`RoundRobinRouter`, whose `__init__` took no arguments while
`_build_router` passed four — all nine round-robin combinations died,
and it was found only by `plugin_matrix`. `BadSelectSignature` and
`BadEnqueueSignature` are the shape of the `TempPool` bug, where an
object implemented two of the three methods its consumer called and the
resulting `AttributeError` was swallowed by a bare `except`, hanging the
shell forever. Both are now caught at registration, with the offending
signature printed.

**Why the method checks cover more than the abstract methods.** Three of
the four component kinds use a template-method pattern: the kernel calls
`router.route()`, which is concrete and delegates to the plugin's
abstract `select()`. Checking only `route()` would pass a plugin whose
`select()` has the wrong signature, because it inherits a perfectly
valid `route()`. The same holds for `allocate()`/`feasible()` and
`schedule()`/`enqueue()`, so both halves of each pair are checked.

### `registry_frozen`

*Registration after `build()` is refused rather than silently ignored.*

`build()` reads the configuration and freezes the registry, so a later
registration could not affect the system that was built. Accepting it
and doing nothing would be the worse failure: the user would have a
plugin they believe is active.

Also asserts the freeze is **per-instance** — a fresh `DevQ` can still
register after another has been built. The registry is instance-scoped
precisely so that the test suite, which builds ~30 shells in one
process, cannot leak registrations between blocks.

### `plugin_config_keys`

*Plugin-declared config keys cascade, validate and appear in `qconfig`.*

The block runs the same config file twice, before and after registering
the plugin that declares its keys:

| | `mock.batch_window` | `"scheduler": "mock"` |
|---|---|---|
| before registering | unknown key, ignored | invalid value |
| after registering | `12`, `User (global)` | accepted |

Nothing in DevQ core changes between the two runs. That is the whole
claim of the registry: the legal set of scheduler names and the legal
set of config keys are both **derived from what is registered**, not
from a hand-maintained list. A namespaced key is not privileged simply
for being namespaced — it is legal only once its owner is registered.

Also asserts an unset plugin key resolves to its declared default with
`DevQ Core` provenance, and that a bad value is rejected by the
**plugin's own validator**, quoting the message that validator supplied.

**Scope isolation is asserted against the resolved config, not
`qconfig`.** An earlier version of this block checked that a
device-scope key was absent from `qconfig`'s global section — which
passed regardless, because `qconfig` renders only the keys it iterates
over, so a leaked key would never have appeared there. Mutation testing
caught it. The check now reads `shell._global_config` and the device
context's config directly, and asserts both directions: a device key is
absent from the global scope, and a global key is absent from the device
scope.

### `plugin_normalise_group`

*A plugin's own normalise group is scaled to sum to 1.*

`mock.wait_weight: 3` and `mock.fid_weight: 1` must resolve to `0.75`
and `0.25` — only the ratio between members carries meaning, so any
scale is equivalent. Asserts the core `noise_cost` group normalises
independently in the same pass, so groups do not interfere.

The all-zero case then reverts the group to its declared defaults
(`0.4`/`0.6`) with a warning. A group summing to zero has an undefined
ratio and would make every candidate score identical, silently degrading
the consuming policy to "first candidate found" — the same failure the
core `zero_weight_fallback` block guards against, here proven to work
for a group DevQ core has never heard of.

### `component_labels`

*`qconfig` shows declared human labels, not class names.*

Asserts `[Circuit Packing Scheduler]` rather than `[PackingScheduler]`,
and that a component declaring no `LABEL` falls back to its class name
rather than showing nothing.

This block exists because of a real regression. When the three
module-level label tables were replaced by a `LABEL` class attribute,
the attribute was added to the components but not committed. Every one
of the then-31 blocks still passed — nothing asserted on label text —
and `qconfig` silently degraded to class names on `main`. The block is
cheap and would have caught it immediately.

---

### `shipped_workloads`

Runs every spec in `benchmark/workloads/` end to end, mirroring the way
`config/config_examples/` are consumed by the config blocks: those files
are runnable examples *and* test fixtures, so a schema change that
breaks one fails the suite rather than surfacing when a user tries it.

Validation alone is not enough — a spec can parse and still fail at
execution. Each must complete, produce a log opening with a `header`
that records its own spec verbatim and closing with a `summary`, and
expand to exactly the job count its `repeat` fields imply.

Seeded specs are run twice and their **decisions** compared: the
`submit`/`route`/`dispatch` sequence with job ids, devices and
allocations. Completion order and wall clock are deliberately excluded —
those belong to the executor, and on real hardware to the provider's
queue. This is the reproducibility DevQ actually guarantees.

A spec naming a provider the caller must register (`ibm_federation.json`
does) is not broken — that is the documented extension model — so the
block supplies the providers DevQ ships.

Output is written to `test_results/` and KEPT, unlike every other block,
so a run can be opened and read after the suite finishes. It is
overwritten each run rather than timestamped, so it cannot accumulate,
and both its existence and its content are asserted — "the directory
exists" and "the directory has usable logs" are different claims.

`block_benchmark_runner` builds its own spec in a temp directory because
it asserts exact job counts and needs a crash injected; this block runs
what actually ships.


### `repo_hygiene`

Checks invariants the README states and nothing else enforces: every
`.py` file carries a `Tags:` header, `TEST_BLOCKS.md` is 1:1 with the
block list, and the block count stated in prose matches reality.

These break silently. A new file without a tag costs nothing at runtime
and misleads every reader afterwards; a stale count in this document
does the same. `verify_local.py` shipped untagged for exactly that
reason, and a count drifted twice across earlier sessions. None of it is
catchable by a test that only exercises behaviour, so it is asserted
directly.

It also validates every shipped workload spec in
`benchmark/workloads/` and checks the circuits they name exist. Those
are the only runnable examples of the benchmark runner —
`block_benchmark_runner` builds its specs in a temp directory and
deletes them — so without this nothing would notice if the schema
drifted away from them, and the failure would surface only when someone
tried to run one.

The tag scan walks DevQ's own packages by name, plus the top-level
scripts. It must NOT walk the repository root with a blocklist: the
first version did, and on a machine with a `venv/` in the working tree
it audited numpy, scipy, matplotlib and qiskit, reporting several
thousand missing headers. Blocklisting `venv` would not fix it either —
the next one is `.venv` or `env`. Naming what belongs to DevQ cannot
fail that way.


### `benchmark_runner`

Covers `benchmark/runner.py`: run directories, the manifest, atomic
writes, crash isolation and resume.

A run always produces a directory with one JSONL log per session plus a
manifest — a single-spec run has the same shape as an 18-session
matrix, so a reader never branches on which it is looking at. The log
opens with a `header` carrying the spec verbatim and the device table
(written once, not repeated per record) and closes with a `summary`
carrying one row per job ordered by job id. The log body itself stays
chronological; the per-job table is a derived view.

Three outcomes are distinguished, and the first two must not be
confused: `completed`, `completed_with_failures` (jobs were rejected or
failed — a *result*, and exactly what a threshold sweep is meant to
produce), and `crashed` (the session died). Phase 5.3 reads this
distinction.

One run is made with no `--out` at all, from a temp working directory,
to assert the *default* path: `results/<spec name>_<timestamp>/`. Every
other assertion passes `out_dir` explicitly, so the path a user actually
gets was untested — and was in fact wrong, with the summary line
reconstructing the directory from a bare log filename and printing a
literal `results`.

Crash isolation is asserted by forcing one matrix session to raise: the
other seventeen must still complete. Atomic writes are asserted by
checking no `.partial` file is orphaned and that a crashed session's log
is kept under a name no reader will trust.

Resume is asserted to skip completed sessions and re-run the crashed one
whole. It is deliberately session-level only: seeding is sequential —
IBM derives each run's seed as `seed + k` from a submission counter — so
a session restarted mid-way would reproduce different noise than an
uninterrupted one, and the two halves would not be comparable. Sessions
are identified by what varied rather than by position, so inserting a
component into the matrix cannot silently re-map existing results onto
different configs.


### `workload_spec`

Covers `benchmark/spec.py`: validation, seed resolution, and an
end-to-end run from a spec.

Thirteen malformed specs must each raise `SpecError`. Spec parsing is
strict where config parsing is lenient, and the reason is the absence
of a fallback rather than a difference in severity: every config key has
a documented default, and a spec key has none. There is no sensible
default for which circuit to run or which device to run it on, so
refusing is the only alternative to guessing.

Absent-with-a-default is not an exception — `repeat` and
`arrival.pattern` have defaults, and omitting them is asserted to be
silent. It is keys carrying no actionable meaning that are refused.

Seed resolution has four distinct cases, all asserted: a registered
CLASS is constructed with the spec's seed (no conflict possible); an
UNSEEDED instance accepts the spec's seed via `set_seed`; a SEEDED
instance overrides the spec and warns; a seeded instance with no spec
seed is not a conflict. Code wins over the spec because a collaborator
pinning a seed in their own code, against a shared spec they do not
own, is expressing intent.

`set_seed` must reproduce a *freshly constructed* provider, not merely
set an attribute — devq builds its RNG in `__init__`, so a provider that
only stored the value would keep generating unseeded devices while
reporting the spec's seed. It must also refuse once devices exist, since
their error maps already derive from the old seed.

The end-to-end case asserts `repeat: N` expands to N distinct job ids,
`no_exec_on` survives the id→index translation, and spec ids become
device names in spec order.

One assertion is a regression guard rather than a specification: `drain`
must complete a five-job workload in under 200 cycles. An early version
stepped the kernel whenever a future was in flight, producing 37,923
empty cycles and 37,923 `cycle_end` records for twenty real events.


### `event_log`

Covers the kernel's structured event stream and QCB timing.

The central assertion is that attaching a sink leaves console output
byte-identical — if that drifts, every other block's expected output
becomes a function of whether logging is on. Also asserts all six event
kinds appear, that `seq` is a dense monotonic range (a gap means an emit
site bypassed `_emit`), that `cycle` never decreases, that every
dispatch pairs with exactly one resolve *by job_id*, and that route
records carry one score per candidate.

Sink failure is isolated at two levels: a raising sink must not kill the
job, and `MultiSink` must keep delivering to healthy members when one
raises. Both are asserted.

Timing assertions cover both clocks. `*_seq` is deterministic and
ordered; `*_at` is wall clock and only ordered. Turnaround must equal
queue latency plus execution time rather than being measured
independently. Unfinished jobs must report `None` from **all three**
derived properties — each needs its own guard, and without one the
property raises `TypeError` on a job that never dispatched, which would
crash a metrics pass on the first rejection.

NOT asserted: that two identical seeded runs produce identical logs.
They do not, and should not. Completion order belongs to the executor —
and on real hardware to the provider's queue — so which cycle a job
resolves in varies between runs. DevQ guarantees *decision* determinism
(same seed, same routing, allocation and counts), not completion-order
determinism. A determinism comparison therefore sorts on `seq` and
excludes wall-clock fields.


### `router_scoring`

Pins `NoiseRouter`'s scores and routing decisions at five weightings
against independently computed values, then repeats three of them with
asymmetric queue load. Also asserts that `explain()` records true raw
terms, that those terms re-derive the decision at other weights, and
that a non-scoring router returns `None`.

Two lessons are built into it. First, asserting `explain()` against
`select()` proves nothing — both read one shared scoring path, so a
mutation moves them together and the comparison still holds; the scores
are therefore pinned to externally computed values. Second, every other
routing block runs on idle devices, where queue pressure is uniformly 0
and normalises away, making the two router weights interchangeable: a
swap survived 39 blocks. The loaded fixture (d2 cheapest but most
loaded) is the only configuration that witnesses it.

Deliberately untested: the `(score, index)` tie-break in `select()`.
Candidates arrive in index order and `min()` is stable, so removing the
index term changes nothing observable — a test for it could not fail.


### `provider_registration`

Asserts that `add_device()` refuses a device whose provider class is not
registered: a built-in attaches with no registration line, an
unregistered `IBMSimulatedProvider` device is refused with the class
named, registering the **class** admits a device built by an instance
the caller constructed themselves, a **subclass** of a registered
provider is still refused, and a provider **instance** cannot be
registered at all.

Exists because `is_registered()` returning `True` unconditionally
survived all 45 preceding blocks. Every other block registers its
providers correctly, so a gate that never rejects is indistinguishable
from one that works — only an assertion on the REFUSAL pins it.

The subclass case is asserted rather than assumed: matching is on exact
type, so registering a base class must not bless its derivatives. The
sibling block `provider_global_key` is the standing proof that a
subclass can behave differently from its base.

Two of this block's assertions were themselves self-satisfying when
first written. `check(False, ...)` inside a `try` whose `except Exception`
followed caught the `AssertionError` that `check()` raises and reported
it as a pass, so re-allowing provider instances survived. The refusal is
now captured into a variable outside the check. Every other `check(False)`
in the suite catches a specific exception type and is not exposed to
this.

### `device_identity`

Asserts the three identity fields against the **device object**, not
rendered output: `index` and `name` are `None` before attach, indices
follow add order, aliases arrive lowercased, an unnamed device keeps
`None`, `kind` is shared across same-kind devices, and a second
`attach()` raises.

Exists because a mutation that dropped the alias in `DevQ.build()` passed
all 37 preceding blocks — `DeviceContext` carried the alias for every
consumer, so nothing read it off the device. The event log reads
device-side identity, so the gap was latent rather than harmless.

### `same_kind_isolation`

Builds the four-same-kind-device session and asserts on resolved provider
state: four sessions keyed `0..3`, four distinct noise models, one shared
backend object, and a backend cache keyed by kind with caller casing
preserved.

Witness for the session-collision bug — `_sessions` was keyed by
`backend_name`, so N same-kind devices collapsed onto one entry and the
last built won. Skips cleanly when qiskit is absent.

## Driving a session by hand

The automated blocks cover everything above; this is for interactive
exploration.

```bash
python example.py              # three-device session
python example.py --seed 42    # reproducible
python example.py --help
```

Paste any block's commands into the shell. To reproduce a block that
uses a different config or per-device file, edit the `DevQ(...)` call in
`example.py` — for instance `per_device_config`'s setup is:

```python
.add_device(ibm.get_device("FakeNairobiV2"),
            "./config/config_examples/d1.static.config.json",
            name="nairobi")
```

All configs referenced above live in `config/config_examples/`. Job IDs
restart at 1 in each fresh session, so blocks run out of order will
number differently than described here.