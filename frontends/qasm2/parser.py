'''
Tags: Main

OpenQASM 2.0 parser — source text to CircuitRep.

Ties the tokenizer and the expression evaluator together into a full
2.0 reader. What it does that the original whitespace-splitting reader
did not:

  - keeps gate PARAMETERS, evaluating each expression to a float
    (rx(pi/2) now carries 1.5708..., not a mangled gate name);
  - tolerates any spacing, since it works on tokens, not split lines;
  - flattens SEVERAL qreg/creg declarations into one global index space,
    so a circuit with `qreg a[2]; qreg b[3]` has 5 qubits and b[0] is
    global qubit 2;
  - inlines custom `gate` definitions RECURSIVELY, substituting both
    parameters and qubit arguments;
  - records measure and reset as first-class instructions, inline in
    CircuitRep's single ordered `instructions` stream in source position
    (so a `reset` keeps its place relative to the gates around it — see
    CircuitRep's module docstring);
  - parses `if (creg==N) <stmt>` and EMITS it as classical feedback: a
    first-class `conditional` op per guarded operation, resolving the
    register to its clbit indices. It is no longer rejected — whether a
    dynamic circuit can run is a per-device capability decided at routing
    time (a provider's supports_dynamic), not a parse-time verdict.

SCOPE IS 2.0, NOT 3.0. This is the 2.0 implementation of a
version-agnostic idea: a frontend lowers a source language to
CircuitRep. A 3.0 (or Silq, or Q#) frontend is a separate implementation
of the same BaseFrontend contract; it is not blocked by anything here.
'''

from circuits.circuit_rep import CircuitRep
from .tokenizer import tokenize, QASMError
from . import expression


class TokenCursor:
    '''
    A position in the token stream, with lookahead.

    Shared by the parser and the expression evaluator so the two never
    disagree about where parsing is. peek() does not consume; next()
    does; expect() consumes a specific token or raises.
    '''
    def __init__(self, tokens):
        self._tokens = tokens
        self._pos = 0

    def peek(self, ahead=0):
        idx = self._pos + ahead
        if idx < len(self._tokens):
            return self._tokens[idx]
        return self._tokens[-1]   # EOF, which is always last

    def next(self):
        tok = self._tokens[self._pos]
        if self._pos < len(self._tokens) - 1:
            self._pos += 1
        return tok

    def expect(self, kind, value=None):
        tok = self.peek()
        if tok.kind != kind or (value is not None and tok.value != value):
            want = value if value is not None else kind
            raise QASMError(f"expected {want!r}, got {tok.value!r}", tok.line)
        return self.next()

    def at_end(self):
        return self.peek().kind == "EOF"


# Gates whose definition is built in to qelib1.inc. Custom `gate`
# definitions add to this set. The values are (num_params, num_qubits),
# used only to validate arity at the call site — the providers hold the
# actual gate semantics, so this is a shape check, not an implementation.
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


class _GateDef:
    '''A parsed custom gate: its formal parameter and qubit names, and
    the token slice of its body, ready to be inlined at each call.'''
    __slots__ = ("params", "qubits", "body_tokens")

    def __init__(self, params, qubits, body_tokens):
        self.params = params          # list of formal parameter names
        self.qubits = qubits          # list of formal qubit names
        self.body_tokens = body_tokens


class _Parser:
    def __init__(self, tokens):
        self.cursor = TokenCursor(tokens)
        self.qregs = {}      # name -> (base_index, size)
        self.cregs = {}      # name -> (base_index, size)
        self.num_qubits = 0  # running total across all qregs
        self.num_clbits = 0  # running total across all cregs
        self.gate_defs = {}  # name -> _GateDef, custom gates
        self.circuit = None

    # ── Top level ────────────────────────────────────────────────────

    def parse(self):
        c = self.cursor

        self._parse_header()

        # A single forward pass: 2.0 requires registers and gates to be
        # declared before use. The CircuitRep is created lazily by
        # _ensure_circuit() the first time an operation is emitted, once
        # the register declarations that precede it have set num_qubits
        # and num_clbits. This keeps register resolution and emission in
        # one pass without a NoneType circuit during the walk.
        while not c.at_end():
            self._parse_statement()

        # A circuit with declarations but no operations still needs to
        # exist (e.g. a register-only file, or one that is all resets).
        self._ensure_circuit()

        # Record the declared classical registers on the circuit, in the
        # SAME flattened global index space the parser assigned them from
        # (self.cregs is name -> (base, size)). Done once here, after the
        # full pass, so the circuit carries the complete register set
        # regardless of declaration order — a `conditional` op resolves a
        # register name against exactly this mapping. Structure only; it
        # appends nothing to the instruction stream.
        for name, (base, size) in self.cregs.items():
            self.circuit.add_creg(name, base, size)

        return self.circuit

    def _ensure_circuit(self):
        '''
        Create the CircuitRep if it does not exist yet, sizing it from
        the registers declared so far. A gate that appeared before any
        qreg would already have failed in resolution, so by the time this
        runs the register widths are final for a valid circuit.
        '''
        if self.circuit is None:
            self.circuit = CircuitRep(self.num_qubits, self.num_clbits)

    def _parse_header(self):
        c = self.cursor
        # OPENQASM 2.0 ;
        if c.peek().kind == "ID" and c.peek().value == "OPENQASM":
            c.next()
            ver = c.peek()
            if ver.kind != "NUMBER":
                raise QASMError("expected a version after OPENQASM", ver.line)
            if not ver.value.startswith("2"):
                raise QASMError(
                    f"this frontend reads OpenQASM 2.0, not {ver.value}. "
                    f"A 3.0 frontend is a separate implementation of the "
                    f"same contract.", ver.line)
            c.next()
            c.expect("SYMBOL", ";")
        # An absent header is tolerated: many QASMBench fragments omit it.

    # ── Statements ───────────────────────────────────────────────────

    def _parse_statement(self):
        c = self.cursor
        tok = c.peek()

        if tok.kind != "ID":
            raise QASMError(f"unexpected {tok.value!r} at statement start",
                            tok.line)

        kw = tok.value

        if kw == "include":
            self._parse_include()
        elif kw == "qreg":
            self._parse_reg("qreg")
        elif kw == "creg":
            self._parse_reg("creg")
        elif kw == "gate":
            self._parse_gate_def()
        elif kw == "opaque":
            # An opaque gate has no body; DevQ cannot execute it. Reject
            # rather than silently accept a gate with no meaning.
            raise QASMError(
                "opaque gates have no definition to lower and cannot be "
                "executed by DevQ", tok.line)
        elif kw == "measure":
            self._parse_measure()
        elif kw == "reset":
            self._parse_reset()
        elif kw == "barrier":
            self._parse_barrier()
        elif kw == "if":
            self._parse_if()
        else:
            # Anything else beginning with an identifier is a gate call.
            self._parse_gate_call()

    def _parse_include(self):
        c = self.cursor
        c.next()  # 'include'
        target = c.expect("STRING").value
        c.expect("SYMBOL", ";")
        # Only qelib1.inc is meaningful, and its gates are built in, so
        # the include is a no-op. A different include is accepted but
        # unused — the gates it would define are simply unknown if called.

    def _parse_reg(self, kind):
        c = self.cursor
        c.next()  # 'qreg' or 'creg'
        name = c.expect("ID").value
        c.expect("SYMBOL", "[")
        size_tok = c.expect("NUMBER")
        size = int(float(size_tok.value))
        c.expect("SYMBOL", "]")
        c.expect("SYMBOL", ";")

        if size <= 0:
            raise QASMError(f"register {name!r} must have positive size",
                            size_tok.line)

        if kind == "qreg":
            if name in self.qregs:
                raise QASMError(f"qreg {name!r} already declared", size_tok.line)
            self.qregs[name] = (self.num_qubits, size)
            self.num_qubits += size
        else:
            if name in self.cregs:
                raise QASMError(f"creg {name!r} already declared", size_tok.line)
            self.cregs[name] = (self.num_clbits, size)
            self.num_clbits += size

    # ── Register references ──────────────────────────────────────────

    def _resolve_qubit(self, name, index, line):
        '''A qreg element name[index] to its global qubit index.'''
        if name not in self.qregs:
            raise QASMError(f"unknown qreg {name!r}", line)
        base, size = self.qregs[name]
        if not 0 <= index < size:
            raise QASMError(
                f"qubit index {index} out of range for qreg {name!r}[{size}]",
                line)
        return base + index

    def _resolve_clbit(self, name, index, line):
        if name not in self.cregs:
            raise QASMError(f"unknown creg {name!r}", line)
        base, size = self.cregs[name]
        if not 0 <= index < size:
            raise QASMError(
                f"bit index {index} out of range for creg {name!r}[{size}]",
                line)
        return base + index

    def _parse_qubit_arg(self):
        '''
        Parse one qubit argument: name[index] or a bare register name.

        Returns a list of global qubit indices — a single element for
        name[index], or the whole register for a bare name (2.0 broadcast
        form). The gate-call code decides whether a broadcast is valid.
        '''
        c = self.cursor
        name = c.expect("ID").value
        if c.peek().kind == "SYMBOL" and c.peek().value == "[":
            c.next()
            idx_tok = c.expect("NUMBER")
            index = int(float(idx_tok.value))
            c.expect("SYMBOL", "]")
            return [self._resolve_qubit(name, index, idx_tok.line)]
        # Bare register name: the whole register.
        if name not in self.qregs:
            raise QASMError(f"unknown qreg {name!r}", c.peek().line)
        base, size = self.qregs[name]
        return [base + k for k in range(size)]

    def _parse_creg_arg(self):
        '''Parse a classical target: name[index] or a bare creg name.'''
        c = self.cursor
        name = c.expect("ID").value
        if c.peek().kind == "SYMBOL" and c.peek().value == "[":
            c.next()
            idx_tok = c.expect("NUMBER")
            index = int(float(idx_tok.value))
            c.expect("SYMBOL", "]")
            return [self._resolve_clbit(name, index, idx_tok.line)]
        if name not in self.cregs:
            raise QASMError(f"unknown creg {name!r}", c.peek().line)
        base, size = self.cregs[name]
        return [base + k for k in range(size)]

    # ── Parameter lists ──────────────────────────────────────────────

    def _parse_param_list(self, params_binding):
        '''
        Parse an optional (expr, expr, ...) parameter list, returning a
        list of floats. `params_binding` supplies values for any formal
        parameter names in scope (empty at top level).
        '''
        c = self.cursor
        if not (c.peek().kind == "SYMBOL" and c.peek().value == "("):
            return []
        c.next()  # '('
        values = []
        if c.peek().kind == "SYMBOL" and c.peek().value == ")":
            c.next()
            return values
        while True:
            values.append(expression.evaluate(c, params_binding))
            if c.peek().kind == "SYMBOL" and c.peek().value == ",":
                c.next()
                continue
            break
        c.expect("SYMBOL", ")")
        return values

    # ── Gate definitions and calls ───────────────────────────────────

    def _parse_gate_def(self):
        c = self.cursor
        c.next()  # 'gate'
        name = c.expect("ID").value

        # Formal parameters: optional ( p, q, ... )
        formal_params = []
        if c.peek().kind == "SYMBOL" and c.peek().value == "(":
            c.next()
            if not (c.peek().kind == "SYMBOL" and c.peek().value == ")"):
                while True:
                    formal_params.append(c.expect("ID").value)
                    if c.peek().kind == "SYMBOL" and c.peek().value == ",":
                        c.next()
                        continue
                    break
            c.expect("SYMBOL", ")")

        # Formal qubits: at least one, comma-separated identifiers.
        formal_qubits = []
        while True:
            formal_qubits.append(c.expect("ID").value)
            if c.peek().kind == "SYMBOL" and c.peek().value == ",":
                c.next()
                continue
            break

        # Body: { ... } captured as a raw token slice for later inlining.
        c.expect("SYMBOL", "{")
        body = []
        depth = 1
        while depth > 0:
            tok = c.peek()
            if tok.kind == "EOF":
                raise QASMError(f"unterminated body for gate {name!r}", tok.line)
            if tok.kind == "SYMBOL" and tok.value == "{":
                depth += 1
            elif tok.kind == "SYMBOL" and tok.value == "}":
                depth -= 1
                if depth == 0:
                    c.next()  # consume closing brace
                    break
            body.append(c.next())
        # Terminate the captured body with EOF so a sub-cursor over it
        # has a proper stop token.
        from .tokenizer import Token
        body.append(Token("EOF", None, body[-1].line if body else 0))

        if name in self.gate_defs or name in _BUILTIN_GATES:
            raise QASMError(f"gate {name!r} is already defined", c.peek().line)

        self.gate_defs[name] = _GateDef(formal_params, formal_qubits, body)

    def _parse_gate_call(self, param_binding=None, qubit_binding=None):
        '''
        Parse and emit one gate application.

        param_binding / qubit_binding are set only when inlining a custom
        gate body: they map the body's formal names to the caller's
        actual values. At top level both are None.
        '''
        c = self.cursor
        name_tok = c.expect("ID")
        name = name_tok.value

        binding = param_binding or {}
        params = self._parse_param_list(binding)

        # Qubit arguments.
        qubit_args = []
        while True:
            if qubit_binding is not None:
                # Inside a gate body, qubit arguments are formal names.
                qname = c.expect("ID").value
                if qname not in qubit_binding:
                    raise QASMError(
                        f"gate body refers to unknown qubit {qname!r}",
                        c.peek().line)
                qubit_args.append([qubit_binding[qname]])
            else:
                qubit_args.append(self._parse_qubit_arg())
            if c.peek().kind == "SYMBOL" and c.peek().value == ",":
                c.next()
                continue
            break
        c.expect("SYMBOL", ";")

        self._emit_gate(name, params, qubit_args, name_tok.line)

    def _emit_gate(self, name, params, qubit_args, line):
        '''
        Emit a gate — inlining if it is custom, appending if it is a
        primitive. qubit_args is a list of qubit-index lists (each entry
        is one argument, which may broadcast over a register).
        '''
        self._ensure_circuit()
        # 2.0 broadcast: if any argument is a whole register, the gate
        # applies element-wise across them. Single-qubit args stay fixed.
        widths = [len(a) for a in qubit_args]
        broadcast = max(widths) if widths else 1
        for w in widths:
            if w != 1 and w != broadcast:
                raise QASMError(
                    f"register sizes disagree in call to {name!r}", line)

        for slot in range(broadcast):
            resolved = [a[slot] if len(a) > 1 else a[0] for a in qubit_args]

            if name in self.gate_defs:
                self._inline_custom(name, params, resolved, line)
            else:
                # A primitive. Validate arity against the builtin table
                # when known; unknown gates are passed through (a provider
                # may support a gate this table does not list).
                if name in _BUILTIN_GATES:
                    np_, nq_ = _BUILTIN_GATES[name]
                    if len(params) != np_ or len(resolved) != nq_:
                        raise QASMError(
                            f"gate {name!r} takes {np_} param(s) and {nq_} "
                            f"qubit(s), got {len(params)} and {len(resolved)}",
                            line)
                self.circuit.add_gate(name, resolved, params)

    def _inline_custom(self, name, params, resolved_qubits, line, depth=0):
        '''
        Recursively inline a custom gate: bind its formal parameters and
        qubits to actual values, then parse its body against those
        bindings, emitting the primitives it expands to.
        '''
        if depth > 100:
            raise QASMError(
                f"custom gate {name!r} nests more than 100 deep — likely "
                f"a recursive definition, which 2.0 does not allow", line)

        gate = self.gate_defs[name]
        if len(params) != len(gate.params):
            raise QASMError(
                f"gate {name!r} expects {len(gate.params)} parameter(s), "
                f"got {len(params)}", line)
        if len(resolved_qubits) != len(gate.qubits):
            raise QASMError(
                f"gate {name!r} expects {len(gate.qubits)} qubit(s), "
                f"got {len(resolved_qubits)}", line)

        param_binding = dict(zip(gate.params, params))
        qubit_binding = dict(zip(gate.qubits, resolved_qubits))

        # Parse the body with a fresh cursor over its captured tokens.
        # Swap the active cursor so the shared helpers operate on the body.
        saved = self.cursor
        self.cursor = TokenCursor(gate.body_tokens)
        try:
            while not self.cursor.at_end():
                inner = self.cursor.peek()
                if inner.kind != "ID":
                    raise QASMError(
                        f"unexpected {inner.value!r} in body of gate {name!r}",
                        inner.line)
                # A gate body contains only gate calls (and barrier).
                if inner.value == "barrier":
                    self._parse_barrier(qubit_binding)
                else:
                    self._parse_body_call(param_binding, qubit_binding, depth)
        finally:
            self.cursor = saved

    def _parse_body_call(self, param_binding, qubit_binding, depth):
        '''One gate call inside a custom-gate body, resolved against the
        caller's bindings, expanding nested customs recursively.'''
        c = self.cursor
        name_tok = c.expect("ID")
        name = name_tok.value
        params = self._parse_param_list(param_binding)

        qubit_args = []
        while True:
            qname = c.expect("ID").value
            if qname not in qubit_binding:
                raise QASMError(
                    f"gate body refers to unknown qubit {qname!r}",
                    name_tok.line)
            qubit_args.append(qubit_binding[qname])
            if c.peek().kind == "SYMBOL" and c.peek().value == ",":
                c.next()
                continue
            break
        c.expect("SYMBOL", ";")

        if name in self.gate_defs:
            self._inline_custom(name, params, qubit_args, name_tok.line,
                                depth + 1)
        else:
            if name in _BUILTIN_GATES:
                np_, nq_ = _BUILTIN_GATES[name]
                if len(params) != np_ or len(qubit_args) != nq_:
                    raise QASMError(
                        f"gate {name!r} takes {np_} param(s) and {nq_} "
                        f"qubit(s), got {len(params)} and {len(qubit_args)}",
                        name_tok.line)
            self.circuit.add_gate(name, qubit_args, params)

    # ── Measure, reset, barrier ──────────────────────────────────────

    def _parse_measure(self):
        c = self.cursor
        c.next()  # 'measure'
        qubits = self._parse_qubit_arg()
        c.expect("SYMBOL", "->")
        clbits = self._parse_creg_arg()
        c.expect("SYMBOL", ";")

        self._ensure_circuit()
        # measure q -> c broadcasts element-wise over equal-width regs.
        if len(qubits) != len(clbits):
            raise QASMError(
                f"measure maps {len(qubits)} qubit(s) onto {len(clbits)} "
                f"bit(s) — sizes must match", c.peek().line)
        for q, cl in zip(qubits, clbits):
            self.circuit.add_measure(q, cl)

    def _parse_reset(self):
        c = self.cursor
        c.next()  # 'reset'
        qubits = self._parse_qubit_arg()
        c.expect("SYMBOL", ";")
        self._ensure_circuit()
        for q in qubits:
            self.circuit.add_reset(q)

    def _parse_barrier(self, qubit_binding=None):
        '''barrier is a scheduling hint with no effect on the lowered
        circuit; parse its arguments and discard.'''
        c = self.cursor
        c.next()  # 'barrier'
        # A barrier may name qubits or be bare (whole circuit). Consume
        # up to the semicolon either way.
        while not (c.peek().kind == "SYMBOL" and c.peek().value == ";"):
            if c.at_end():
                raise QASMError("unterminated barrier", c.peek().line)
            c.next()
        c.expect("SYMBOL", ";")

    def _parse_if(self):
        '''
        Parse `if (creg == N) <statement>` and emit it as classical
        feedback — a first-class construct, not a rejected one.

        A conditional guards a statement on the value of a classical
        register. DevQ represents it as one `conditional` op per guarded
        operation (see below), carried in source order in the ordered
        stream. Whether it can actually RUN is decided per-device at
        routing time by the provider's supports_dynamic — a dynamic
        circuit routes to a device whose provider honours feedback and is
        rejected only where none does. So the frontend's job is to
        REPRESENT it faithfully; it neither rejects nor raises.

        We do NOT raise, and no longer mark the circuit unrunnable: the
        circuit is well-formed OpenQASM and now has a faithful
        representation. The condition is fully parsed (register name and
        value), the register resolved to its clbit indices in the global
        flattened space, and the guarded statement parsed normally — then
        every op that statement appended is re-wrapped in a conditional
        sharing this one condition.

        WHY WRAP EACH APPENDED OP, NOT ONE. A guarded statement is not
        always a single primitive: 2.0 register broadcast (`if(c==1) h q;`
        over a whole qreg) and custom-gate inlining both expand one call
        into several ops. Each must be individually conditioned on the
        same value — which is exactly the documented rule that a block of
        guarded statements is several conditional ops sharing a condition.
        Capturing the ops the statement produced (from a marked stream
        position to the end) and wrapping each keeps that correct without
        threading return values through every _parse_* method.
        '''
        c = self.cursor
        line = c.peek().line
        c.next()  # 'if'
        c.expect("SYMBOL", "(")
        creg = c.expect("ID").value
        c.expect("SYMBOL", "==")
        value = int(float(c.expect("NUMBER").value))
        c.expect("SYMBOL", ")")

        # Resolve the register name to its clbit indices, LSB-first, in the
        # global flattened space. An unknown register is a genuine parse
        # error (the condition names something that was never declared),
        # so raise rather than mark — consistent with measure/reset, which
        # also raise on an undeclared creg.
        if creg not in self.cregs:
            raise QASMError(f"unknown creg {creg!r} in if-condition", line)
        base, size = self.cregs[creg]
        cond_clbits = list(range(base, base + size))

        # A negative or too-large value can never be equalled by a
        # `size`-bit register; the condition is well-formed but dead. That
        # is not a parse error (the source is legal), so accept it — the
        # op is emitted and simply never fires, which faithfully preserves
        # the source's meaning. (Left as-is deliberately; a linting pass
        # could warn, but the frontend does not editorialise.)

        self._ensure_circuit()
        mark = len(self.circuit.instructions)

        # Parse the guarded statement; it appends its op(s) normally.
        self._parse_statement()

        # Re-wrap each op the statement produced as a conditional sharing
        # this condition, preserving their source order.
        produced = self.circuit.instructions[mark:]
        del self.circuit.instructions[mark:]
        for op in produced:
            self.circuit.add_conditional(cond_clbits, value, op)


def parse(source_text, source_name="<qasm>"):
    '''
    Parse OpenQASM 2.0 source text into a CircuitRep.

    Args:
        source_text: the QASM source as a string.
        source_name: a label used only in error messages.

    Returns:
        CircuitRep — all operations in one ordered `instructions` stream,
        num_qubits/num_clbits from the declarations, and the declared
        classical registers recorded on the circuit. A well-formed
        `if (creg==N)` is emitted as first-class `conditional` ops (the
        circuit is `is_dynamic`); its runnability is decided per-device at
        routing time, not here. Only mid-circuit measurement — a construct
        no current backend can faithfully execute — still sets
        `unrunnable_reason` for the kernel to reject; parsing still
        succeeds.

    Raises:
        QASMError: only on genuinely MALFORMED or unparseable source (bad
                   syntax, undeclared register — including a creg named in
                   an if-condition that was never declared — opaque gate
                   with no body). An unsupported-but-well-formed construct
                   does NOT raise — mid-circuit measurement is marked
                   unrunnable instead, so it becomes a REJECTED job rather
                   than aborting submission.
    '''
    tokens = tokenize(source_text)
    circuit = _Parser(tokens).parse()
    if circuit.num_qubits == 0:
        raise QASMError("no qreg declared — nothing to run")

    # Mid-circuit measurement (a qubit operated on after being measured) is
    # NO LONGER marked unrunnable here. It is a per-device capability, just
    # like classical feedback: a provider whose measurement is non-terminal
    # (both IBM providers) runs it, one whose model is terminal-only
    # (devq.simulated) declines, and the kernel routes the job to a capable
    # device or REJECTs it per-device when none is attached (see
    # MemoryManager.unsatisfiable_reason and
    # CircuitRep.has_mid_circuit_measurement). The 2.0 frontend now marks NO
    # circuit unrunnable: both former cases (classical control and
    # mid-circuit measurement) became per-device capability questions. The
    # unrunnable_reason field remains on CircuitRep for any future
    # construct that truly no backend can run, and the kernel still honours
    # it, but the frontend sets it for none.

    return circuit