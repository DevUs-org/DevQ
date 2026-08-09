'''
Tags: Main

engine.gates — the native engine's gate vocabulary.

This is the LOCKED matrix set the statevector core will apply. Every matrix
here was verified identical to Qiskit's `Operator` for the corresponding
gate (constants exactly; parameterised gates at several angles), rather than
asserted from memory — the same discipline the IBM lowering follows, and for
the same reason: a wrong matrix is a silently-wrong distribution, and a
fidelity number computed against a wrong ideal is high, plausible, and
meaningless.

WHAT THIS FILE IS, AND IS NOT. It defines the unitary of each gate as a
SMALL matrix on its own qubits — 2x2 for a one-qubit gate, 4x4 for `ecr`
(the only intrinsically-two-qubit unitary; every other multi-qubit gate is a
CONTROLLED or PERMUTATION structure the state core builds by bit-indexing,
not a stored 2^n matrix). It does NOT apply gates to a state — that is
statevector.py. Keeping the vocabulary separate means the matrices can be
locked and tested before a single line applies them.

CONVENTION. All matrices are little-endian to match Qiskit and the rest of
DevQ: for a controlled or multi-qubit application, basis-state index bit k is
qubit k (qubit 0 is the least-significant bit). The state core honours this
when it embeds a gate; this file only needs it for `ecr`, whose 4x4 is given
in that basis (control/first qubit = bit 0).

ALIASES. Four names in the qasm2 vocabulary are exact rewrites of a surviving
gate, so they share a builder rather than duplicating a matrix — verified by
Operator equality, not assumed:

    u1(lambda)          == p(lambda)                  (diag(1, e^{i lambda}))
    cu1(lambda)         == cp(lambda)                 (same, controlled)
    u2(phi, lambda)     == u(pi/2, phi, lambda)       (parameter packing)
    u3(theta, phi, lam) == u(theta, phi, lam)         (identical)

VOCABULARY PARITY. This module's gate names are asserted equal to the qasm2
frontend's _BUILTIN_GATES (see `engine_gates` test block). The two tables are
written for different reasons — the frontend's to qelib1's spec, this one to
what the engine can simulate — and nothing else couples them, so a gate added
to one and not the other would otherwise be invisible until a circuit used it.
'''

import math
import cmath
import numpy as np

_PI = math.pi
_SQRT1_2 = 1.0 / math.sqrt(2.0)


# ── one-qubit constant matrices ───────────────────────────────────────────
# Each is the exact 2x2 unitary, verified == Operator(QuantumCircuit).

I2 = np.array([[1, 0], [0, 1]], dtype=complex)
X  = np.array([[0, 1], [1, 0]], dtype=complex)
Y  = np.array([[0, -1j], [1j, 0]], dtype=complex)
Z  = np.array([[1, 0], [0, -1]], dtype=complex)
H  = np.array([[1, 1], [1, -1]], dtype=complex) * _SQRT1_2
S  = np.array([[1, 0], [0, 1j]], dtype=complex)
SDG = np.array([[1, 0], [0, -1j]], dtype=complex)
T   = np.array([[1, 0], [0, cmath.exp(1j * _PI / 4)]], dtype=complex)
TDG = np.array([[1, 0], [0, cmath.exp(-1j * _PI / 4)]], dtype=complex)
# sqrt(X) and its dagger.
SX   = 0.5 * np.array([[1 + 1j, 1 - 1j], [1 - 1j, 1 + 1j]], dtype=complex)
SXDG = 0.5 * np.array([[1 - 1j, 1 + 1j], [1 + 1j, 1 - 1j]], dtype=complex)


# ── one-qubit parameterised builders ──────────────────────────────────────
# Angles in radians. Each returns a fresh 2x2; none mutates a shared array.

def rx(theta):
    c, s = math.cos(theta / 2), math.sin(theta / 2)
    return np.array([[c, -1j * s], [-1j * s, c]], dtype=complex)


def ry(theta):
    c, s = math.cos(theta / 2), math.sin(theta / 2)
    return np.array([[c, -s], [s, c]], dtype=complex)


def rz(theta):
    return np.array([[cmath.exp(-1j * theta / 2), 0],
                     [0, cmath.exp(1j * theta / 2)]], dtype=complex)


def p(lam):
    # phase gate: diag(1, e^{i lambda}). u1 is the same matrix.
    return np.array([[1, 0], [0, cmath.exp(1j * lam)]], dtype=complex)


def u(theta, phi, lam):
    # The general one-qubit gate. u3 == u; u2(phi, lam) == u(pi/2, phi, lam).
    ct, st = math.cos(theta / 2), math.sin(theta / 2)
    return np.array([
        [ct, -cmath.exp(1j * lam) * st],
        [cmath.exp(1j * phi) * st, cmath.exp(1j * (phi + lam)) * ct],
    ], dtype=complex)


def u2(phi, lam):
    return u(_PI / 2, phi, lam)


def u3(theta, phi, lam):
    return u(theta, phi, lam)


# ── the one intrinsically-two-qubit unitary: ECR ──────────────────────────
# IBM's native echoed cross-resonance gate. qelib1 does not define it; IBM
# hardware has it as the native two-qubit gate. Given as a 4x4 in the
# little-endian basis (index = 2*q1 + q0), verified == Operator(ecr).
ECR = _SQRT1_2 * np.array([
    [0,    1,    0,   1j],
    [1,    0,  -1j,    0],
    [0,   1j,    0,    1],
    [-1j,  0,    1,    0],
], dtype=complex)


# ── the vocabulary registry ───────────────────────────────────────────────
# name -> GateSpec. `kind` tells the state core HOW to embed the gate:
#   "u1"   — a single-qubit unitary; `matrix` is a 2x2 (constant) or the
#            builder returns one from the gate's params.
#   "ctrl" — a controlled-U on 2 qubits (control = qubit[0], target =
#            qubit[1]); `matrix`/builder gives the 2x2 U applied on the
#            target when the control is set.
#   "swap" — the 2-qubit SWAP permutation (no matrix needed).
#   "ecr"  — the 4x4 ECR, applied on qubits[0], qubits[1] directly.
#   "ccx"  — Toffoli: controls = qubits[0], qubits[1]; target = qubits[2].
#   "cswap"— Fredkin: control = qubits[0]; swapped = qubits[1], qubits[2].
# A `builder` is a callable(params) -> 2x2 for parameterised gates; `matrix`
# is a fixed array for constant gates. Exactly one of the two is set for the
# unitary/controlled kinds; permutation kinds (swap, ecr, ccx, cswap) carry
# neither a builder nor need a 2x2 (ecr carries its own 4x4 as `matrix`).

class GateSpec:
    __slots__ = ("name", "num_params", "num_qubits", "kind",
                 "matrix", "builder")

    def __init__(self, name, num_params, num_qubits, kind,
                 matrix=None, builder=None):
        self.name = name
        self.num_params = num_params
        self.num_qubits = num_qubits
        self.kind = kind
        self.matrix = matrix
        self.builder = builder

    def unitary(self, params):
        '''
        The 2x2 (or 4x4 for ecr) unitary for this gate given its params.
        For controlled/unitary kinds this is the acted-on 2x2; for ecr it is
        the 4x4; for pure-permutation kinds (swap, ccx, cswap) it is None —
        the state core permutes basis amplitudes directly.
        '''
        if self.builder is not None:
            if len(params) != self.num_params:
                raise ValueError(
                    f"gate '{self.name}' takes {self.num_params} param(s), "
                    f"got {len(params)}")
            return self.builder(*params)
        return self.matrix


def _spec(name, np_, nq, kind, matrix=None, builder=None):
    return name, GateSpec(name, np_, nq, kind, matrix, builder)


GATES = dict([
    # one qubit, no parameters
    _spec('id',   0, 1, 'u1', matrix=I2),
    _spec('x',    0, 1, 'u1', matrix=X),
    _spec('y',    0, 1, 'u1', matrix=Y),
    _spec('z',    0, 1, 'u1', matrix=Z),
    _spec('h',    0, 1, 'u1', matrix=H),
    _spec('s',    0, 1, 'u1', matrix=S),
    _spec('sdg',  0, 1, 'u1', matrix=SDG),
    _spec('t',    0, 1, 'u1', matrix=T),
    _spec('tdg',  0, 1, 'u1', matrix=TDG),
    _spec('sx',   0, 1, 'u1', matrix=SX),
    _spec('sxdg', 0, 1, 'u1', matrix=SXDG),

    # one qubit, parameterised
    _spec('rx', 1, 1, 'u1', builder=rx),
    _spec('ry', 1, 1, 'u1', builder=ry),
    _spec('rz', 1, 1, 'u1', builder=rz),
    _spec('p',  1, 1, 'u1', builder=p),
    _spec('u1', 1, 1, 'u1', builder=p),     # alias: u1(l) == p(l)
    _spec('u2', 2, 1, 'u1', builder=u2),    # alias: u2 == u(pi/2, .)
    _spec('u3', 3, 1, 'u1', builder=u3),    # alias: u3 == u
    _spec('u',  3, 1, 'u1', builder=u),

    # two qubits, no parameters — controlled-U (control q0, target q1)
    _spec('cx', 0, 2, 'ctrl', matrix=X),
    _spec('cy', 0, 2, 'ctrl', matrix=Y),
    _spec('cz', 0, 2, 'ctrl', matrix=Z),
    _spec('ch', 0, 2, 'ctrl', matrix=H),
    _spec('swap', 0, 2, 'swap'),
    _spec('ecr',  0, 2, 'ecr', matrix=ECR),

    # two qubits, parameterised — controlled-U
    _spec('crx', 1, 2, 'ctrl', builder=rx),
    _spec('cry', 1, 2, 'ctrl', builder=ry),
    _spec('crz', 1, 2, 'ctrl', builder=rz),
    _spec('cp',  1, 2, 'ctrl', builder=p),
    _spec('cu1', 1, 2, 'ctrl', builder=p),  # alias: cu1(l) == cp(l)

    # three qubits — permutations
    _spec('ccx',   0, 3, 'ccx'),
    _spec('cswap', 0, 3, 'cswap'),
])


class UnknownGateError(ValueError):
    '''
    Raised when a circuit names a gate the native engine has no matrix for.

    The engine declines rather than skipping, exactly as the IBM lowering
    does: a dropped gate silently simulates a DIFFERENT circuit, and a
    fidelity number against that ideal is plausible and wrong. The caller
    (the reference path) catches this, falls back to a registered
    reference-capable provider if one is attached, and otherwise records no
    ideal — fidelity None, an honest undefined, never a forged value.
    '''


def gate_spec(name):
    '''
    Look up a gate by (lower-cased) name, or raise UnknownGateError naming
    the known vocabulary. The caller lower-cases; matching is exact.
    '''
    spec = GATES.get(name)
    if spec is None:
        raise UnknownGateError(
            f"gate '{name}' is not in the native engine's vocabulary. "
            f"Known gates: {', '.join(sorted(GATES))}.")
    return spec


def vocabulary():
    '''The set of gate names the native engine can simulate.'''
    return set(GATES)