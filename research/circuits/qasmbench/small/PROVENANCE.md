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

**Vendored (42): the complete upstream `small/` suite.** Every circuit is
vendored — there are no exclusions. A circuit is included whether DevQ can
execute it or must reject it, because a REJECTED result is a valid,
informative outcome that belongs in the results, not a reason to hide the
circuit. The 42 split into three outcome classes:

**33 run** — DevQ executes them and reports a fidelity number:
`adder_n4`, `adder_n10`, `basis_change_n3`, `basis_trotter_n4`, `bell_n4`,
`cat_state_n4`, `deutsch_n2`, `dnn_n2`, `dnn_n8`, `error_correctiond3_n5`,
`fredkin_n3`, `grover_n2`, `hhl_n7`, `hs4_n4`, `ising_n10`, `iswap_n2`,
`linearsolver_n3`, `lpn_n5`, `pea_n5`, `qaoa_n3`, `qaoa_n6`, `qec_en_n5`,
`qft_n4`, `qpe_n9`, `qrng_n4`, `quantumwalks_n2`, `sat_n7`, `simon_n6`,
`teleportation_n3`, `toffoli_n3`, `variational_n4`, `vqe_n4`, `wstate_n3`.

**5 rejected — well-formed but unsupported.** They parse into valid
circuits using constructs DevQ's execution model does not support, so the
frontend marks them unrunnable and the kernel rejects the job (REJECTED)
with a precise reason:
- **Classical control (4):** `inverseqft_n4`, `ipea_n2`, `qec_sm_n5`,
  `shor_n5` use `if (creg==N)`, which needs mid-circuit measurement
  feedback the model does not provide.
- **Mid-circuit measurement (1):** `bb84_n8` measures qubits and then
  operates on them again; DevQ measures terminally, so it is detected and
  rejected rather than silently mis-run.

**4 rejected — unparseable.** `vqe_uccsd_n4`, `vqe_uccsd_n6`,
`vqe_uccsd_n8`, `hhl_n10` measure a register (`q`) the file never declares.
This is a defect in the upstream QASMBench encoding, confirmed by Qiskit's
own OpenQASM 2.0 parser rejecting them with the identical error (`'q' is
not defined in this scope`). These are standard, valuable algorithms
(VQE-UCCSD ansätze, HHL) — only this particular serialisation is invalid.
Rather than drop them, the benchmark runner turns a parse failure into a
REJECTED job whose reason is the parse error, so they appear as rejected
rows with a clear cause.

## Two rejection reasons, distinguished

The results show two flavours of REJECTED, so the cause is legible at a
glance:
- *"...requires mid-circuit measurement feedback..."* / *"mid-circuit
  measurement: qubit N ..."* — a well-formed circuit DevQ's execution model
  cannot run.
- *"could not parse circuit: ...unknown qreg 'q'..."* — a circuit whose
  source is not valid OpenQASM 2.0.

A note on the runner's failure policy: a parse failure becomes a REJECTED
job (a property of the circuit), but a genuine SPEC-authoring error — a
missing circuit file, or an unknown frontend for the extension — still
aborts the run loudly, because that is the user's spec being wrong, not a
circuit being unrunnable.

## Gate coverage

Every gate used by the vendored circuits is in the IBM lowering's
vocabulary (`providers/ibm/qiskit_lowering.py` `_GATE_TABLE`), which is a
superset of the qasm2 frontend's `_BUILTIN_GATES`. A test block asserts
that superset relation, so a QASMBench circuit cannot silently lose a gate.