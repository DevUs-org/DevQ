'''
Tags: Plugin

Qiskit lowering — the one place a Qiskit QuantumCircuit becomes a DevQ
CircuitRep.

This is the mirror image of the IBM provider's qiskit_lowering. That
module is the single place a CircuitRep is lowered INTO a Qiskit circuit
for execution; this one is the single place a Qiskit circuit is lowered
INTO a CircuitRep for DevQ to schedule, route and benchmark. Keeping each
direction to one walk is the same discipline for the same reason: a second
copy drifts, and a circuit that means one thing on the way in and another
on the way out is a silent fidelity bug.

WHY THIS EXISTS AS ITS OWN MODULE. Two callers want the SAME walk. The
QiskitFrontend's parse() reads a .py source that builds a QuantumCircuit
and lowers it; a future object/REST entry point (a live QuantumCircuit
handed in over an API, no file) will want the identical lowering with a
different way of OBTAINING the circuit. So the walk lives here as a pure
QuantumCircuit -> CircuitRep function, and each caller supplies the
circuit its own way.

IMPORT DISCIPLINE (see docs/EXTENDING.md). A plugin reaches into DevQ only
through plugin_bases: the contract from its base, every other core type
from plugin_bases.common. CircuitRep is the core type this lowering
constructs, so it comes from plugin_bases.common — never from circuits.*
directly. Third-party imports (qiskit) are unrestricted, and are kept
LAZY, inside the functions that touch qiskit types, so importing this
module costs nothing and needs no qiskit installed; only CALLING the
lowering does.

FLATTENED INDEX SPACE. CircuitRep addresses qubits and clbits by a single
flat global index, and records classical registers as
name -> (base_index, size). Qiskit gives exactly this via
QuantumCircuit.find_bit(bit).index — the global position of a bit across
all registers — so a bit's DevQ index is its Qiskit find_bit index, with
no remapping. Registers are recorded from qc.cregs using the base bit's
find_bit index.

GATE NAMES. Qiskit's instruction names are already lowercase and already
match DevQ's gate vocabulary (h, cx, rz, ccx, …), so a gate lowers by
carrying its name through verbatim with its qubit indices and float
params. Structural ops — measure, reset, barrier, if_else — are handled
explicitly, never emitted as gates: measure -> add_measure, reset ->
add_reset, barrier dropped (a transpiler hint with no execution semantics,
as the qasm2 frontend also treats it), if_else -> add_conditional per
guarded body gate. Anything else with no CircuitRep representation is
DECLINED by raising, never silently dropped — a dropped operation is a
silent correctness bug in a benchmark that measures fidelity.
'''

# Stdlib only at module scope. CircuitRep comes from the plugin seam, not
# core. qiskit stays LAZY — imported inside the functions that touch qiskit
# types — so importing this module costs nothing.
from plugin_bases.common import CircuitRep


class QiskitLoweringError(ValueError):
    '''
    Raised when a Qiskit circuit holds an operation this lowering cannot
    represent as a CircuitRep op.

    A silently dropped operation is dropped from a circuit nobody notices
    is wrong — the number it produces is plausible and invalid. Raising
    surfaces the gap: the frontend wraps this in a ValueError naming the
    source, and the job never enters the queue pretending to be a circuit
    it is not.

    Distinct from a circuit's unrunnable/capability status: a construct
    DevQ CAN represent but a given device cannot RUN (mid-circuit
    measurement, dynamic feedback) is left on the circuit for the kernel to
    route per device. This exception is for a construct DevQ cannot
    REPRESENT at all — there is no faithful CircuitRep to carry.
    '''


# Qiskit instruction names that are structural, not gates.
_MEASURE = "measure"
_RESET = "reset"
_BARRIER = "barrier"
_IF_ELSE = "if_else"


def _qubit_indices(qc, bits):
    '''Flat global indices of the given Qiskit qubits, via find_bit — the
    same space CircuitRep addresses, in the instruction's own qubit order
    (control before target for cx, etc.).'''
    return [qc.find_bit(b).index for b in bits]


def _params_as_floats(operation):
    '''
    A gate's parameters as plain floats, in order.

    A ParameterExpression with no free symbols is coerced with float(). An
    unbound Parameter cannot become a concrete CircuitRep gate arg, so this
    raises rather than inventing a value: a benchmark cannot run a circuit
    whose rotation angle is not yet a number.
    '''
    out = []
    for p in operation.params:
        try:
            out.append(float(p))
        except (TypeError, ValueError):
            raise QiskitLoweringError(
                f"gate '{operation.name}' has a non-numeric parameter "
                f"({p!r}); an unbound Parameter cannot be lowered. Bind "
                f"all parameters before handing the circuit to DevQ."
            ) from None
    return out


def _lower_conditional(circuit, qc, instruction):
    '''
    Lower one Qiskit if_else (classical feedback) into CircuitRep
    conditional ops.

    DevQ's conditional op is `if (bits == value) <single gate>` with the
    guard given as clbit indices LSB-first plus the integer value. Qiskit's
    IfElseOp carries a `condition` of either (ClassicalRegister, int) or
    (Clbit, int|bool), and a `blocks` tuple whose first block is the
    true-branch body.

    GUARD. A register condition (creg, N) becomes the flat indices of that
    register's bits, LSB-first, with value N. A single-clbit condition
    (clbit, v) becomes a one-element index list with value int(v).

    BODY. DevQ keeps `body` a SINGLE gate op, so each gate in the true
    block is emitted as its own conditional sharing the guard. A nested if,
    measure or reset inside the guarded block is beyond a single-gate body,
    so it is declined. An else branch is likewise unrepresentable and
    declined.
    '''
    from qiskit.circuit import ClassicalRegister

    op = instruction.operation

    if len(op.blocks) > 1 and op.blocks[1] is not None \
            and len(op.blocks[1].data) > 0:
        raise QiskitLoweringError(
            "if/else with a non-empty else branch is not representable as "
            "a CircuitRep conditional (DevQ models `if (c==N) gate` only). "
            "Rewrite without the else branch."
        )

    condition = op.condition
    if condition is None:
        raise QiskitLoweringError("if_else with no condition.")

    target, value = condition
    if isinstance(target, ClassicalRegister):
        clbits = [qc.find_bit(b).index for b in target]   # LSB-first
        cond_value = int(value)
    else:
        clbits = [qc.find_bit(target).index]
        cond_value = int(bool(value))

    body = op.blocks[0]
    for bi in body.data:
        bop = bi.operation
        name = bop.name
        if name in (_MEASURE, _RESET, _BARRIER, _IF_ELSE):
            raise QiskitLoweringError(
                f"the guarded block of an if contains a '{name}'; DevQ's "
                f"conditional body is a single gate, so measures, resets, "
                f"barriers and nested ifs inside a guarded block are not "
                f"supported."
            )
        gate_op = {
            "op": "gate",
            "gate": name,
            "qubits": _qubit_indices(qc, bi.qubits),
            "params": _params_as_floats(bop),
        }
        circuit.add_conditional(clbits, cond_value, gate_op)


def lower_circuit(qc):
    '''
    Lower a Qiskit QuantumCircuit into a DevQ CircuitRep.

    Walks qc.data in SOURCE ORDER, so measures and resets land where the
    circuit put them relative to the gates around them (a reset before vs
    after a 2q gate is a different circuit). Gates carry their Qiskit name
    and float params through verbatim; measure/reset/if_else lower to the
    matching CircuitRep op; barrier is dropped.

    Args:
        qc: a qiskit.circuit.QuantumCircuit with all parameters bound.

    Returns:
        CircuitRep — widths from the circuit, classical registers in the
        flat index space, and the ordered instruction stream.

    Raises:
        QiskitLoweringError: on an operation with no faithful CircuitRep
            representation. A caller wraps this with the source's name.

    MID-CIRCUIT MEASUREMENT. Measures are recorded in source position, so a
    circuit that operates on a qubit after measuring it yields a CircuitRep
    whose has_mid_circuit_measurement is true. That is left as-is: it is a
    well-formed circuit, and whether a device can run it is the kernel's
    per-device routing decision. This lowering represents faithfully and
    lets routing decide.
    '''
    circuit = CircuitRep(qc.num_qubits, qc.num_clbits)

    for creg in qc.cregs:
        base = qc.find_bit(creg[0]).index
        circuit.add_creg(creg.name, base, len(creg))

    for instruction in qc.data:
        op = instruction.operation
        name = op.name

        if name == _BARRIER:
            continue

        if name == _MEASURE:
            q = qc.find_bit(instruction.qubits[0]).index
            c = qc.find_bit(instruction.clbits[0]).index
            circuit.add_measure(q, c)
            continue

        if name == _RESET:
            q = qc.find_bit(instruction.qubits[0]).index
            circuit.add_reset(q)
            continue

        if name == _IF_ELSE:
            _lower_conditional(circuit, qc, instruction)
            continue

        # Everything else is a gate. A control-flow / classical-writing op
        # we do not handle (for_loop, while_loop, switch_case) would touch
        # clbits without being a measure; guard that explicitly rather than
        # emitting it as a bogus gate.
        if len(instruction.clbits) > 0:
            raise QiskitLoweringError(
                f"operation '{name}' touches classical bits but is not a "
                f"measure or a supported if; DevQ cannot represent it as a "
                f"gate. Supported classical constructs: measure, reset, "
                f"and `if (c==N) gate`."
            )

        circuit.add_gate(name, _qubit_indices(qc, instruction.qubits),
                         _params_as_floats(op))

    return circuit
