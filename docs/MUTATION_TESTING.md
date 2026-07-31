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

**140 distinct mutants, 137 killed, 3 excluded** (M10 equivalent, P7 and
CC1 inert — see below). Grouped by subsystem. Several were re-run against
`main` after each push to confirm the pushed state matches what was
verified locally; those re-runs are not counted again here.

The total is delta-consistent, not recounted: 136/133/3 from the prior
state plus the 4 new comparison-engine mutants below (all killed — MC-c
and MC-d after `comparison` was strengthened for them). The 136/133/3
itself was 135/132/3 plus the 1 new allocator-sweep-and-capture mutants below (all
killed — MA-b and MA-c after `allocator_scoring` was strengthened for
them), each verified by running the mutant against the affected block,
not assumed. The 131/128/3 itself was 125/122/3 plus the 6
Fidelity mutants (all killed, FID11 after a fixture was added for it).
Earlier deltas: 66/64/2 before the metrics layer, plus the 14 Metrics, 3
Shell, 6 Frontend, 6 OpenQASM 2.0 parser, 6 measurement/execution, and 2
provider-contract mutants. The pre-existing set was taken as given.

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

### Fidelity — `benchmark/metrics.py`, `benchmark/reference.py`, `providers/ibm/`

| # | Mutation | Result |
|---|---|---|
| FID1 | `_marginalise` places a bit by qubit index, not clbit index | killed (1) |
| FID2 | Hellinger drops the outer square (`(1−H²)²` → `1−H²`) | killed (1) |
| FID3 | Hellinger drops the `½` in `H²` | killed (1) |
| FID4 | TVD drops the `½` factor | killed (1) |
| FID5 | `_normalise` zero-fills empty counts (`{}`) instead of `None` | killed (1) |
| FID6 | fidelity folds no-ideal / no-counts jobs in as `0`, not skipped | killed (1) |
| FID7 | measure-all fallback range off by one | killed (fidelity + ibm_measurement) |
| FID8 | Hellinger uses `+` instead of `−` inside the square | killed (1) |
| FID9 | fidelity ignores the per-job `circuit_hash` join, grabs any ideal | killed (1) |
| FID10 | reference-provider capability check inverted | killed (1) |
| FID11 | reference uses `statevector` instead of `density_matrix` | **survived → killed** |

FID1 is the marginalisation survivor: the `swapped_measure` fixture
(`q0 -> c1`, `q1 -> c0`) makes the map-based `"10"` and the qubit-order
`"01"` distinguishable, so a reading that ignores the measure map fails —
a fixture with aligned indices could not catch this (the W1 lesson).
FID8 and FID2/FID3/FID4 are caught by the hand-computed distance values
and the assertion that DevQ's Hellinger equals Qiskit's on shared inputs.
FID9 is killed by the population fixture's two distinct circuits (one with
an ideal, one without): a metric that does not respect per-job identity
mixes them.

**FID11 SURVIVED first, then drove a new fixture.** Switching the
reference from `density_matrix` to `statevector` left the suite green,
because no fixture contained a mid-circuit reset *after entanglement* —
the only case where the two methods diverge. Bell, GHZ, and
`swapped_measure` have no reset, and a reset on an unentangled qubit
stays pure, so statevector matched density-matrix on all of them. The
`reset_entangled` fixture was added (a Bell pair then `reset q0`, leaving
q1 in a mixed 50/50 state that statevector collapses to `{"00": 1.0}`
while density-matrix correctly gives `{"00": 0.5, "10": 0.5}`); with it,
FID11 is killed. This is the survivor lesson again — the missing case, not
the passing suite, is the signal.

### Metrics — `benchmark/metrics.py`

| # | Mutation | Result |
|---|---|---|
| MET1 | interval union counts overlap twice (`sum`, not union) | killed (1) |
| MET2 | population rule disabled (`None` timings not skipped) | killed (1) |
| MET3 | nearest-rank p95 uses `floor` instead of `ceil` | killed (1) |
| MET4 | empty-population throughput returns `0` instead of `None` | killed (1) |
| MET5 | `run()` skips the `write_metrics` pass | killed (1) |
| RR1 | `WAITING` counted as a rejection | killed (1) |
| RR2 | empty-run rejection rate returns `0` instead of `None` | killed (1) |
| RR3 | rejection denominator excludes `WAITING` (wrong population) | killed (1) |
| LB1 | load balance ignores the roster, dropping idle devices | killed (1) |
| LB2 | load-balance CV uses sample stddev (`n-1`) not population (`n`) | killed (1) |
| LB3 | zero-load CV returns `0` instead of `None` | killed (1) |
| LB4 | balance inversion is `1-cv` instead of `1/(1+cv)` | killed (1) |
| LB5 | `run()` writes an empty `devices_attached` roster | killed (1) |
| UT1 | utilisation per-device reverts to bare index keys, not ids | killed (1) |

These are the four claims the module makes, each turned into a mutant and
run against the `metrics` block rather than assumed dead from a green
suite. MET1 is the load-bearing one: summing overlapping intervals gives
dev 0 a utilisation of `90/60 = 1.5`, so the block's `≤ 1.0` and
`= 1.0` assertions on the hand-built fixture both fail — the union is
what makes utilisation a fraction at all. MET2 breaks the skip rule a
rejected job depends on: counting its `None` wait folds a phantom `0`
into the latency distribution and its absent interval into utilisation.
MET3 is why the p95 convention is pinned — `floor(0.95·3)=2` returns the
2nd value `20` where nearest-rank returns the 3rd, `30`; a silent switch
to a library default would move the number without any test noticing
unless the convention is asserted, which it is. MET4 is the
`None`-not-zero rule: a run that rejected everything has an undefined
throughput, and a `0` would misreport it as infinitely slow work rather
than no work. MET5 guards the wiring rather than a formula — `run()`
computes metrics as part of finishing a run, so a run directory is
self-contained (logs, manifest, `metrics.json`); skipping the pass leaves
a directory the comparative modes cannot read, and the `benchmark_runner`
block fails on the missing file. The pass itself is failure-swallowed, so
this mutant tests that the call is *present*, not that it is fatal.

RR1 and RR3 both guard the WAITING-versus-REJECTED distinction from
opposite sides: RR1 wrongly counts WAITING jobs in the numerator, RR3
wrongly drops them from the denominator, and the mixed fixture (one
FINISHED, one WAITING, one REJECTED) catches both — it must read one
rejection of three, and either mutation moves the rate off `1/3`. RR2 is
the counts-versus-ratio rule unique to this metric: an empty run reports
truthful zero counts but a `None` rate, and turning that rate into `0`
would claim a no-op run rejected nothing as a *measured* fact rather than
an undefined one.

LB1 is the load-balance analogue of the whole design: dropping the roster
makes idle devices invisible, so a starved fleet reads as balanced — the
idle-device fixture (all work on device 0, device 1 at zero) catches it.
LB2 and LB4 pin the two arithmetic conventions, population stddev and the
`1/(1+cv)` inversion, against hand-computed values that a library default
or a plausible-looking `1-cv` would move. LB3 is the population rule for
the zero-load case.

LB5 is worth recording because it **survived first**, the same shape as
R10. The runner emits `devices_attached`, and blanking it to `{}` left
the `metrics` block green: load balance falls back to the devices present
in `per_job` when the roster is absent, and on that all-completing
fixture both devices had run, so the fallback recovered them and the
assertion could not tell roster from fallback. One of two paths was
guarded and its twin invisible. Fixed by asserting the full roster
directly on the summary in `benchmark_runner`, where an empty roster now
fails — a green metrics suite around the fallback was not evidence the
roster itself was tested.

UT1 guards the device-id labelling shared by utilisation and load
imbalance: reverting utilisation's per-device keys to bare indices fails
the fixture assertion that the keys are ids (`alpha`, `bravo`), so the
readable-output property cannot silently regress.

### Shell — `shell/qshell.py`

| # | Mutation | Result |
|---|---|---|
| QR1 | `qregistry` ignores flags, always lists all kinds | killed (1) |
| QR2 | unknown-flag guard disabled, bad flag silently accepted | killed (1) |
| QR3 | kinds shown in typed order, not canonical order | killed (1) |

`qregistry` renders the labels map, so these guard the command's own logic
rather than the registry. QR1 catches a flag that stops filtering — the
single-flag test asserts the other kinds are *absent*, so listing them all
fails. QR2 catches a dropped input guard: an unknown flag must error with
no listing, so proceeding to render is caught by the expect-absent on the
provider name. QR3 catches typed-order output — the `s p` test asserts
providers appear before schedulers regardless of typed order, so canonical
ordering cannot regress to input order.

### Frontend dispatch — `frontends/resolver.py`, `shell/parser.py`, `devq.py`, `shell/qshell.py`

| # | Mutation | Result |
|---|---|---|
| F1 | ambiguous-extension branch defeated, first claimant taken silently | killed (1) |
| F2 | explicit `--frontend` override ignored, always extension dispatch | killed (1) |
| F3 | unhandled-extension reject removed, some frontend returned instead | killed (1) |
| F4 | parser drops the `--frontend` flag value | killed (1) |
| F5 | `_build_frontends()` returns an empty map | killed (1) |
| F6 | `frontend` removed from `qregistry`'s kind table | killed (1) |

These guard the per-job dispatch seam, which is what makes several
frontends usable at once. F1 catches an ambiguity that silently resolves:
the block registers two frontends claiming `.qasm` and asserts the job is
rejected naming both, so picking one instead fails. F2 catches a dropped
override — `--frontend=mock` on a `.qasm` file must yield the mock's
nine-qubit circuit, a size `qasm2` can never produce, so falling through
to extension dispatch is caught by the qubit count. F3 catches a reject
that stopped firing: an unhandled `.txt` must be refused *before* the file
is read, so returning a frontend is caught by the expected message. F4
proves the flag survives parsing — dropping its value collapses the
override to extension dispatch, again caught by the qubit count. F5
catches the map that feeds the shell going empty — with nothing to
resolve, the first `qrun` fails to dispatch at all. F6 catches the kind
vanishing from `qregistry f`, caught by the expect on the `Frontends`
heading. Each was run against `frontend_dispatch` and confirmed to turn it
red, then reverted; the files were diffed clean afterward to confirm no
mutation residue.

### OpenQASM 2.0 parser — `frontends/qasm2/`, `circuits/circuit_rep.py`

| # | Mutation | Result |
|---|---|---|
| P1 | binary subtraction becomes addition in the expression evaluator | killed (1) |
| P2 | `^` power ignored, base returned unchanged | killed (1) |
| P3 | custom-gate qubit substitution binds every formal to qubit 0 | killed (1) |
| P4 | `qreg` base offset dropped, so registers stop flattening | killed (1) |
| P5 | `measure` recorded into the gate list instead of its channel | killed (1) |
| P6 | `get_depth()` counts measurements as well as gates | killed (1) |

These guard the ways a parser silently corrupts a circuit rather than
failing loudly. P1 is the reason the `expressions` fixture and block were
strengthened: the mutant **survived** the first version, because every
subtraction in the fixtures was a *unary* minus (`-pi/2`), which takes a
different code path than binary `a - b`. A `3 - 1` case and a
`5 + 2*3` precedence case were added, and only then did P1 die — a green
suite around the original fixture was not evidence the subtraction path
was tested, the same lesson as the LB5/R10 survivors. P2 confirms the
power operator is real, not decorative. P3 catches corrupted qubit
substitution during custom-gate inlining — the mutant maps every inlined
gate onto qubit 0, which the recursive-inline assertion catches by exact
qubit list. P4 catches registers that stop flattening into one index
space (`b[0]` resolves to 0, not 2). P5 and P6 both guard the
measure/reset channel separation that keeps the gate list byte-compatible
with what the providers consume: P5 leaks a measure into the gate list
(caught by the gate-only assertion), P6 leaks it into scheduling depth
(caught by the exact depth value). Each was run against `qasm2_parser`,
confirmed to turn it red, then reverted; the parser files were diffed
clean afterward.

### Measurement & execution — `providers/devq/…`, `providers/ibm/…`

| # | Mutation | Result |
|---|---|---|
| W1 | devq bitstring width uses `num_qubits` instead of `num_clbits` | killed (1) |
| W2 | devq width fallback removed (unmeasured → width 0) | killed (1) |
| M1 | IBM ignores the circuit's explicit `measure` ops | killed (1) |
| M2 | IBM ignores `reset` ops | killed (1) |
| M3 | IBM width uses `num_qubits` instead of `num_clbits` | killed (1) |
| M4 | IBM fallback measure-all fires even when measures are present | killed (1) |

These guard the execution-path change that made measure and reset real
(Option B width: bitstrings span the declared classical register).
W1 is the reason a `narrow_creg` fixture was added: the original
`partial_measure` fixture had `num_clbits == num_qubits == 3`, so "width
is the register" and "width is the qubit count" produced identical
3-bit strings and W1 **survived**. A circuit with three qubits but a
2-bit creg makes the two disagree, and only then did W1 (and its IBM twin
M3) die — the same survivor lesson as P1 and LB5. W2 catches the missing
fallback that would collapse an unmeasured circuit to a single outcome.
M1 catches explicit measures being dropped (partial-measure would lose
its declared width); M2 catches reset being ignored — the assertion is
positional, `x` then `reset` must measure ~0, so a dropped reset (which
would measure ~1) is caught; M4 catches the fallback firing on top of
explicit measures, which double-measures and unpins the c[2] bit. Each
was run against `devq_measurement` or `ibm_measurement`, confirmed to
turn it red, then reverted; both provider files were diffed clean after.

### Provider contract — `providers/base_provider.py`

| # | Mutation | Result |
|---|---|---|
| CW1 | `_counts_width` uses `and` instead of `or` (wrong when a creg is declared) | killed (1) |
| CW2 | `_counts_width` ignores `num_clbits`, always returns `num_qubits` | killed (1) |

`_counts_width` is the single source of the Option B width rule, shared
by both providers so it cannot drift between them. CW1 and CW2 are the
two ways to get the fallback wrong: `and` returns the qubit count
whenever a creg is declared (backwards), and dropping `num_clbits`
entirely loses the register width. Both are caught by
`counts_width_contract`, which asserts the helper directly rather than
only through a provider's end-to-end counts. Each was run against that
block, confirmed red, then reverted; `base_provider.py` was diffed clean.

### Workload spec — `benchmark/spec.py`, `providers/base_provider.py`

| # | Mutation | Result |
|---|---|---|
| S1 | unknown spec keys silently accepted | killed (1) |
| S2 | `repeat` ignored (always 1 job) | killed (1) |
| S4 | `set_seed` does not rebuild the RNG | killed (1) |
| S5 | late-`set_seed` guard removed | killed (1) |
| S6 | `exec_on` device ids unchecked | killed (1) |
| S7 | `drain` busy-waits | killed (1) |
| S8 | scalar coercion is a no-op (returns the string unchanged) | killed (1) |
| S9 | header records the RESOLVED spec instead of verbatim | killed (1) |
| S10 | `seed_requested` reverts to the resolved int | killed (1) |

S3 (*seed conflict never detected*) was retired, not killed: class-only
registration removed the negotiation it broke, so nothing can hold a
competing seed and the conflict is unrepresentable rather than merely
untested. The `workload_spec` block pins that by asserting an instance
is refused at registration.

S8–S10 cover the placeholder feature's failure modes in `spec.py`. S8 is
the self-satisfying-test guard made concrete: a coercion that silently
returned its input would pass any "did not raise" check, so the block
asserts the OUTPUT TYPE (`"42"` becomes int `42`), which S8 violates. S9
and S10 are the secret-leak guards — S9 is the exact bug found this
session, where the header recorded the resolved spec, so a resolved
`${IONQ_API_KEY}` would reach disk; both are caught by the
`placeholders.json` assertions that the header keeps `${NAME}` literal.

### Placeholder resolution — `benchmark/placeholders.py`

| # | Mutation | Result |
|---|---|---|
| PH1 | an unset `${NAME}` resolves to `''` instead of raising | killed (1) |
| PH2 | lookup recases (`${name}` falls back to `NAME`) | killed (1) |

PH1 is the mutation-critical refusal, the same shape as P1: a resolver
that never raises on a missing variable is indistinguishable from a
working one across every happy-path spec, so the `placeholder_resolution`
block asserts the rejection directly rather than inferring it from the
passes. PH2 guards case-sensitivity — a recasing fallback would resolve
`${seed}` from `SEED`, a silent wrong-value of exactly the kind DevQ
refuses elsewhere; the block sets `DEVQ_T_SEED` and asserts
`${devq_t_seed}` does *not* find it. (The no-op-coercion mutant lives
under Workload spec as S8, since coercion is `spec.py`'s job, not the
resolver's — the resolver only ever emits strings.)

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
| R10 | manifest records the RESOLVED spec instead of verbatim | killed (2) |

R10 survived first time: the header leak (S9) was asserted but the
manifest, written to disk beside the log, was not — so a resolved
`${SECRET}` in the manifest passed every check. `shipped_workloads` now
reads `manifest["spec"]` for the placeholder spec and asserts the
placeholder stays literal, closing the second leak site. It is a paired
guard with S9: the log and the manifest are both published artifacts,
and a credential must not survive resolution into either.

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
| H4 | shipped specs validated RAW, without resolving `${NAME}` | killed (1) |

H4 confirms the block resolves before validating: reverted to
`json.load` + `validate_spec`, the placeholder spec's raw `${DEVQ_SEED}`
fails integer coercion and the block's own "validates" assertion goes
red. A placeholder spec is only well-formed after resolution, so a
hygiene check that skipped it would wrongly reject a valid shipped spec.

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

### Unrunnable-circuit detection and rejection — `circuits/circuit_rep.py`, `frontends/qasm2/parser.py`, `kernel/kernel.py`, `benchmark/spec.py`

DevQ declines a circuit it cannot faithfully run — a well-formed but
unsupported construct (classical control, mid-circuit measurement) or
malformed source that fails to parse — and surfaces it as a REJECTED job
with a reason rather than dropping it, forging a number, or crashing the
run. These mutants attack each link of that chain.

| # | Mutation | Result |
|---|---|---|
| MC1 | `find_mid_circuit_measurement` always returns None (never detects) | killed (1) |
| MC2 | the gate-after-measure check disabled (`if False and ...`) | killed (1) |
| MC3 | `_parse_if` no longer calls `_mark_unrunnable` | killed (1) |
| MC4 | `parse()` skips the mid-circuit scan (result never applied) | killed (1) |
| MC5 | the qrun-path kernel guard neutralised (`reason = None`) | killed (1) |
| MC6 | the scheduling-path kernel guard neutralised | killed (1) |
| RJ1 | the `reject` event drops `circuit_label` | killed (1) |
| SP1 | a parse failure re-raises `SpecError` instead of a placeholder job | killed (1) |
| SP2 | the placeholder is built but never marked unrunnable | killed (1) |
| SP3 | every unparseable placeholder gets one constant hash (collision) | killed (1) |
| CC1 | the classical-control reason wording changed ("mid-circuit"→other) | **inert** |

MC6 survived first. The kernel has TWO unrunnable-circuit guards — one on
the `qrun` fast path, one on the scheduling (`qrunpack`) path — and
`rejection_semantics` exercised only the first, so neutralising the second
changed nothing observable. The block now submits an unrunnable circuit
via the queue and drains it, so both guards are covered; MC6 is killed.

SP3 survived first, twice over. The first attempt was a no-op mutation
(`chash = "" or hashlib...` still evaluates to the hash); the second, a
genuine constant-hash collision, passed because the block had only ONE
malformed circuit and a collision needs two to be visible. The
`unrunnable_circuits` block now runs two DIFFERENT malformed circuits and
asserts their hashes differ — the collision that made rejected rows print
a shared bare hash. Both drove the block that now exists to catch them.

CC1 is **inert**, like P7: it changes only the human-readable wording of a
rejection reason, which no test asserts on verbatim (and none should — the
exact prose is not a behavioural contract). The reason is checked for the
substring that carries meaning ("feedback"), not for its full text, so a
cosmetic reword leaves the suite correctly green. Recorded, not counted as
a gap.

### Sweepable contract and cost decomposition — `kernel/sweep.py`, `kernel/router/noise_router.py`

Phase 5.5a. The α/β sweep re-weights the raw per-candidate cost
decomposition, and both the decomposition and the shared `Sweepable`
machinery that derives `explain()`/sweep for every scoring component are
mutation-checked. Six mutants, all killed; blocks `router_scoring` and
`sweepable_contract`.

- **M-a — swapped decomposition sums.** Returning `edge_cost, qubit_cost`
  instead of `qubit_cost, edge_cost` from `_best_case_cost`. Killed by
  `router_scoring`'s pinned `qubit_error_sum`/`edge_error_sum` — the
  reason those sums are pinned to externally computed values and not
  merely asserted present.
- **M-b — dropped α/β weighting in `_sweep_score`.** Summing the raw sums
  without weighting. Killed by the `α·Σq + β·Σe` reproduces
  `best_case_cost` invariant and the sweep-matches-live checks.
- **M-c — collapsed the router weight separation in `_sweep_rank`.**
  `w_queue · p + w_queue · c` instead of `+ w_noise · c`. Killed by the
  faithfulness anchor at asymmetric weights.
- **MS-b — `min` → `max` in the base `sweep_decision`.** Killed by
  `sweepable_contract`'s argmin-replay assertion.
- **MS-c — base `explain_decision` ignores the `NOT_SCORED` sentinel**
  (builds an empty report instead of returning `None`). Killed: a
  non-scoring component reaching `_sweep_rank` raises, and the not-scored
  `None` assertion catches it.

**MS-a SURVIVED first, then drove a fixture change.** The mutant made the
base `explain_decision` report the raw `_sweep_score` output instead of
the ranked final from `_sweep_rank`. It survived because the original
`ToyScorer` double's rank echoed the score unchanged, so raw and ranked
were indistinguishable. Adding a `+100` rank offset to the double made the
two witnessably different, and the mutant is now killed — the same
survive-then-strengthen pattern as FID11 and the unrunnable-circuit
mutants. A test double that does not differ observably from the code path
it exercises cannot witness a mutation to that path.

### Allocator sweep and per-job capture — `kernel/memory/allocators/noise_graph_allocator.py`, `kernel/kernel.py`, `kernel/scheduler/`

Phase 5.5a. The allocator is the second `Sweepable` component: it logs the
per-block cost decomposition (the `allocate` event) and its decision is
pinned per-job so a batch scheduler cannot clobber it. Four mutants, all
killed; block `allocator_scoring`.

- **MA-a — swapped decomposition sums** in the general-path enumeration.
  Killed by the pinned `qubit_error_sum`/`edge_error_sum` per block.
- **MA-d — dropped α/β weighting in the allocator's `_sweep_score`.**
  Killed by the reproduces-S invariant and the pinned swept block choice.

Two survived first and drove the block's per-job assertions:

- **MA-b — the kernel reads the allocator's live `_last_decision` at
  dispatch** instead of the job's pinned `alloc_decision` (reintroducing
  the batch clobber). It SURVIVED the first assertion — distinctness of
  candidate sets across jobs — because on the shipped workload two jobs
  with different pool states differ even under the clobber. The killing
  assertion is **dispatch-to-allocate parity**: under the clobber, jobs
  whose decision was overwritten before dispatch emit no `allocate` event,
  so the dispatched-job and allocated-job id sets diverge. Distinctness
  measured the wrong thing; parity measures the actual invariant.
- **MA-c — dropped the per-job capture in the base scheduler**
  (`_attempt_allocation`). It SURVIVED because the shipped smoke workload
  drives the *packing* scheduler, whose capture is in a different method
  (`_try_allocate_temp`) — the base/serial path was untested. The block
  now drives an FCFS scheduler directly and asserts the decision lands on
  the job, exercising the path a batch-only workload never touches.

Both are the same lesson: a witness workload that does not force the
discriminating case leaves an assertion decorative. The clobber needed a
parity check, and the serial-path capture needed a serial scheduler.

**MSch — `BaseScheduler` stripped of `Sweepable`.** Killed by the
scheduler-parity assertions in `sweepable_contract`: a scoring mock
scheduler loses `is_sweepable`/`explain_decision` and the block fails.
This pins the third component onto the shared contract — the scheduler
inherits the same sweep machinery as the router and allocator, so the QOS
baseline in 5.6 is sweepable with no base-class change.

### Comparison engine — `benchmark/comparison.py`

Phase 5.5a. The cross-config engine: matrix assembly and the α/β sweep
driver. Four mutants, all killed; block `comparison`.

- **MC-a — `_cost_params` ignores α** (constant 0.5/0.5 weights). Killed:
  a sweep that does not vary weights produces no block-choice flip, so the
  allocator-flip assertion fails.
- **MC-b — `_sweepable_axes` returns every axis** regardless of the log.
  Killed: a cost-oblivious `graph` allocator would be reported sweepable,
  failing the axis-detection assertion.

Two survived first and drove the block's refusal assertions:

- **MC-c — the `is_sweepable()` guard removed** from `sweep`. It SURVIVED
  because a non-scoring allocator, allowed past the guard, then hits the
  empty-decisions branch and is refused anyway — so a refusal still
  happened, just via the wrong path. The killing assertion pins the
  *reason*: the refusal must name the component non-scoring, distinct from
  "no decisions found". Two refusal paths that a coarse `faithful is False`
  check cannot tell apart.
- **MC-d — the faithfulness anchor defanged** (`if replayed != winner`
  became `if False`). It SURVIVED because every faithful run reproduces
  its recorded winner, so the anchor never fires and removing its teeth is
  invisible. Killed by a planted fixture: a log whose recorded winner
  contradicts its own scores, which the anchor must refuse. A guard that
  only acts on bad input needs bad input to be tested — no honest run can
  witness it.

---

## The five that survived first time

Each exposed a real gap and produced a new test block or assertion.

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

**R10 — the manifest leak hid behind the header guard.** The header was
asserted to keep `${NAME}` literal (S9), so it was tempting to think the
leak was covered — but the manifest is a *second* artifact written to
the same directory, and nothing read its spec. A resolved `${SECRET}`
there would ship just as surely. The same one-of-two-sites blind spot as
P1: the guarded site passed and the unguarded twin went unnoticed. →
`shipped_workloads` now asserts the manifest verbatim too. The lesson
generalises to 5.8: every path that writes a spec to disk is a leak
site, and each needs its own assertion — one is not proxy for another.

---

## Three test blocks were self-satisfying when first written

This happened three times. `router_scoring` originally asserted
`explain()` against `select()`.
Both read one shared scoring path, so a mutation moves them *together*
and the comparison still holds — 3 of 7 mutants survived. Fixed by
pinning scores to independently computed values.

`shipped_workloads` later did the same thing in a second costume,
deriving a spec's expected job count from that spec.

A third costume appeared with `provider_registration`, and it is worth
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