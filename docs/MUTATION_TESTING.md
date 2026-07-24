# Mutation testing

`run_tests.py` answers *does the code work?* This answers *would the
tests notice if it stopped?*

A suite can be entirely green and assert nothing. Mutation testing finds
that by deliberately breaking the code and checking the suite goes red.
If it stays green, the test is decorative.

```bash
cp kernel/router/noise_router.py /tmp/backup                     # save
sed -i 's/router_queue_weight \* p/router_noise_weight * p/' ...  # break
python run_tests.py                                              # expect FAIL
cp /tmp/backup kernel/router/noise_router.py                     # restore
```

**Killed** = the suite failed, so something was genuinely asserting on
that line. **Survived** = nothing was.

Mutants are chosen by hand, one per plausible failure mode, rather than
generated exhaustively. That is a deliberate trade at this size: an
automated tool would produce thousands of trivial mutants against a
~25 s suite. It also bounds the claim — these results show the suite is
sensitive to *known* failure modes, not that it is sensitive in general.

This is a development-time audit. Nothing here is committed; each
mutation lives for one `sed` and is reverted. What reaches the repo is
only the consequence: several test blocks exist *because* a mutant
survived.

---

## Why it matters here

Line coverage is a poor signal for this codebase. `plugin_matrix` runs
18 scheduler × allocator × router combinations and inflates coverage
enormously while asserting only that each finishes;
`bug_fix_witnesses` touches almost no new lines and is among the most
valuable blocks in the suite. Mutation score measures what coverage
cannot.

---

## Results

**60 distinct mutants, 58 killed, 2 equivalent** (excluded by
convention — see below). Grouped by subsystem. Several were re-run
against `main` after each push to confirm the pushed state matches what
was verified locally; those re-runs are not counted again here.

### Device identity — `hardware/device.py`, `providers/`, `devq.py`

| # | Mutation | Result |
|---|---|---|
| M1 | `_sessions[device.index]` → `[device.kind]` | killed (11 blocks) |
| M2 | `on_attach()` call deleted | killed (11) |
| M3 | `attach(index, name)` → `attach(index)` | killed (1) |
| M5 | `self.name = name` → `= None` | killed (1) |
| M6 | `kind = backend_name` → `.lower()` | killed (12) |
| M7 | double-attach guard removed | killed (1) |
| M4 | backend cache removed | killed (11) |

### Router scoring — `kernel/router/`

| # | Mutation | Result |
|---|---|---|
| M8 | the two router weights swapped | killed (1) |
| M9 | min-max normalisation skipped | killed (2) |
| M10 | `(score, index)` tie-break → `score` | **equivalent** |
| M11 | `explain()` output order reversed | killed (1) |
| M12 | raw `queue_pressure` zeroed in the log | killed (1) |
| M13 | raw `best_case_cost` zeroed in the log | killed (1) |
| M14 | `running_jobs` dropped from queue pressure | killed (1) |

### Event log — `kernel/events.py`, `kernel/kernel.py`

| # | Mutation | Result |
|---|---|---|
| E1 | `cycle` always 0 | killed (1) |
| E2 | `seq` never increments | killed (1) |
| E3 | `cycle` never increments | killed (1) |
| E4 | `RecordSink` drops every record | killed (1) |
| E5 | `MultiSink` propagates sink exceptions | killed (1) |
| E6 | `route` records no scores | killed (1) |
| E7 | kernel's sink call unguarded | killed (1) |
| E8 | `cycle_end` never emitted | killed (1) |
| E9 | `PrintSink` drops `dispatch` | killed (14) |

E9 failing 14 blocks is the useful signal: it confirms console output
genuinely flows through the sink rather than a stray `print` left
behind by the refactor.

### QCB timestamps — `kernel/process/qcb.py`

| # | Mutation | Result |
|---|---|---|
| T1 | `submitted_at` never stamped | killed (1) |
| T2 | `dispatched_at` never stamped | killed (1) |
| T3 | `queue_latency` returns 0.0 | killed (1) |
| T4 | `turnaround_time` returns 0.0 | killed (1) |
| T5 | `execution_time` `None` guard removed | killed (1) |
| T6 | `resolved_at` never stamped | killed (1) |
| T7 | `queue_latency` `None` guard removed | killed (1) |

### Workload spec — `benchmark/spec.py`, `providers/base_provider.py`

| # | Mutation | Result |
|---|---|---|
| S1 | unknown spec keys silently accepted | killed (1) |
| S2 | `repeat` ignored (always 1 job) | killed (1) |
| S3 | seed conflict never detected | killed (1) |
| S4 | `set_seed` does not rebuild the RNG | killed (1) |
| S5 | late-`set_seed` guard removed | killed (1) |
| S6 | `exec_on` device ids unchecked | killed (1) |
| S7 | `drain` busy-waits | killed (1) |

### Benchmark runner — `benchmark/runner.py`

| # | Mutation | Result |
|---|---|---|
| R1 | atomic rename removed (partial file left as final) | killed (1) |
| R2 | `--resume` never skips a completed session | killed (1) |
| R3 | failures recorded as `completed` | killed (1) |
| R4 | a crashed session aborts the whole matrix | killed (1) |
| R5 | session ids collide instead of naming the config | killed (1) |
| R6 | `header` record never emitted | killed (1) |
| R7 | default output directory renamed `results/` → `result/` | killed (1) |
| R8 | spec name dropped from the run directory | killed (1) |
| R9 | manifest records the wrong `out_dir` | killed (1) |

### Provider registration — `registry/registry.py`, `devq.py`

| # | Mutation | Result |
|---|---|---|
| P1 | `is_registered()` returns `True` unconditionally | killed (1) |
| P2 | exact-type match relaxed to `issubclass` | killed (1) |
| P3 | the `add_device()` enforcement call deleted | killed (1) |
| P4 | providers accept instances again (`accepts_instance = True`) | killed (2) |
| P5 | the instance check disabled entirely (all kinds) | killed (3) |
| P6 | router built with a hardcoded weight instead of the cascade's | killed (1) |
| P7 | the instance bypass restored in `_build_router` | **inert** |

P1 and P4 both survived first time — see below. P7 is inert rather than
a gap: with instances refused at registration, no instance can reach
`_build_router`, so the branch is unreachable by construction. It was
verified unreachable by inspecting the registry's entries rather than
assumed.

### Repo hygiene — `run_tests.py`

| # | Mutation | Result |
|---|---|---|
| H1 | `Tags:` header removed from a source file | killed (1) |
| H2 | block count in `TEST_BLOCKS.md` left stale | killed (1) |
| H3 | a documented block renamed out of sync with the code | killed (1) |

These guard invariants that break silently rather than loudly — nothing
at runtime depends on them, so only a direct assertion catches a drift.

### Shipped workloads — `benchmark/workloads/`

| # | Mutation | Result |
|---|---|---|
| W1 | a shipped spec's `repeat` changed | killed (1) |
| W2 | a shipped spec made unrunnable | killed (1) |
| W4 | a shipped spec deleted | killed (1) |
| K1 | kept output silently redirected to a temp directory | killed (1) |

W1 initially survived: the assertion computed the expected job count
*from the spec it was checking*, so editing `repeat` moved both sides
together. Counts are now pinned in `run_tests.py`, with a second check
that the spec still declares the same number — so a deliberate change to
an example forces a deliberate change to the pin.

---

## The four that survived first time

Each exposed a real gap and produced a new test block.

**M3 — the alias was dropped and 37 blocks stayed green.** Removing the
device name in `DevQ.build()` broke nothing visible, because
`DeviceContext` carried the alias for every consumer that existed.
Nothing read it off the device. The event log does. → `device_identity`.

**M8 — the router's two weights were swapped and 39 blocks stayed
green.** Every routing test ran on *idle* devices, where queue pressure
is uniformly 0 and normalises to 0 — so `w_queue × 0` vanishes whichever
weight it is. Only asymmetric load can witness the difference. This sat
directly beneath Phase 5.5's premise that a weight sweep means
something. → the loaded fixture in `router_scoring`.

**T5 — a `None` guard was removed and the suite stayed green.** The
assertion checked `turnaround_time` on unfinished jobs but not
`execution_time`. Without its own guard that property raises
`TypeError` on any job that never dispatched, so a metrics pass
iterating every job would crash on the first rejection. → all three
derived properties now asserted.

**P1 — the registration gate was pinned open and 45 blocks stayed
green.** Making `is_registered()` return `True` unconditionally removed
the enforcement entirely, and nothing noticed. Every block registers its
providers correctly, so a gate that never rejects is indistinguishable
from one that works: the *happy path* was covered 45 times over and the
refusal not once. → `provider_registration`.

---

## Two test blocks were self-satisfying when first written

This happened three times. `router_scoring` originally asserted
`explain()` against `select()`.
Both read one shared scoring path, so a mutation moves them *together*
and the comparison still holds — 3 of 7 mutants survived. Fixed by
pinning scores to independently computed values.

`shipped_workloads` later did the same thing in a third costume,
deriving a spec's expected job count from that spec.

A fourth costume appeared with `provider_registration`, and it is worth
recording because the mechanism is different. The assertion was written
as `check(False, ...)` inside a `try` whose `except Exception` followed —
so when the mutant made registration *succeed*, `check(False)` raised its
`AssertionError`, the bare `except` caught it, and the handler reported a
pass. **The test swallowed its own failure.** P4 survived on that alone.
The refusal is now captured into a variable outside the check. An audit
found every other `check(False)` in the suite catches a specific
exception type; only the two written that session were exposed.

The rule this produced: **when a test compares two things that share an
implementation, it is not a test.** It is the same failure as the older
"assert against resolved state, not rendered output" lesson wearing a
different hat — asserting internal self-consistency rather than
external truth.

---

## The equivalent and inert mutants

M10 removes the `(score, index)` tie-break from `NoiseRouter.select()`.
It survives and always will: candidates arrive in index order and
`min()` is stable, so the index term changes nothing any input can
observe.

That is an *equivalent mutant* — a mutation producing a program
behaviourally indistinguishable from the original. No test can kill it.
It is excluded from the score by convention, and the tie-break is kept
because it makes the intent explicit and would matter if a future
candidate pipeline ever reordered.

**Do not write a test for it.** Such a test could not fail, which is
precisely the thing this exercise exists to prevent.

P7 is a second one, of a slightly different kind. It restores the
instance branch in `_build_router` — dead code, because the registry
now refuses instances, so nothing can reach it. The distinction worth
holding onto: M10 is *behaviourally* equivalent for every input, while
P7 is unreachable given a gate upstream of it. Both are excluded, but
P7 would stop being inert the moment that gate changed, so it is worth
re-running rather than retiring. Unreachability was confirmed by
inspecting the registry's entries, not assumed from the code.

---

## Adding a subsystem

Mutation testing is not a one-time exercise. Mutants are per-subsystem,
and anything new needs its own set — the failure modes above cannot
witness a bug in code they never touch. When adding one, write mutants
for the mistakes a reasonable implementation would actually make:
a value silently defaulted, a guard removed, a loop bound off by one,
a field recorded but never read.

Two mechanics worth knowing. The full suite takes ~25 s, so a long
sweep should run in batches rather than one command. And a mutation
that leaves the suite green is not automatically a missing test — check
first whether it is *inert*, like M10, before writing an assertion that
cannot fail.