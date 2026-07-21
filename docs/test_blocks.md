# DevQ Sanity Test Plan

Specification for the sanity blocks in `run_tests.py`, covering Phases
0–5.1.

`run_tests.py` asserts **what** each block expects. This document
records **why** those values are correct — the S-cost arithmetic behind
a mapping, the reason a device is rejected, the physics behind an error
rate. When a block fails, the assertion tells you something changed;
this tells you whether the change was a regression or an improvement.

## Running

```bash
python run_tests.py              # all 20 blocks
python run_tests.py --list       # block names and descriptions
python run_tests.py -k single    # only blocks matching a pattern
python run_tests.py -k matrix    # just the plugin sweep
python run_tests.py -v           # also print captured output
```

Run from the project root — circuit paths are relative. Exit status is
0 only when every block passes, so `python run_tests.py && git push`
works as a pre-push gate.

The full suite takes a few minutes, most of it `plugin_matrix` running
36 Aer simulations. While iterating, `-k <block>` is much faster.

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
[`cost-model.md`](cost-model.md).

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

Enumerates `_SCHEDULER_MAP` × `_ALLOCATOR_MAP` × `_ROUTER_MAP` — 18
combinations today — writes a temporary config for each, and runs a bell
and a ghz through a two-device federation. A combination passes only if
both jobs reach FINISHED.

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