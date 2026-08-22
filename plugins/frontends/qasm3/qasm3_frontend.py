'''
Tags: Plugin

QASM3Frontend — DevQ frontend for OpenQASM 3.0, via the official parser.

An opt-in frontend (registered by hand). It parses OpenQASM 3.0 with the
OFFICIAL openqasm3 reference parser (the `openqasm3[parser]` package, an
ANTLR grammar maintained alongside the spec) and walks the resulting AST
into a CircuitRep with its OWN walk. Register it when you want it:

    from plugins.frontends.qasm3.qasm3_frontend import QASM3Frontend
    devq.register_frontend("qasm3", QASM3Frontend())

INDEPENDENT OF QISKIT. Unlike the Qiskit frontend, this path never touches
qiskit. The reference parser yields a pure syntactic AST, and this module
lowers that AST directly. So the two frontends share NO lowering: the
Qiskit one walks a QuantumCircuit, this one walks an openqasm3 AST. That
keeps this frontend's notion of "valid OpenQASM 3" tied to the standard,
not to one vendor's loader.

EXTENSIONS AND DISPATCH. Claims ".qasm3" and also ".qasm". The built-in
qasm2 frontend also claims ".qasm"; DevQ resolves a ".qasm" file that two
frontends claim by requiring an explicit --frontend (per docs/EXTENDING.md).
A ".qasm3" file resolves here unambiguously.

SCOPE. Lowers the STANDARD gate set (stdgates.inc names, which match DevQ's
gate vocabulary), qubit/bit declarations, measure (single and
whole-register), reset, and single-level classical feedback
`if (creg == N) { ... }`. It relies on the reference parser for syntax; it
does NOT itself expand `include` files or inline user `gate` definitions.
A custom-gate definition or unsupported construct is DECLINED with a clear
message rather than silently mislowered. Extending to custom-gate inlining
is future work with a marked boundary here.

IMPORT DISCIPLINE (docs/EXTENDING.md). The contract base comes from
plugin_bases.base_frontend; CircuitRep — the core type this frontend
constructs — from plugin_bases.common; no core path is imported. openqasm3
is imported lazily inside parse(), so importing this module needs no
openqasm3 installed.
'''

import math

from plugin_bases.base_frontend import BaseFrontend
from plugin_bases.common import CircuitRep


# DevQ's gate vocabulary — the qelib1/stdgates names — as
# (num_params, num_qubits). A gate call in the AST is validated against
# this: a name not here is a custom or unsupported gate and is declined,
# not passed through blind. stdgates.inc names map 1:1 onto these.
_BUILTIN_GATES = {
    "id": (0, 1), "x": (0, 1), "y": (0, 1), "z": (0, 1),
    "h": (0, 1), "s": (0, 1), "sdg": (0, 1), "t": (0, 1), "tdg": (0, 1),
    "sx": (0, 1), "sxdg": (0, 1),
    "rx": (1, 1), "ry": (1, 1), "rz": (1, 1),
    "p": (1, 1), "u1": (1, 1), "u2": (2, 1), "u3": (3, 1), "u": (3, 1),
    "cx": (0, 2), "cy": (0, 2), "cz": (0, 2), "ch": (0, 2),
    "swap": (0, 2), "ecr": (0, 2),
    "crx": (1, 2), "cry": (1, 2), "crz": (1, 2), "cp": (1, 2), "cu1": (1, 2),
    "ccx": (0, 3), "cswap": (0, 3),
}


class QASM3Error(ValueError):
    '''An OpenQASM 3 construct DevQ cannot represent, or a malformed
    reference — declined with a message. parse() wraps it with the source
    name, matching the qasm2 frontend's ValueError-with-provenance.'''


class QASM3Frontend(BaseFrontend):
    LABEL = "OpenQASM 3.0"

    # Claims .qasm3 and .qasm. DevQ disambiguates a .qasm file that both
    # this and the built-in qasm2 frontend claim (per-job --frontend); a
    # .qasm3 file is unambiguous.
    EXTENSIONS = (".qasm3", ".qasm")

    def parse(self, source):
        '''
        Lower an OpenQASM 3.0 source file into a CircuitRep.

        Parses with the official openqasm3 reference parser, then walks the
        AST. Raises ValueError naming the source on a read failure, a
        syntax error the reference parser reports, or a construct this
        frontend declines.
        '''
        try:
            with open(source) as handle:
                text = handle.read()
        except OSError:
            raise

        try:
            from openqasm3.parser import parse as oq3_parse
        except ImportError as e:
            raise ValueError(
                f"{source}: the OpenQASM 3 frontend needs the reference "
                f"parser. Install it with: pip install 'openqasm3[parser]' "
                f"({e})"
            ) from None

        try:
            program = oq3_parse(text)
        except Exception as e:
            # The reference parser raises its own error types on bad syntax;
            # normalise them to a ValueError naming the source.
            raise ValueError(f"{source}: {type(e).__name__}: {e}") from None

        try:
            return _Lowerer().lower(program)
        except QASM3Error as e:
            raise ValueError(f"{source}: {e}") from None


class _Lowerer:
    '''
    Walks an openqasm3 AST Program into a CircuitRep in one pass.

    Holds the register tables it builds as it goes — qubit and bit
    registers mapped to the flat global index space CircuitRep addresses,
    the (base, size) model — so an indexed reference `q[k]` resolves to a
    flat qubit index and a whole-register reference `q` expands to its bits
    in order.
    '''

    def __init__(self):
        self.qregs = {}       # name -> (base, size)
        self.cregs = {}       # name -> (base, size)
        self.num_qubits = 0
        self.num_clbits = 0
        self.circuit = None

    def lower(self, program):
        from openqasm3 import ast
        self.ast = ast

        # OpenQASM 3, like 2.0, requires declaration before use, so a single
        # ordered walk sees every register before the operation that
        # references it.
        for stmt in program.statements:
            self._statement(stmt)

        self._ensure_circuit()
        for name, (base, size) in self.cregs.items():
            self.circuit.add_creg(name, base, size)
        return self.circuit

    def _ensure_circuit(self):
        '''Create the CircuitRep once register widths are known — on the
        first emitted operation, or at end for a declaration-only file.
        Widths are final by the time any operation emits, since
        declarations precede use.'''
        if self.circuit is None:
            self.circuit = CircuitRep(self.num_qubits, self.num_clbits)

    # ── statements ──────────────────────────────────────────────────────

    def _statement(self, stmt):
        ast = self.ast
        if isinstance(stmt, ast.Include):
            # `include "stdgates.inc"` is not expanded — the standard gate
            # names are handled by _BUILTIN_GATES directly.
            return
        if isinstance(stmt, ast.QubitDeclaration):
            self._declare_qubits(stmt)
            return
        if isinstance(stmt, ast.ClassicalDeclaration):
            self._declare_classical(stmt)
            return
        if isinstance(stmt, ast.QuantumGate):
            self._gate(stmt)
            return
        if isinstance(stmt, ast.QuantumReset):
            self._reset(stmt)
            return
        if isinstance(stmt, ast.QuantumMeasurementStatement):
            self._measure(stmt)
            return
        if isinstance(stmt, ast.BranchingStatement):
            self._branch(stmt)
            return
        if isinstance(stmt, ast.QuantumGateDefinition):
            raise QASM3Error(
                f"custom gate definition '{stmt.name.name}' is not "
                f"supported; DevQ's OpenQASM 3 frontend handles the "
                f"standard gate set (stdgates) only."
            )
        raise QASM3Error(
            f"unsupported statement {type(stmt).__name__}. Supported: qubit "
            f"/ bit declarations, standard gates, measure, reset, and "
            f"`if (creg == N) {{ ... }}`."
        )

    def _declare_qubits(self, stmt):
        name = stmt.qubit.name
        size = self._const_int(stmt.size) if stmt.size is not None else 1
        self.qregs[name] = (self.num_qubits, size)
        self.num_qubits += size

    def _declare_classical(self, stmt):
        ast = self.ast
        # Only bit / bit[n] declarations map to a classical register. A
        # non-bit classical variable (int, float, angle) is not a creg and
        # is not referenced by a gate/measure in the supported subset, so
        # it is declined rather than silently ignored.
        if not isinstance(stmt.type, ast.BitType):
            raise QASM3Error(
                f"classical declaration of type "
                f"{type(stmt.type).__name__} is not supported; only "
                f"`bit` / `bit[n]` classical registers are handled."
            )
        name = stmt.identifier.name
        size_node = stmt.type.size
        size = self._const_int(size_node) if size_node is not None else 1
        self.cregs[name] = (self.num_clbits, size)
        self.num_clbits += size

    def _gate(self, stmt):
        self._ensure_circuit()
        name = stmt.name.name.lower()
        if name not in _BUILTIN_GATES:
            raise QASM3Error(
                f"gate '{name}' is not a standard gate DevQ knows. "
                f"Supported: {', '.join(sorted(_BUILTIN_GATES))}."
            )
        if stmt.modifiers:
            raise QASM3Error(
                f"gate modifiers (ctrl/inv/pow) on '{name}' are not "
                f"supported."
            )
        n_params, n_qubits = _BUILTIN_GATES[name]
        params = [self._eval_angle(a) for a in stmt.arguments]
        qubits = []
        for qref in stmt.qubits:
            qubits.extend(self._qubit_operands(qref))

        if len(params) != n_params:
            raise QASM3Error(
                f"gate '{name}' takes {n_params} parameter(s), got "
                f"{len(params)}."
            )
        if len(qubits) != n_qubits:
            raise QASM3Error(
                f"gate '{name}' takes {n_qubits} qubit(s), got "
                f"{len(qubits)}. (Register-broadcast gate application is "
                f"not supported; index each qubit.)"
            )
        self.circuit.add_gate(name, qubits, params)

    def _reset(self, stmt):
        self._ensure_circuit()
        for q in self._qubit_operands(stmt.qubits):
            self.circuit.add_reset(q)

    def _measure(self, stmt):
        self._ensure_circuit()
        qubits = self._qubit_operands(stmt.measure.qubit)
        if stmt.target is None:
            raise QASM3Error(
                "measure without an assignment target is not supported; "
                "write `c[i] = measure q[i];`."
            )
        clbits = self._clbit_operands(stmt.target)
        if len(qubits) != len(clbits):
            raise QASM3Error(
                f"measure maps {len(qubits)} qubit(s) onto {len(clbits)} "
                f"classical bit(s); the widths must match."
            )
        for q, c in zip(qubits, clbits):
            self.circuit.add_measure(q, c)

    def _branch(self, stmt):
        '''
        Lower `if (creg == N) { body }` into conditional ops — one per body
        gate, sharing the guard (DevQ keeps `body` a single gate). The
        condition must be `<creg> == <int>`; the guarded body may hold only
        plain standard gates (no nested measure/reset/if). An else branch
        is unrepresentable and declined.
        '''
        ast = self.ast
        self._ensure_circuit()

        if stmt.else_block:
            raise QASM3Error(
                "`else` is not representable as a CircuitRep conditional; "
                "DevQ models `if (creg == N) gate` only."
            )

        cond = stmt.condition
        if not isinstance(cond, ast.BinaryExpression) or cond.op.name != "==":
            raise QASM3Error(
                "only an equality condition `creg == N` is supported in "
                "`if`."
            )
        if not isinstance(cond.lhs, ast.Identifier):
            raise QASM3Error(
                "the `if` condition must compare a whole classical register "
                "to an integer, e.g. `if (c == 2)`."
            )
        reg_name = cond.lhs.name
        if reg_name not in self.cregs:
            raise QASM3Error(f"unknown classical register '{reg_name}' in "
                             f"`if` condition.")
        base, size = self.cregs[reg_name]
        clbits = list(range(base, base + size))       # LSB-first
        value = self._const_int(cond.rhs)

        for body_stmt in stmt.if_block:
            if not isinstance(body_stmt, ast.QuantumGate):
                raise QASM3Error(
                    f"the body of an `if` may contain only standard gates; "
                    f"found {type(body_stmt).__name__}. Measures, resets and "
                    f"nested ifs inside a guarded block are not supported."
                )
            name = body_stmt.name.name.lower()
            if name not in _BUILTIN_GATES:
                raise QASM3Error(
                    f"gate '{name}' in an `if` body is not a standard gate.")
            if body_stmt.modifiers:
                raise QASM3Error(
                    f"gate modifiers on '{name}' in an `if` body are not "
                    f"supported.")
            n_params, n_qubits = _BUILTIN_GATES[name]
            params = [self._eval_angle(a) for a in body_stmt.arguments]
            qubits = []
            for qref in body_stmt.qubits:
                qubits.extend(self._qubit_operands(qref))
            if len(params) != n_params or len(qubits) != n_qubits:
                raise QASM3Error(
                    f"gate '{name}' in `if` body has wrong arity.")
            gate_op = {"op": "gate", "gate": name,
                       "qubits": qubits, "params": params}
            self.circuit.add_conditional(clbits, value, gate_op)

    # ── operand resolution ──────────────────────────────────────────────

    def _qubit_operands(self, ref):
        '''Flat qubit indices a qubit reference denotes: `q[k]` -> [base+k],
        a whole register `q` -> all its bits in order.'''
        return self._operands(ref, self.qregs, "qubit")

    def _clbit_operands(self, ref):
        '''Flat clbit indices a classical reference denotes, same rules over
        the classical register table.'''
        return self._operands(ref, self.cregs, "classical")

    def _operands(self, ref, table, kind):
        ast = self.ast
        if isinstance(ref, ast.Identifier):
            name = ref.name
            if name not in table:
                raise QASM3Error(f"unknown {kind} register '{name}'.")
            base, size = table[name]
            return list(range(base, base + size))
        if isinstance(ref, ast.IndexedIdentifier):
            name = ref.name.name
            if name not in table:
                raise QASM3Error(f"unknown {kind} register '{name}'.")
            base, size = table[name]
            index_list = ref.indices[0]
            if not isinstance(index_list, list) or len(index_list) != 1:
                raise QASM3Error(
                    f"only single-element indexing of a 1-D {kind} register "
                    f"is supported.")
            k = self._const_int(index_list[0])
            if not (0 <= k < size):
                raise QASM3Error(
                    f"{kind} index {name}[{k}] out of range 0..{size - 1}.")
            return [base + k]
        raise QASM3Error(
            f"unsupported {kind} reference {type(ref).__name__}.")

    # ── constant / angle evaluation ─────────────────────────────────────

    def _const_int(self, node):
        '''An integer constant from an AST node — a register size, an index,
        or a condition value. Literal integers are accepted; a folded
        constant expression that evaluates to a whole number is accepted;
        anything else is declined.'''
        ast = self.ast
        if isinstance(node, ast.IntegerLiteral):
            return int(node.value)
        val = self._eval_angle(node)
        if abs(val - round(val)) < 1e-12:
            return int(round(val))
        raise QASM3Error(
            f"expected an integer constant, got {type(node).__name__}.")

    def _eval_angle(self, node):
        '''
        Evaluate a gate-argument expression to a float: literals,
        `pi`/`tau`/`euler`, unary +/-, and +-*/ and power over them. A gate
        angle like `pi/2` or `-pi/4` must reach the CircuitRep as a number;
        a symbolic identifier that is not a known constant is declined —
        DevQ gates carry concrete float params.
        '''
        ast = self.ast
        if isinstance(node, (ast.FloatLiteral, ast.IntegerLiteral)):
            return float(node.value)
        if isinstance(node, ast.Identifier):
            name = node.name.lower()
            consts = {"pi": math.pi, "\u03c0": math.pi,
                      "tau": math.tau, "euler": math.e}
            if name in consts:
                return consts[name]
            raise QASM3Error(
                f"unknown constant '{node.name}' in a gate argument; only "
                f"pi, tau and euler are known.")
        if isinstance(node, ast.UnaryExpression):
            v = self._eval_angle(node.expression)
            if node.op.name == "-":
                return -v
            if node.op.name == "+":
                return v
            raise QASM3Error(f"unsupported unary operator {node.op.name}.")
        if isinstance(node, ast.BinaryExpression):
            l = self._eval_angle(node.lhs)
            r = self._eval_angle(node.rhs)
            op = node.op.name
            if op == "+":
                return l + r
            if op == "-":
                return l - r
            if op == "*":
                return l * r
            if op == "/":
                return l / r
            if op in ("**", "^"):
                return l ** r
            raise QASM3Error(f"unsupported operator {op} in a gate "
                             f"argument.")
        raise QASM3Error(
            f"unsupported expression {type(node).__name__} in a gate "
            f"argument.")
