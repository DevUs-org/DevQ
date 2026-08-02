# DevQ References

Every external work DevQ builds on, cites, or borrows a definition from —
academic papers, software dependencies, benchmark suites, and the
provenance of the calibration data the IBM-simulated provider uses. This
file is the single bibliography the other docs and the code point into:
an in-text marker like `[QOS]` or `[Qiskit-HF]` at a use-site (in prose or
a code comment) resolves here.

It serves two purposes. First, ATTRIBUTION: DevQ replicates formulas and
borrows rationale from published work, and every such use is recorded at
its point of use and collected here, so provenance is never ambiguous.
Second, PAPER PREPARATION: when the DevQ paper is written, its
bibliography and its "we use X" statements draw from this file rather than
being reconstructed from memory.

> **Compliance items marked _(verify before submission)_ are not settled
> by this file.** License terms and trademark guidelines change and are
> not asserted here from memory. Each such item points at the primary
> source to read directly before publishing or releasing. This file
> records WHAT to check and WHERE, not a legal determination.

---

## How to cite from here

- In a doc, mark a borrowed definition or claim inline with its key, e.g.
  "Hellinger fidelity `[Qiskit-HF]`, as used by `[QOS]`".
- In code, a short comment at the use-site names the key, e.g.
  `# Hellinger fidelity; see docs/REFERENCES.md [Qiskit-HF]`.
- Keys are stable; the full entry (authors, venue, URL, DOI) lives only
  here, so a citation never drifts across files.

---

## Academic references

### [QOS] — the closest comparable system, and the source of the fidelity metric choice
Emmanouil Giortamis, Francisco Romão, Nathaniel Tornow, Pramod Bhatotia.
"QOS: Quantum Operating System." 19th USENIX Symposium on Operating
Systems Design and Implementation (OSDI '25), Boston, MA, 2025,
pp. 429–447. arXiv:2406.19120.
- USENIX: https://www.usenix.org/conference/osdi25/presentation/giortamis
- arXiv: https://arxiv.org/abs/2406.19120

The nearest system-level comparable to DevQ, and the benchmark a DevQ
paper must position against. QOS evaluates **Hellinger fidelity** (as
defined by Qiskit, `[Qiskit-HF]`) as its execution-quality metric,
alongside utilisation and waiting time — which is why DevQ's fidelity
metric matches that exact definition, so a cross-system comparison is
like-for-like rather than a comparison of two differently-defined
"fidelities". See [`METRICS.md`](METRICS.md) (fidelity) and
[`ROADMAP.md`](ROADMAP.md).

### [Qiskit-HF] — the exact fidelity definition DevQ replicates
Qiskit `quantum_info.hellinger_fidelity` — the Hellinger fidelity between
two count distributions, ranging `[0, 1]`, higher-is-better.
- https://docs.quantum.ibm.com/api/qiskit/qiskit.quantum_info.hellinger_fidelity

DevQ computes fidelity from the formula directly (it does not import
Qiskit into the pure metric layer), and the `metrics` test asserts DevQ's
value equals Qiskit's `hellinger_fidelity` on shared inputs, so the
headline number is provably this definition. The fidelity is derived from
the Hellinger distance `[Hellinger]` as `(1 − H²)²`.

### [Hellinger] — the Hellinger distance between probability distributions
The Hellinger distance `H(P, Q) = (1/√2)·√(Σₖ (√pₖ − √qₖ)²)`, ranging
`[0, 1]`, `H = 0` iff `P = Q`. A standard statistical distance; stated in
this form in, among others:
- QEMI (arXiv:2602.09942), §3.3 — states the formula and notes Hellinger
  is well defined on zero-probability events, unlike KS or cross-entropy.
- IQM noise-model benchmarking (arXiv:2508.04483), §IV.A — states the same
  formula and the disjoint-support property `[GHZ-rationale]`.

DevQ's headline fidelity is derived from this distance; TVD `[TVD]` is
reported alongside as a hand-verifiable companion.

### [GHZ-rationale] — why Hellinger suits GHZ-like ideals
The rationale that Hellinger distance handles distributions whose supports
differ — an ideal concentrated on a few bitstrings versus a noisy result
smeared across many — is drawn from the IQM benchmarking work
(arXiv:2508.04483, §IV.A), which notes this is exactly the GHZ case (the
noiseless GHZ ideal has mass on two strings while the noisy histogram
spreads over all), and that classical fidelity neglects the
zero-probability outcomes Hellinger accounts for. DevQ records this as the
stated reason for choosing Hellinger over classical fidelity for its
GHZ-heavy workloads; the argument is borrowed, not original.

### [TVD] — total variation distance
Total variation distance `TVD(P, Q) = (1/2)·Σₖ |pₖ − qₖ|`, ranging
`[0, 1]`, lower-is-better. A standard statistical distance (a textbook
reference such as Levin & Peres, *Markov Chains and Mixing Times*, suffices
_(verify before submission)_ if a citation is wanted). DevQ reports TVD as
the hand-verifiable companion to Hellinger fidelity — it is trivially
checkable by eye, and is a numerically distinct quantity from Hellinger on
the same inputs, so the two together guard against a swapped-formula
regression.

### [Scheffe-Mixtures] — the simplex-lattice the weight sweep enumerates
Henry Scheffé. "Experiments with Mixtures." *Journal of the Royal
Statistical Society, Series B* 20(2):344–360, 1958.
- https://www.jstor.org/stable/2983895

The Phase 5.5c weight sweep enumerates a component's normalised weight
group (n terms summing to 1) over the **Scheffé {n, m} simplex-lattice**:
every weight n-tuple whose entries are multiples of `1/m`, i.e. the integer
compositions of `m` into `n` parts divided by `m`. DevQ uses Scheffé's
construction and his point-count `C(m+n−1, n−1)` directly. At `n=2` the
lattice is the historical `(α, 1−α)` grid; at `n≥3` it tiles the triangle /
tetrahedron / … . Cited at the use-site in `benchmark/comparison.py`
(`_simplex_lattice`, `_int_lattice`). See [`COST_MODEL.md`](COST_MODEL.md#answering-the-sweep-from-one-recorded-run-phase-55a) and [`EXTENDING.md`](EXTENDING.md).

### Elementary results used in the sweep (stated inline, not attributed)
Two facts the sweep rests on are elementary and are proved inline at their
use-site rather than cited, since manufacturing an authoritative reference
for textbook material would misrepresent it:
- **Scale-invariance of a linear ranking.** The arg-min of `w · x` over
  candidates is unchanged when `w` is multiplied by any positive scalar, so
  only the *direction* of the weight vector matters — which is why the
  faithful search space is the simplex regardless of whether a component's
  weights are stored normalised or raw. (A one-line proof accompanies the
  code; Boyd & Vandenberghe, *Convex Optimization* (2004), is a "see also"
  for level-set scaling if a reference is ever wanted.)
- **Piecewise-constant winner surface.** The winner a weight point induces
  is constant within cells and jumps across straight tie-loci; the objective
  is therefore non-smooth with no useful gradient, which is why the sweep
  *enumerates* the simplex rather than descending it. For the general
  "non-smooth, black-box ⇒ enumerate/grid" context, Conn, Scheinberg &
  Vicente, *Introduction to Derivative-Free Optimization* (SIAM, 2009), is a
  contextual reference, not a prescription of DevQ's exact method.

---

## Software dependencies

Authoritative pinned versions are in `requirements.txt`; the notes on
version-sensitivity in [`ROADMAP.md`](ROADMAP.md) (Phase 8) and
[`TEST_BLOCKS.md`](TEST_BLOCKS.md) apply — fake-backend calibration numbers
are tied to the pinned `qiskit-ibm-runtime`.

### [Qiskit] — Qiskit
The quantum SDK DevQ's IBM-simulated provider and reference generator lower
circuits into (`QuantumCircuit`), transpile with, and read exact
probabilities from.
- https://www.ibm.com/quantum/qiskit
- Source / license: https://github.com/Qiskit/qiskit — Apache License 2.0
  _(verify before submission: retain the Apache-2.0 NOTICE/attribution
  requirements if any Qiskit-derived code or data is redistributed)_.

### [Qiskit-Aer] — Qiskit Aer
The high-performance simulator DevQ uses for both noisy execution (with a
fake-backend noise model) and the **noiseless density-matrix reference
run** that produces each circuit's ideal distribution. Density-matrix
method is used for the reference because it honours mid-circuit `reset`, a
non-unitary operation statevector evolution cannot represent.
- https://github.com/Qiskit/qiskit-aer — Apache License 2.0 _(verify
  before submission)_.

### [Qiskit-IBM-Runtime] — qiskit-ibm-runtime (fake backends)
Source of the `FakeXxxV2` backends whose IBM device snapshots supply the
calibration data the IBM-simulated provider uses. See **Data provenance**
below for the important provenance and staleness caveats.
- https://github.com/Qiskit/qiskit-ibm-runtime — Apache License 2.0
  _(verify before submission)_.

### [OpenQASM2] — OpenQASM 2.0
The quantum assembly language DevQ's built-in `qasm2` frontend reads. DevQ
ships a complete, dependency-free OpenQASM 2.0 parser.
- Cross, Bishop, Smolin, Gambetta. "Open Quantum Assembly Language."
  arXiv:1707.03429 (2017).
- https://arxiv.org/abs/1707.03429

### [NetworkX] — NetworkX
The graph library backing DevQ's topology representation (`TopologyGraph`)
and BFS-based allocators.
- Hagberg, Schult, Swart. "Exploring Network Structure, Dynamics, and
  Function using NetworkX." Proceedings of the 7th Python in Science
  Conference (SciPy 2008), pp. 11–15.
- https://networkx.org — BSD License _(verify before submission)_.

---

## Benchmark suites

### [QASMBench] — QASMBench (Phase 5.4 workload suite)
Ang Li, Samuel Stein, Sriram Krishnamoorthy, James Ang. "QASMBench: A
Low-Level Quantum Benchmark Suite for NISQ Evaluation and Simulation." ACM
Transactions on Quantum Computing, 2022. DOI:10.1145/3550488. (Supersedes
the 2020 preprint arXiv:2005.13018 — cite the ACM TQC version.)
- https://github.com/pnnl/QASMBench — **BSD License**.
- DOI: https://doi.org/10.1145/3550488

The OpenQASM benchmark suite DevQ's 5.4 workload set draws circuits from.
Methodologically aligned with DevQ's fidelity work: the QASMBench paper
itself measures execution fidelity of its applications on IBM-Q machines
via density-matrix state tomography, the same density-matrix basis DevQ's
noiseless reference uses.
> **_(verify before submission / before vendoring circuits)_** BSD
> requires retaining the copyright notice and license text. When Phase 5.4
> vendors `.qasm` fixtures into `test_circuits/`, QASMBench's LICENSE and
> attribution must travel with them (e.g. a `test_circuits/qasmbench/`
> subtree carrying the upstream LICENSE and a note of the source commit).

### [SuperMarQ] — SuperMarQ (candidate, not yet included)
Teague Tomesh, Pranav Gokhale, Victory Omole, Gokul Subramanian Ravi,
Kaitlin N. Smith, Joshua Viszlai, Xin-Chuan Wu, Nikos Hardavellas, Margaret
R. Martonosi, Frederic T. Chong. "SuperMarQ: A Scalable Quantum Benchmark
Suite." 2022 IEEE International Symposium on High-Performance Computer
Architecture (HPCA), 2022, pp. 587–603.
- Recorded now for paper-prep; license and canonical repo to be pinned
  _(verify before submission)_ if/when SuperMarQ is actually included.

---

## Data provenance & trademark notice

### IBM device calibration data (via fake backends)
The IBM-simulated provider does not connect to IBM hardware. It uses the
`FakeXxxV2` backends from `qiskit-ibm-runtime` `[Qiskit-IBM-Runtime]`,
which carry **system snapshots of real IBM Quantum devices** — coupling
map, basis gates, and qubit properties (T1, T2, error rates). This is what
makes DevQ's noisy simulation representative of real hardware behaviour.

Two caveats, both load-bearing for the paper:

1. **The snapshots are historical.** Per IBM's own documentation, a fake
   backend's noise model is generated from a snapshot taken in the past
   (sometimes years earlier) and is *not* representative of the current
   behaviour of the real system it mimics. DevQ's fidelity numbers are
   therefore against a **pinned historical calibration snapshot**, not
   live hardware — state this explicitly rather than implying live-device
   fidelity.
2. **Calibration is version-pinned.** The exact numbers depend on the
   pinned `qiskit-ibm-runtime` version (see `requirements.txt`); a version
   bump silently changes them. `TEST_BLOCKS.md` reference values assume
   the pinned version. This is the reproducibility prerequisite
   `ROADMAP.md` Phase 8 records.

### Trademark and independence _(verify before submission)_
"IBM", "Qiskit", "IBM Quantum", and the device/backend names (Nairobi,
Lagos, Sherbrooke, Eagle, Heron, Falcon, etc.) are trademarks or product
names of their respective owners. DevQ is an **independent, unaffiliated**
research project; its use of these names is **nominative** — accurately
identifying the open-source tooling and the origin of calibration data it
builds on — and does **not** imply endorsement, affiliation, or
partnership. The provider identifier `ibm.simulated` names the data source,
not a relationship.

> Before publishing or releasing, read the primary sources directly and
> add an appropriately-worded disclaimer to the README/paper: IBM's
> trademark guidelines, and the Apache-2.0 / BSD attribution requirements
> of the dependencies above. This note records the obligation; it does not
> discharge it, and none of the wording here is a legal determination.