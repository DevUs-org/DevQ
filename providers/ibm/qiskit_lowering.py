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

# Stdlib only at module scope. qiskit stays LAZY (imported inside
# build_qiskit_circuit) so importing this module costs nothing; math is
# needed for u2's fixed π/2 and carries no such cost.
import math


# ── the gate map ────────────────────────────────────────────────────────
#
# CircuitRep gate name -> a call on a Qiskit QuantumCircuit. Genuinely at
# module scope: built once at import, not per instruction. Each entry takes
# (qc, qubits, params) as arguments rather than closing over them, which is
# what lets the table live out here — the previous version rebuilt a dict of
# closures on EVERY gate, a cost a 1500-instruction circuit pays 1500 times.
#
# SCOPE OF THIS TABLE. It covers every name in the qasm2 frontend's
# _BUILTIN_GATES (the qelib1.inc vocabulary) plus `ecr`, which IBM hardware
# has as a native two-qubit gate and qelib1 does not define. A test block
# asserts that superset relation, because the two tables were written for
# different reasons — the frontend's to qelib1's spec, this one inherited
# from a phase whose whole circuit corpus was Bell and GHZ — and nothing
# else couples them. Every .qasm fixture in the repo uses six gate names
# between them, so a gap here is invisible to the suite unless something
# asserts the coupling directly.
#
# FOUR SUBSTITUTIONS. u1/u2/u3/cu1 have no QuantumCircuit methods — they
# were removed in Qiskit 1.0 — so they lower to the surviving general
# forms. Verified identical by Operator comparison rather than assumed:
#
#     u1(λ)      -> p(λ)                (both are diag(1, e^{iλ}))
#     u2(φ, λ)   -> u(π/2, φ, λ)
#     u3(θ, φ, λ)-> u(θ, φ, λ)
#     cu1(λ)     -> cp(λ)
#
# These are exact rewrites, not approximations, so nothing about a fidelity
# comparison follows from the substitution.

_PI = math.pi

_GATE_TABLE = {
    # ── one qubit, no parameters ──
    'id':    lambda qc, q, p: qc.id(q[0]),
    'x':     lambda qc, q, p: qc.x(q[0]),
    'y':     lambda qc, q, p: qc.y(q[0]),
    'z':     lambda qc, q, p: qc.z(q[0]),
    'h':     lambda qc, q, p: qc.h(q[0]),
    's':     lambda qc, q, p: qc.s(q[0]),
    'sdg':   lambda qc, q, p: qc.sdg(q[0]),
    't':     lambda qc, q, p: qc.t(q[0]),
    'tdg':   lambda qc, q, p: qc.tdg(q[0]),
    'sx':    lambda qc, q, p: qc.sx(q[0]),
    'sxdg':  lambda qc, q, p: qc.sxdg(q[0]),

    # ── one qubit, parameterised ──
    'rx':    lambda qc, q, p: qc.rx(p[0], q[0]),
    'ry':    lambda qc, q, p: qc.ry(p[0], q[0]),
    'rz':    lambda qc, q, p: qc.rz(p[0], q[0]),
    'p':     lambda qc, q, p: qc.p(p[0], q[0]),
    'u1':    lambda qc, q, p: qc.p(p[0], q[0]),
    'u2':    lambda qc, q, p: qc.u(_PI / 2, p[0], p[1], q[0]),
    'u3':    lambda qc, q, p: qc.u(p[0], p[1], p[2], q[0]),
    'u':     lambda qc, q, p: qc.u(p[0], p[1], p[2], q[0]),

    # ── two qubits, no parameters ──
    'cx':    lambda qc, q, p: qc.cx(q[0], q[1]),
    'cy':    lambda qc, q, p: qc.cy(q[0], q[1]),
    'cz':    lambda qc, q, p: qc.cz(q[0], q[1]),
    'ch':    lambda qc, q, p: qc.ch(q[0], q[1]),
    'swap':  lambda qc, q, p: qc.swap(q[0], q[1]),
    'ecr':   lambda qc, q, p: qc.ecr(q[0], q[1]),

    # ── two qubits, parameterised ──
    'crx':   lambda qc, q, p: qc.crx(p[0], q[0], q[1]),
    'cry':   lambda qc, q, p: qc.cry(p[0], q[0], q[1]),
    'crz':   lambda qc, q, p: qc.crz(p[0], q[0], q[1]),
    'cp':    lambda qc, q, p: qc.cp(p[0], q[0], q[1]),
    'cu1':   lambda qc, q, p: qc.cp(p[0], q[0], q[1]),

    # ── three qubits ──
    'ccx':   lambda qc, q, p: qc.ccx(q[0], q[1], q[2]),
    'cswap': lambda qc, q, p: qc.cswap(q[0], q[1], q[2]),
}


class UnknownGateError(ValueError):
    '''
    Raised when a CircuitRep names a gate this lowering cannot express.

    WHY THIS DECLINES INSTEAD OF SKIPPING. This used to warn on stdout and
    drop the gate, so a circuit ran minus that gate rather than crashing a
    benchmark. That trade is wrong for anything measuring FIDELITY.
    execute() and reference_ideal() share this lowering deliberately, so a
    dropped gate is dropped on BOTH sides: the measured distribution gets
    compared against the ideal of the same truncated circuit, the two
    agree closely, and Hellinger fidelity comes out HIGH for a circuit
    nobody ran. A silent skip does not degrade the number, it invalidates
    it while leaving it plausible — and a suite whose fixtures all stay
    inside the known vocabulary stays green throughout.

    Raising gives each caller the failure it can actually express.
    execute() already wraps its run in try/except and returns
    ExecutionResult(success=False), so the job lands in FAILED naming the
    gate. reference_ideal() catches this and returns None, which is
    precisely the case benchmark/reference.compute_ideals() already
    documents — "cannot simulate it — a gate it does not know ... an
    absent ideal is not a zero distribution" — so fidelity reports None
    instead of a forged value. That contract was written in 5.4 and could
    never fire while the lowering skipped.

    This is a NORMAL condition, not exotic input. The qasm2 frontend
    validates arity against its builtin table but passes ANY gate name
    through to the CircuitRep by design, "a provider may support a gate
    this table does not list". Declining precisely is the contract.
    '''


def _apply_gate(qc, gate, qubits, params):
    '''
    Apply one CircuitRep gate to a Qiskit circuit. Names are matched
    lower-cased by the caller.

    This is the single gate vocabulary the IBM provider understands;
    execute() and reference_ideal() share it, so a gate added here is
    available to both at once and cannot be known to one but not the
    other.

    Raises:
        UnknownGateError : the name is not in _GATE_TABLE. See that class
            for why this declines rather than skipping.
    '''
    action = _GATE_TABLE.get(gate)
    if action is None:
        raise UnknownGateError(
            f"gate '{gate}' is not in the IBM lowering's vocabulary, so this "
            f"circuit cannot be lowered to Qiskit. Known gates: "
            f"{', '.join(sorted(_GATE_TABLE))}."
        )
    action(qc, qubits, params)


def _build_condition(qc, clbits, value):
    '''
    Build a Qiskit classical-expression testing that the given clbits equal
    `value`, LSB-first — clbits[0] is bit 0 of the value. Used to lower a
    CircuitRep `conditional` op's guard into an `if_test` predicate.

    WHY PER-BIT, NOT A REGISTER COMPARE. Qiskit's `if_test((creg, N))`
    tests a whole ClassicalRegister, but a DevQ condition names specific
    clbit indices in the flattened global classical space, which need not
    be a whole Qiskit register (the lowered circuit has one anonymous
    width-bit register). Conjoining a per-bit equality over exactly the
    named bits is correct for ANY subset and any value, and reduces to a
    single Clbit test in the common one-bit `if (c==0/1)` case — so it
    covers 2.0's `if (creg==N)` uniformly without depending on register
    boundaries that the flattening does not preserve.
    '''
    from qiskit.circuit.classical import expr

    terms = []
    for pos, cb in enumerate(clbits):
        lit = expr.lift(qc.clbits[cb])
        # Bit `pos` of value must be set (lit) or clear (not lit).
        terms.append(lit if (value >> pos) & 1 else expr.logic_not(lit))

    cond = terms[0]
    for t in terms[1:]:
        cond = expr.logic_and(cond, t)
    return cond


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

    DYNAMIC CIRCUITS. A circuit with classical feedback (is_dynamic — it
    holds `conditional` ops) is lowered differently in one respect: the
    measures that a condition READS must be baked into the body inline, at
    their source position, because an `if_test` can only test a classical
    bit that has already been written during the run. So for a dynamic
    circuit this walk applies `measure` ops in place (rather than deferring
    them all to the returned map) and emits each `conditional` as a Qiskit
    `if_test` block guarding its body gate. The returned measure_map still
    lists the (qubit, clbit) pairs for the caller's terminal sampling, but
    the mid-circuit measures are already in the body — so execute() must
    NOT re-apply the ones already baked. This is only reachable via
    execute(): reference_ideal() declines dynamic circuits (their ideal is
    not defined through the noiseless density-matrix + marginalise path),
    so the measurement-free-body invariant it relies on still holds for
    every circuit it actually lowers.

    Raises:
        ImportError propagates if qiskit is not installed — callers guard
        it exactly as they guard their own qiskit imports.
    '''
    from qiskit import QuantumCircuit

    qc = QuantumCircuit(circuit.num_qubits, width)

    dynamic = circuit.is_dynamic

    # Walk the ordered stream. Gates and resets go onto the body in source
    # position. Measures: for a STATIC circuit they are collected via
    # resolve_measure_map and NOT applied here, keeping the body
    # measurement-free for the reference path. For a DYNAMIC circuit they
    # are applied inline, because a later conditional's guard must be able
    # to read them mid-run. Conditionals lower to if_test blocks (dynamic
    # circuits only — a static circuit never holds one).
    for inst in circuit.instructions:
        op = inst["op"]
        if op == "gate":
            _apply_gate(qc, inst["gate"].lower(),
                        inst["qubits"], inst.get("params", []))
        elif op == "reset":
            qc.reset(inst["qubit"])
        elif op == "measure":
            # Baked inline for dynamic circuits so conditions can read the
            # bit; deferred to the map for static circuits (see above).
            if dynamic:
                qc.measure(inst["qubit"], inst["clbit"])
        elif op == "conditional":
            # Guard the body gate on the classical condition. Only dynamic
            # circuits reach here; the body is a single gate op.
            cond = _build_condition(qc, inst["condition"]["clbits"],
                                    inst["condition"]["value"])
            body = inst["body"]
            with qc.if_test(cond):
                _apply_gate(qc, body["gate"].lower(),
                            body["qubits"], body.get("params", []))

    return qc, resolve_measure_map(circuit, width)