# DevQ Sanity Test Plan

Manual verification blocks covering Phases 0–5.1. Run `python main.py`
from the project root and paste each block's commands into the shell.

Blocks are **cumulative within a session** unless a block says
*fresh session* — job IDs continue from the previous block, so running
them out of order will shift every ID.

## Session under test

| Device | Backend | Provider | Qubits |
|---|---|---|---|
| `d0` *(unnamed)* | `random_backend` | `DevQSimulatedProvider` | 7 |
| `nairobi` (`d1`) | `fakenairobiv2` | `IBMSimulatedProvider` | 7 |
| `lagos` (`d2`) | `fakelagosv2` | `IBMSimulatedProvider` | 7 |

## Conventions

**Device references.** Named devices are addressable **both ways** —
`d1` and `nairobi` are the same device in `--exec`/`--no-exec` and in
every device-scoped command. Blocks below deliberately mix the two
forms; where a block writes a name, the index form must work
identically and vice versa (spot-check by re-running a command the
other way). `d0` is unnamed throughout and is only ever addressable as
`d0`.

**Expectations use index form** (`d1`, `d2`) as the canonical
identifier, since indices are always valid. On screen, named devices
*display* as `nairobi (d1)` / `lagos (d2)` — in Dispatching lines, the
`qps` device column, `qmap`, `qmem`, `qerrors`, `qtopology` and
`qconfig` headers. `d0` displays bare. Read "Job 1 → d1" as satisfied
by `nairobi (d1)`.

**Config files** are referenced by name and all live in
`config/config_examples/`. A block's stated config stays in effect for
every following block until a block states a new one.

**Before trusting anything downstream:** `d1`/`d2` calibration values
assume `qiskit-ibm-runtime` 0.45.1. Verify Block 1's `qerrors` output
first; if those match, the rest should follow.

**Counts are unseeded** except in Block 10 — check mappings, states and
reasons, not counts. `d0`'s error map is random per launch, so
deterministic checks pin `--exec` or exclude `d0`.

---

## Block 1 — Devices and config

**Config:** `router_only.config.json` (global, as `main.py`'s
`config_path`). Fresh session.

```
qdevices
qconfig
qerrors q d2
qerrors e d2
qtopology d1 1
```

**Expect.** `qdevices` shows three rows — `d0` `random_backend` (7q),
`d1` `fakenairobiv2` (7q), `d2` `fakelagosv2` (7q), all `queued: 0`
`running: 0` — plus an **alias column** (`d0` shows `-`, `d1`
`nairobi`, `d2` `lagos`), which appears because at least one device is
named.

`qconfig` shows global `router=noise` [User (global)] with router
weights 0.5 [User (global)], **and** global `qubit_error_weight` 0.1 /
`edge_error_weight` 0.9 [DevQ Core] (the router's S yardstick); `d0`
packing/noise_graph [DevQ Core] shots 1024 [DevQ Core]; `d1` and `d2`
packing/noise_graph [DevQ Core] shots 2048 [IBMSimulatedProvider].
**Every** device section additionally shows `qubit_error_weight` 0.1 /
`edge_error_weight` 0.9 [DevQ Core].

`qerrors q d2` (Lagos):

| Qubit | 0 | 1 | 2 | 3 | 4 | 5 | 6 |
|---|---|---|---|---|---|---|---|
| Error | 0.1690 | 0.1362 | 0.4638 | 0.0167 | 0.0292 | 0.2619 | 0.3480 |

`qerrors e d2`:

| Edge | (0,1) | (1,2) | (1,3) | (3,5) | (4,5) | (5,6) |
|---|---|---|---|---|---|---|
| Error | 0.0094 | 0.0103 | 0.0107 | 0.0290 | 0.0083 | 0.0202 |

`qtopology d1 1` filtered to `0--1`, `1--2`, `1--3` only.

---

## Block 2 — Noise routing and Lagos mappings

**Deterministic.** Same session as Block 1.

```
qrun test_circuits/bell.qasm --exec=nairobi,lagos
qrun test_circuits/bell.qasm --exec=d2
qrun test_circuits/ghz.qasm --exec=d2
```

**Expect.**

| Job | Routes to | Mapping | Why |
|---|---|---|---|
| 1 | `d1` | `{0:1, 1:2}` | Nairobi S≈0.0102 beats Lagos S≈0.0249 |
| 2 | `d2` | `{0:1, 1:3}` | Lagos's best bell block |
| 3 | `d2` | `{0:3, 1:4, 2:5}` | pinned |

All three FINISHED.

---

## Block 3 — Cross-device REJECTED semantics

**Deterministic.** Same session.

```
qrun test_circuits/bell.qasm --max-qubit-error=0.03 --exec=lagos
qrun test_circuits/bell.qasm --max-qubit-error=0.03 --exec=d1,d2
qrun test_circuits/bell.qasm --max-qubit-error=0.0185 --exec=nairobi,lagos
```

**Expect.**

- **Job 4** REJECTED — `...d2: no connected block of 2 qubits...`
  (Lagos qubits 3 and 4 pass the 0.03 threshold but are not adjacent).
- **Job 5** → `d1` `{0:1, 1:2}` — same threshold, feasible on Nairobi
  so routed there rather than rejected.
- **Job 6** REJECTED with **two aggregated reasons**:
  `d1: ...only 1...satisfy max_qubit_error=0.0185; d2: ...only 1...`.

---

## Block 4 — Batch, bracket groups, packing across devices

Same session.

```
qsubmit [test_circuits/bell.qasm test_circuits/bell.qasm test_circuits/ghz.qasm --no-exec=d0] test_circuits/ghz.qasm --exec=lagos
qrunpack
qps
qmap 7
qmem
```

**Expect.**

- **Jobs 7 and 8** (bells) → `d1`, packed in the **same cycle** on
  `{1,2}` and `{4,5}`.
- **Job 9** (ghz) → `d1` (0.5/0.5 score tie vs `d2`, lower index wins),
  waits one cycle then runs on `{0,1,2}`.
- **Job 10** (ghz, pinned) → `d2` `{3,4,5}`.

All four FINISHED. Dispatch and resolution lines from `d1` and `d2` may
interleave — that is the async concurrency working, not a fault.

`qps`: jobs 1–3 and 5 FINISHED on `d1`/`d2`; 4 and 6 REJECTED with
device `-`; 7–9 on `d1`; 10 on `d2`.
`qmap 7`: device `nairobi (d1) (fakenairobiv2)`, `0->1`, `1->2`.
`qmem`: three sections, all qubits free.

---

## Block 5 — Parser and validation errors

Same session.

```
qsubmit test_circuits/bell.qasm --exec=d5
qsubmit test_circuits/bell.qasm --exec=d0 --no-exec=d1
qsubmit test_circuits/bell.qasm --exec=[d0,d1]
qsubmit test_circuits/bell.qasm --exec=sherbrooke
qsubmit nofile.qasm test_circuits/bell.qasm
qps
```

**Expect.** Each command prints a clear `[DevQ Error]` and creates
**zero** jobs — the closing `qps` confirms the job count has not grown.
Errors in order:

1. Only 3 device(s) attached.
2. `--exec` and `--no-exec` are mutually exclusive.
3. Brackets are reserved for grouping.
4. Unknown device **name** `sherbrooke` — the message lists the
   attached named devices (`Named devices: nairobi, lagos`).
5. Bad file path kills the whole batch, including the valid
   `bell.qasm` alongside it.

---

## Block 6 — Round-robin router

**Config:** `round_robin.config.json` (global). Fresh session — job IDs
restart at 1.

```
qconfig
qsubmit test_circuits/bell.qasm test_circuits/bell.qasm test_circuits/bell.qasm
qrunpack
qps
```

**Expect.** `qconfig` shows `router = round_robin`
`[Round Robin Router]` source User (global). After `qrunpack`, the
routing sequence is exactly `d0`, `d1`, `d2` — check the Dispatching
lines or the `qps` device column.

---

## Block 7 — Free routing with d0

**Qualitative** — `d0`'s random error map changes per launch.
**Config:** `router_only.config.json` (global). Fresh session.

```
qrun test_circuits/bell.qasm
qps
```

**Expect.** The bell routes to whichever of `d0`/`d1` scores lower this
launch; `d2` essentially never wins a free bell. The job FINISHES. If
it lands on `d0`, counts are uniform mock counts (roughly equal across
`00`/`01`/`10`/`11`); if on `d1`, counts are noisy Bell-like (heavy
`00`/`11`).

---

## Block 8 — Per-device config

**Config:** `router_only.config.json` (global) **plus**
`d1.static.config.json` as the per-device config on the Nairobi line.
Change `main.py` to:

```python
.add_device(ibm.get_device("FakeNairobiV2"),
            "./config/config_examples/d1.static.config.json",
            name="nairobi")
```

Relaunch (fresh session), and revert `main.py` afterwards.

```
qconfig d1
qconfig
qrun test_circuits/bell.qasm --exec=d1
qmap 1
```

**Expect.** `qconfig d1` shows `allocator = static` `[Static Allocator]`
source User (d1) and `shots = 512` source User (d1), while `scheduler`
stays packing [DevQ Core] and the weight pair stays 0.1/0.9 [DevQ Core].
`d0` and `d2` are unaffected — confirm with the bare `qconfig`.

The pinned bell on `d1` maps to the **first free block** `{0:0, 1:1}`
instead of noise_graph's `{0:1, 1:2}` — Static ignores noise by design.

---

## Block 9 — Common-scope noise cost weights

**Config:** `weights_1_9.config.json` (global, as `main.py`'s
`config_path`) **plus** `d1.edge_only.config.json` as the per-device
config on the Nairobi line — same `main.py` edit as Block 8, swapping
the path. Relaunch (fresh session); revert afterwards. The bonus check
uses `zero_weights.config.json` as a per-device or global file in a
separate launch.

```
qconfig d2
qconfig d1
qrun test_circuits/bell.qasm --exec=d1
qrun test_circuits/bell.qasm --exec=d2
qps
```

**Expect.** `qconfig d2` shows `qubit_error_weight` 0.1 /
`edge_error_weight` 0.9 source User (global) — the raw 1/9 is
**normalised to sum to 1** at resolution time. Any non-negative pair is
accepted and normalised, so 1/9, 0.1/0.9 and 2/18 are equivalent; the
same rule applies to the router weight pair. `qconfig d1` shows 0 / 1
source User (d1), a per-device override of the common key.

- **Job 1** (bell pinned `d1`) → `{0:1, 1:3}` — edge-only weighting
  picks Nairobi's lowest-error edge (1,3)=0.0068 instead of the
  default-weight choice `{0:1, 1:2}`.
- **Job 2** (bell pinned `d2`) → `{0:1, 1:3}` — Lagos unchanged under
  1/9, because the **ratio** equals the 0.1/0.9 default.

**Bonus.** `zero_weights.config.json` prints a `[Config] Warning` and
falls back to 0.1/0.9 [DevQ Core].

---

## Block 10 — Determinism and seeding

**Phase 5.1.** Same three devices and config as every other block
(`router_only.config.json`); the only difference is `main.py`'s
`--seed` flag, which passes a seed to both providers at construction.

Run the command list **four times** as four separate fresh launches,
saving each session's output:

| Run | Command | Role |
|---|---|---|
| A | `python main.py --seed 42` | reproducibility |
| B | `python main.py --seed 42` | reproducibility |
| C | `python main.py` | unseeded control |
| D | `python main.py` | unseeded control |

Diff the full transcripts A vs B, then C vs D. This is the **only**
block that asserts counts — seeding is what makes them assertable.
`python main.py --help` should list `--seed`.

```
qerrors q d0
qtopology d0
qrun test_circuits/bell.qasm --exec=nairobi
qrun test_circuits/bell.qasm --exec=d1
qrun test_circuits/bell.qasm --exec=lagos
qps
```

**Headline assertion.** Run A and Run B transcripts are **identical
byte-for-byte** (diff clean), covering both `d0`'s generated device and
all three jobs' counts.

Specifics under `seed=42`, on `qiskit-ibm-runtime` 0.45.1 —
`qerrors q d0`:

| Qubit | 0 | 1 | 2 | 3 | 4 | 5 | 6 |
|---|---|---|---|---|---|---|---|
| Error | 0.0352 | 0.0177 | 0.0086 | 0.0479 | 0.0175 | 0.0055 | 0.0057 |

`qtopology d0`: `0--2`, `0--3`, `0--4`, `1--3`, `1--5`, `1--6`, `2--4`,
`2--5`, `3--5`, `4--6` (10 edges).

| Job | Device | Mapping | Counts |
|---|---|---|---|
| 1 | `d1` | `{0:1, 1:2}` | `{'00':1004, '11':937, '10':58, '01':49}` |
| 2 | `d1` | `{0:1, 1:2}` | `{'00':996, '11':954, '01':50, '10':48}` |
| 3 | `d2` | `{0:1, 1:3}` | `{'00':871, '11':867, '01':165, '10':145}` |

All three FINISHED.

**Negative control 1 — distinct runs stay distinct.** Jobs 1 and 2 are
the same circuit on the same device and mapping, but their counts
**differ** — derived per-run seeds (`seed+k`), not a single reused seed
cloning results.

**Negative control 2 — omitting `--seed` is unchanged.** Runs C and D
differ from each other *and* from Run A, so unseeded behaviour is
preserved exactly and every other block in this file still behaves as
written. C and D's job mappings still match Blocks 1–2 (`{0:1,1:2}` on
`d1`, `{0:1,1:3}` on `d2`); only counts and `d0`'s generated device vary
between them.

**Bug-fix witnesses.** Phase 5.1 fixed two execution bugs; these numbers
only hold once both are in:

- Job 1's error weight outside `00`/`11` is **~5%, not ~27%** — `d1`
  executes under *Nairobi's* noise model, not Lagos's (fixed
  shared-noise-model leak).
- ~5% rather than ~10% reflects the allocator's mapping `{0:1, 1:2}`
  actually reaching the simulator as `initial_layout=[1,2]` (fixed
  dropped `v2p_map`).
- Job 3's ~15% error on Lagos `{0:1, 1:3}` is likewise real: physical
  qubit 1's 13.6% readout error dominates.

If any of these three error rates is far off, suspect the pinned stack
before the code — verify Block 1's calibration output first.