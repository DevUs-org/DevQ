# The Native Statevector Engine

`engine/` is DevQ's Qiskit-free simulation engine: a pure-Python (numpy)
statevector simulator that interprets a `CircuitRep`'s gates directly. Its
purpose is narrow and specific — to compute a circuit's **noiseless ideal**,
the yardstick the Phase 5.4 fidelity metric compares each measured
distribution against, **without** requiring an Aer-backed reference-capable
provider to be attached to the workload.

## Why it exists

Fidelity needs an ideal, and today the only source of one is a
reference-capable provider — in practice an `ibm.simulated` device running a
noiseless Aer density-matrix simulation (see [`METRICS.md`](METRICS.md), "The
ideal"). That forces an awkward shape on a user who only wants to compare, say,
two schedulers on a mock device: they must also attach an IBM device purely to
supply ideals, and then exclude it from routing on every job (`no_exec_on`) so
it does not distort the comparison it is only there to measure. The device is
scaffolding, not part of the experiment.

The native engine removes that scaffolding. Because it computes the ideal from
the circuit alone — no backend, no noise model, no Qiskit — the fidelity layer
can obtain an ideal for any circuit in the engine's vocabulary regardless of
which providers are attached. When the engine cannot handle a circuit (a gate
outside its vocabulary), the reference path falls back to a registered
reference-capable provider if one is attached, and failing that records no
ideal — fidelity `None`, exactly the honest undefined the metrics layer
already documents, never a forged value.

## What it is not

The engine is a leaf. It touches no provider, no kernel state, no routing. It
consumes a `CircuitRep` and returns probabilities (the exact ideal) or sampled
counts. It does not model noise — that is what the Aer-backed providers are
for; the engine is the *noiseless* reference, and mixing noise into it would
defeat its one job.

It handles DevQ's execution model on two paths. For a plain circuit
(terminal measurement only) it applies gates and resets to one statevector
and marginalises — the fast path. A circuit that uses a qubit **after**
measuring it (mid-circuit measurement) or uses classical feedback
(conditional gates) is routed to a separate **exact branch-enumeration**
path (collapse-and-continue): a mid-circuit measurement produces a classical
mixture no single statevector can hold, so the engine represents it exactly
as a set of weighted pure branches, splitting at each measurement, applying
each conditional only to branches whose recorded classical bits satisfy it,
and reading the ideal from each branch's classical register. This is exact
and deterministic — no sampling, no seed. (These circuits were once rejected
upstream by `CircuitRep.find_mid_circuit_measurement`; that became a
per-device capability, so the engine now computes their ideals rather than
never seeing them.) A reset **before** measurement is a legitimate runnable
construct on both paths — a return of that qubit to |0>; a reset on a qubit
still entangled (with no measurement having separated it) is declined, its
mixed result having no statevector form.

## The gate vocabulary

`engine/gates.py` defines the gate set as small matrices — 2x2 for each
one-qubit gate, a single 4x4 for `ecr` (the one intrinsically-two-qubit
unitary), and permutation structure for `swap`, `ccx`, and `cswap`. Every
other multi-qubit gate is a **controlled**-U: the state core embeds the 2x2
into the n-qubit state by bit-indexing rather than storing a 2^n matrix.

The vocabulary is **exactly** the qasm2 frontend's `_BUILTIN_GATES` — 32 gate
names — asserted equal (not merely subset) by the `engine_gates` test block.
Equality matters in both directions: the engine must simulate everything the
frontend can emit, and must not claim a gate the frontend cannot produce. The
32 names break down as 19 one-qubit, 11 two-qubit, and 2 three-qubit gates.

Custom `gate` definitions do not widen this set. The qasm2 frontend inlines
every custom gate **recursively** at parse time — binding its formal
parameters and qubits at each call site — so a `CircuitRep`'s instruction
stream contains only builtin gate names by the time any consumer sees it. A
circuit defining `gate entangle(θ) a,b { rz(θ) a; cx a,b; }` reaches the
engine as a stream of `rz` and `cx`, not as an `entangle` op. The engine
therefore needs no custom-gate machinery: covering `_BUILTIN_GATES` covers
every circuit the parser can produce. (An `opaque` gate, which has no body to
inline, is rejected by the frontend before it becomes a circuit at all.)

Four of the 32 are exact rewrites of another gate and share its builder rather
than duplicating a matrix — verified by matrix equality, not assumed:

| alias | resolves to | why |
|---|---|---|
| `u1(λ)` | `p(λ)` | identical matrix `diag(1, e^{iλ})` |
| `cu1(λ)` | `cp(λ)` | identical matrix, controlled |
| `u2(φ,λ)` | `u(π/2, φ, λ)` | parameter packing of the general gate |
| `u3(θ,φ,λ)` | `u(θ, φ, λ)` | identical to `u` |

Folding the aliases, the file defines 28 distinct matrix builders behind the
32 names.

## Correctness discipline

Every matrix in `engine/gates.py` was checked identical to Qiskit's `Operator`
for the corresponding gate — constants exactly, parameterised gates across
several angles including the edges (0, π) where a sign or half-angle slip
hides. This is asserted live in the `engine_gates` block, not trusted from
memory: the block rebuilds each gate through Qiskit and compares, so a matrix
that drifts from Qiskit's definition fails the suite. The comparison also pins
**tensor ordering** — DevQ is little-endian throughout (qubit 0 is the
least-significant basis bit, matching Qiskit), and a big-endian slip in a
controlled gate flips control and target and is caught here.

The vocabulary parity and matrix correctness together are what let the engine
be trusted as a reference: a wrong matrix would produce a silently-wrong ideal,
against which any measured distribution yields a high, plausible, and
meaningless fidelity — the worst kind of failure, invisible in a green suite.
Locking the matrices against Qiskit before any engine code consumes them is the
guard against exactly that.

## Layout

Both files carry a `Tags: Main` header — the engine is core infrastructure
(a leaf the reference/fidelity layer consumes), not a provider or a pluggable
component — and the `repo_hygiene` block walks `engine/` for that header
alongside DevQ's other own packages.

- `engine/gates.py` — the locked gate vocabulary: base matrices, parameterised
  builders, alias resolution, the `GATES` registry, `gate_spec()` lookup
  (raising `UnknownGateError` for a name outside the vocabulary), and
  `vocabulary()`.
- `engine/statevector.py` — the state core: gate application by
  bit-indexing (one-qubit, controlled, ecr, swap, ccx, cswap — reusing the
  little-endian embedding verified in `engine_gates`), reset, terminal
  measurement, `simulate()` returning the exact `{bitstring: probability}`
  ideal at the Option-B classical width, and `run(circuit, shots, seed)`
  returning seeded integer counts sampled from that exact distribution. It
  implements the `BaseProvider` output contract directly — width via
  `BaseProvider._counts_width`, clbit placement, and the measure-all
  fallback — so its keys align with any provider's for a fidelity comparison,
  without importing or depending on any provider.

## simulate() versus run()

`simulate()` returns exact probabilities — the noiseless *ideal*, the
fidelity yardstick, seedless and deterministic. `run(circuit, shots, seed)`
draws `shots` outcomes from that exact distribution and returns integer
counts, the counts a noiseless execution of the circuit would produce. The
two serve different roles: the ideal is what a measured distribution is
*compared against*; sampled counts are a gate-honest noiseless *execution*
for a caller who wants counts without an Aer-backed device (unlike the
uniform mock `devq.simulated` returns, which does not interpret gates). The
seed is explicit on `run()` because it is a standalone function, not a
provider with a submission counter; a fixed seed reproduces counts exactly,
and both surfaces propagate the engine's decline (`UnsupportedByEngine`,
`UnknownGateError`) rather than swallowing it.

## The engine as a reference source

The engine's reason for existing is to free a run from having to attach a
reference-capable device (and then exclude it from routing on every job with
`no_exec_on`) purely to obtain ideals. It slots into `benchmark/reference.py`
as the middle of a **three-tier precedence**, applied per circuit:

1. an **attached** reference-capable provider wins outright for the run;
2. else the **core native statevector engine** computes the exact ideal for
   pure circuits within a qubit cap (`_ENGINE_MAX_QUBITS`, 20 — a 2^20
   complex vector is 16 MB; the cap is a memory guard, not a correctness
   one);
3. else a **registered** provider class overriding `reference_ideal` is
   instantiated unattached and used — for what the engine declines (an
   entangled reset, or a circuit above the cap), internalising the dry-run
   hand-roll.

Per-circuit fallback between tiers 2 and 3 is safe because a noiseless ideal
is mathematically unique and every tier returns normalised probabilities (not
shot-quantised counts), so two tiers can never disagree on a given circuit's
ideal. A density matrix costs 2^n × 2^n (16 TB at n=20) against the
statevector's 2^n (16 MB), so the engine is the cheap exact primary for pure
circuits and the density-matrix tier the fallback for the genuinely
mixed-state cases — not a degraded engine, the right tool per circuit.

## The reset boundary

A `reset` returns a qubit to |0>. A statevector can represent that *exactly*
only when the qubit is **separable** — its reduced state is pure, so the
post-reset whole-register state is still a pure product |0> ⊗ (rest). The
engine handles this common case (a leading reset, or a reset on a qubit not
yet entangled) precisely, moving the qubit's population to |0> and
renormalising.

When the reset qubit is **entangled**, the reset leaves the rest of the
register in a genuinely *mixed* state — resetting q0 of a Bell pair leaves q1
in a 50/50 mixture whose correct ideal is `{"00": 0.5, "10": 0.5}`. A pure
statevector cannot hold that; it would collapse it to `{"00": 1.0}`, a
plausible and wrong ideal. So the engine detects this — forming the qubit's
reduced density matrix and testing purity (`Tr(ρ²) == 1`) at the reset — and
**declines** the circuit (`UnsupportedByEngine`) rather than emit the wrong
distribution. The reference path then falls back to a reference-capable
provider, or records no ideal (fidelity `None`) — the same honest handoff as
an unknown gate. Simulate what can be simulated exactly; hand off what
cannot.

A reset (or gate) on a qubit after it has been measured is mid-circuit
measurement. This is no longer rejected upstream — it is a per-device
capability — so such a circuit reaches the engine and is handled by the
branch-enumeration path, where the measurement has already split the state
into pure branches and the reset acts on a (now separable) collapsed qubit
within each branch. Only a reset on a qubit still entangled within a branch
is declined.