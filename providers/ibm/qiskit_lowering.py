'''
Tags: Provider

Qiskit lowering — the one place a CircuitRep becomes a Qiskit
QuantumCircuit.

WHY THIS EXISTS. Two callers inside the IBM provider need the SAME walk
of a CircuitRep's ordered instruction stream into Qiskit:

  - execute()          — builds the circuit, measures into the classical
                         register, and samples it on a NOISY Aer backend.
  - reference_ideal()  — builds the SAME gate/reset body, but reads EXACT
                         probabilities from a NOISELESS density-matrix
                         simulation and marginalises them itself.

If each wrote its own walk they would drift — a gate added to one, a
reset-ordering fix landing in the other, the Option-B width rule
re-derived slightly differently on each side. A fidelity comparison is
SILENT when it breaks (a measured `001` and an ideal `001` must mean the
same classical bits), so the measured circuit and the ideal circuit must
be lowered by identical code. That is the whole reason this module is
one function pair rather than two copies.

QISKIT IS AN OPT-IN DEPENDENCY. This module lives under providers/ibm/
— opt-in territory — and imports qiskit LAZILY, inside the function, the
same way the provider's execute() does. Importing this module costs
nothing; only CALLING build_qiskit_circuit() needs qiskit installed. DevQ
core never imports it, and never has to.

WHAT IS SHARED VS WHAT DIFFERS. The shared piece is the gate/reset walk
in source order plus the RESOLVED measurement map (the (qubit, clbit)
pairs, with the measure-all fallback already applied). Both callers need
that map to agree: execute() places `qc.measure(q, c)` from it, and the
reference marginalises full-qubit probabilities down to the classical
register using the very same pairs. What DIFFERS is what each does after:
execute() bakes the measures into the circuit and samples; the reference
leaves the body unmeasured, reads the statevector probabilities, and
marginalises. So this module returns the body and the map, and lets each
caller finish its own way — it does not itself call qc.measure().
'''


# ── the gate map ────────────────────────────────────────────────────────
#
# CircuitRep gate name -> a call on a Qiskit QuantumCircuit. Kept at module
# scope (not rebuilt per gate) and applied by name. An unknown gate is a
# warning and a skip, matching the provider's historical behaviour — a
# circuit using a gate the lowering does not know still runs, minus that
# gate, rather than crashing a whole benchmark.
#
# The lambdas close over (qc, qubits, params) passed at call time.

def _apply_gate(qc, gate, qubits, params):
    '''
    Apply one CircuitRep gate to a Qiskit circuit. Names are matched
    lower-cased by the caller. Unknown gate -> warn and skip.

    This is the single gate vocabulary the IBM provider understands;
    execute() and reference_ideal() share it, so a gate added here is
    available to both at once and cannot be known to one but not the
    other.
    '''
    table = {
        'h':    lambda: qc.h(qubits[0]),
        'x':    lambda: qc.x(qubits[0]),
        'y':    lambda: qc.y(qubits[0]),
        'z':    lambda: qc.z(qubits[0]),
        's':    lambda: qc.s(qubits[0]),
        't':    lambda: qc.t(qubits[0]),
        'sx':   lambda: qc.sx(qubits[0]),
        'cx':   lambda: qc.cx(qubits[0], qubits[1]),
        'cz':   lambda: qc.cz(qubits[0], qubits[1]),
        'ecr':  lambda: qc.ecr(qubits[0], qubits[1]),
        'swap': lambda: qc.swap(qubits[0], qubits[1]),
        'rz':   lambda: qc.rz(params[0], qubits[0]),
        'rx':   lambda: qc.rx(params[0], qubits[0]),
        'ry':   lambda: qc.ry(params[0], qubits[0]),
        'ccx':  lambda: qc.ccx(qubits[0], qubits[1], qubits[2]),
    }
    action = table.get(gate)
    if action:
        action()
    else:
        print(f"[qiskit_lowering] Warning: unknown gate '{gate}', skipping.")


def resolve_measure_map(circuit, width):
    '''
    The RESOLVED (qubit, clbit) measurement pairs for a circuit, with the
    measure-all fallback already applied — the single source of "what gets
    measured onto which classical bit".

    A circuit with explicit measures uses them verbatim. A circuit with
    NONE falls back to measuring every qubit onto the matching classical
    bit (`q -> c[q]`), the historical behaviour and what most tools do for
    an unmeasured circuit.

    Both sides of a fidelity comparison call this so the mapping cannot
    differ between the measured run and the ideal: execute() places
    qc.measure() from these pairs, and the reference marginalises exact
    probabilities using the same pairs. `width` is the Option-B classical
    width (BaseProvider._counts_width) — passed in rather than recomputed
    here so the width rule stays owned by BaseProvider and this module
    never re-derives it.

    Returns a list of (qubit, clbit) tuples.
    '''
    explicit = [(i["qubit"], i["clbit"])
                for i in circuit.instructions if i["op"] == "measure"]
    if explicit:
        return explicit
    # No explicit measures: measure each qubit onto its own classical bit,
    # bounded by the declared width (never wider than the classical
    # register). num_qubits <= width in the fallback case, since width
    # falls back to num_qubits when no creg is declared.
    return [(q, q) for q in range(min(circuit.num_qubits, width))]


def build_qiskit_circuit(circuit, width):
    '''
    Lower a CircuitRep into a Qiskit QuantumCircuit carrying only its
    GATE and RESET body — no measurements baked in — plus the resolved
    measurement map its callers need.

    The body walks circuit.instructions in SOURCE ORDER, so a reset lands
    where the circuit put it relative to the gates around it (a reset
    before versus after a two-qubit gate means different things) rather
    than lumped at the end. Measures are deliberately NOT applied here:
    execute() wants them baked in to sample the classical register, while
    the reference wants an unmeasured body to read exact probabilities and
    marginalise. Returning the body plus the map lets each finish its own
    way from identical lowering.

    Args:
        circuit : CircuitRep
        width   : int — Option-B classical width (from
                  BaseProvider._counts_width), used for the register size
                  and to bound the measure-all fallback.

    Returns:
        (qc, measure_map) where qc is a QuantumCircuit(num_qubits, width)
        holding gates and resets only, and measure_map is the resolved
        list of (qubit, clbit) pairs.

    Raises:
        ImportError propagates if qiskit is not installed — callers guard
        it exactly as they guard their own qiskit imports.
    '''
    from qiskit import QuantumCircuit

    qc = QuantumCircuit(circuit.num_qubits, width)

    # Walk the ordered stream. Gates and resets go onto the body in source
    # position; measures are collected via resolve_measure_map, not applied
    # here, so the body stays measurement-free for the reference path.
    for inst in circuit.instructions:
        op = inst["op"]
        if op == "gate":
            _apply_gate(qc, inst["gate"].lower(),
                        inst["qubits"], inst.get("params", []))
        elif op == "reset":
            qc.reset(inst["qubit"])
        # measure ops are intentionally skipped here; see resolve_measure_map

    return qc, resolve_measure_map(circuit, width)