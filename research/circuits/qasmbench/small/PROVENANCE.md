# QASMBench — vendored circuits

These `.qasm` files are copied verbatim from the **QASMBench** benchmark
suite. They are third-party circuits, vendored so DevQ's fidelity
validation runs against published NISQ benchmarks rather than only its own
Bell/GHZ fixtures. See `docs/REFERENCES.md` `[QASMBench]` for the citation.

## Source

- Upstream: https://github.com/pnnl/QASMBench
- Commit: `357b942396d5c2b7cbc1c229c585a6ef5ccaebac`
- Category: `small/` (≤ ~16 qubits)
- Paper: Ang Li, Samuel Stein, Sriram Krishnamoorthy, James Ang.
  "QASMBench: A Low-Level Quantum Benchmark Suite for NISQ Evaluation and
  Simulation." ACM Transactions on Quantum Computing, 2022.
  DOI: 10.1145/3550488.

## License

QASMBench is **BSD-licensed**. The upstream `LICENSE` and `NOTICE` travel
with these files (this directory) as the license requires. Do not separate
them from the circuits.

## What was taken, and what was not

Each circuit was taken as the upstream `small/<name>/<name>.qasm` — the
plain source, **not** the `_transpiled.qasm` variant. The transpiled files
are pre-mapped to a fixed basis and coupling map, which is exactly the
placement decision DevQ exists to make; running them would bypass the
system under test. The per-circuit READMEs and PNGs were left upstream.

**Vendored (31):** the `small/` set minus the exclusions below.

**Excluded — classical control (4).** `inverseqft_n4`, `ipea_n2`,
`qec_sm_n5`, `shor_n5` use `if (creg==N)`, which DevQ rejects by design:
it needs mid-circuit measurement feedback the execution model does not
provide. Including them would only produce REJECTED jobs.

**Excluded — measure-then-reuse (1).** `bb84_n8` measures qubits and then
operates on them further. DevQ's IBM lowering currently appends all
measures after the circuit body (only `reset` is placed at its source
position), so the post-measurement operations would act on an uncollapsed
state — a different circuit than written. Because `execute()` and
`reference_ideal()` share the lowering, both sides would be wrong
identically and fidelity would read spuriously high. Excluded until
source-position measurement lands. `bb84_n8` is the ONLY `small/` circuit
affected; it was checked against every candidate.

**Excluded — too large for a quick reference (6).** `hhl_n10`,
`vqe_uccsd_n8`, `vqe_uccsd_n6`, `dnn_n8`, `hhl_n7`, `ising_n10`. The
noiseless reference is an exact density-matrix simulation whose cost grows
with instruction count; these range from ~500 to ~187k instructions and
are deferred to keep the validation pass fast. They can be added later
without any code change — only reference runtime.

## Gate coverage

Every gate used by the vendored circuits is in the IBM lowering's
vocabulary (`providers/ibm/qiskit_lowering.py` `_GATE_TABLE`), which is a
superset of the qasm2 frontend's `_BUILTIN_GATES`. A test block asserts
that superset relation, so a QASMBench circuit cannot silently lose a gate.
