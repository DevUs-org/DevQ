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

It also honours DevQ's terminal-measurement execution model. A circuit that
uses a qubit **after** measuring it (mid-circuit measurement) — or resets a
qubit after measuring it — is already rejected upstream by
`CircuitRep.find_mid_circuit_measurement`, before it ever reaches the engine,
so the engine never has to represent a post-measurement (collapsed) state. A
reset **before** measurement is a legitimate runnable construct and the engine
handles it as a return of that qubit to |0>.

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
- `engine/statevector.py` *(next layer)* — the state core: gate application,
  reset, terminal measurement, and the `simulate()` (exact probabilities) /
  `run()` (seeded sampled counts) surfaces.