'''
Tags: Main

CircuitRep — DevQ's hardware-independent internal circuit format.

An ORDERED list of operations, each tagged by kind, in source order.
Frontends produce it; allocators, schedulers, and providers consume it.

    {"op": "gate",    "gate": name, "qubits": [...], "params": [...]}
    {"op": "measure", "qubit": q, "clbit": c}
    {"op": "reset",   "qubit": q}
    {"op": "conditional", "condition": {"clbits": [...], "value": N},
                          "body": {<a single gate op>}}

ONE ORDERED LIST, NOT SEPARATE CHANNELS. An earlier design kept measure
and reset in side channels so gate consumers never saw them. That was
the right isolation while frontends recorded measurement without anyone
executing it — but it discards ORDER, and order is exactly what
execution needs: `reset q[1]` means something different before versus
after a two-qubit gate on q1. Once providers honour measure and reset,
the flat gate-list-plus-channels shape can no longer express the circuit
faithfully, so operations now live together in source order.

The cost of one list is that consumers who want only gates must filter
by op. get_depth() does; both providers do. That filter is the price of
faithful ordering, paid once at each gate iterator.

DYNAMIC CIRCUITS. A `conditional` op is a gate guarded by a classical
condition — `if (creg==N) <gate>` — the classical-feedback construct
mid-circuit measurement is the primitive for. It is a FIRST-CLASS op,
carried in source order like any other, with `condition` naming the
classical bits and the value they must equal (LSB-first over `clbits`)
and `body` the single gate op that fires when they do. A circuit that
uses one is `is_dynamic`. Whether such a circuit can actually RUN is not
a property of the circuit — it is a per-device capability question,
answered at routing time by the provider's `supports_dynamic` (see
providers/base_provider.py): the job routes to a device whose provider
supports feedback, and is REJECTED with a per-device reason only when no
attached device does. This is the shift from the earlier model, where
classical control was a circuit-global `unrunnable_reason`; a conditional
is now representable, and eligibility is decided device by device.

DERIVED VIEWS. `measurements` (list of (qubit, clbit) pairs), `resets`
(list of qubit indices) and `conditionals` (the conditional ops) are
read-only properties computed from the ordered list, so callers that
only want one kind keep a simple view while the ordered list stays the
single source of truth — the two cannot drift apart. `is_dynamic` is the
same kind of derived view, a boolean. `num_clbits` is stored, not
derived: it is the DECLARED classical-register width, which is not
recoverable from the measures that happened to fire (a creg may be wider
than its used bits). `cregs` (name -> (base_index, size)) is likewise
recorded by the frontend, not derived, so a condition can name a register
by the same flattened index space the frontend built.

UNRUNNABLE, BUT WELL-FORMED. `unrunnable_reason` marks a circuit DevQ
parsed successfully but NO device could ever faithfully execute —
mid-circuit measurement (a gate or reset on a qubit after it was
measured), a construct the lowering cannot represent on any current
backend. None for a runnable circuit. It is distinct from the per-device
dynamic-circuit verdict above: `unrunnable_reason` is circuit-global
("no device can run this"), while a dynamic circuit is runnable on a
capable device and declined only on others. A frontend SETS
`unrunnable_reason` rather than raising, so the circuit still becomes a
job and the kernel rejects that job (REJECTED, with this reason) at
routing time — one uniform "declined with a reason" outcome instead of a
parse exception that aborts submission.
'''

class CircuitRep:
    def __init__(self, num_qubits, num_clbits=0):
        self.num_qubits = num_qubits
        self.num_clbits = num_clbits
        # The one ordered, op-tagged operation stream. Everything a
        # circuit does — gates, measures, resets, conditionals — appends
        # here in the order the source expressed it.
        self.instructions = []

        # Declared classical registers: name -> (base_index, size), in the
        # SAME flattened global index space the frontend assigns clbits
        # from. Recorded (not derived) because a condition names a register
        # by name and the (base, size) mapping is how those names resolve
        # to the clbit indices a `conditional` op carries. Empty until a
        # frontend declares registers via add_creg(); a circuit built
        # directly with bare clbit indices simply carries none.
        self._cregs = {}

        # When set (to a human-readable string), this circuit is one DevQ
        # cannot FAITHFULLY execute — a construct the execution model does
        # not support (classical control, mid-circuit measurement). It is
        # NOT a parse failure: the circuit is well-formed and fully built,
        # so it is carried like any other and the kernel rejects the JOB
        # (state REJECTED, this string as the reason) at routing time,
        # before it reaches a device. This keeps every "DevQ declines to
        # run this" verdict expressed as one thing — a REJECTED job with a
        # reason — whether the reason was detected here at the circuit
        # layer or later by the router/allocator. Detecting is the
        # frontend's job; REJECTING is the kernel's. A frontend must never
        # raise for an unsupported-but-well-formed construct, because an
        # exception would abort submission instead of producing a job that
        # can be rejected and reported alongside the ones that ran.
        self.unrunnable_reason = None

    def add_gate(self, name, qubits, params = None):
        self.instructions.append({
            "op": "gate",
            "gate": name,
            "qubits": qubits,
            "params": params or []
        })

    def add_measure(self, qubit, clbit):
        '''
        Append a measurement of one qubit onto one classical bit, in
        source order relative to the gates around it.
        '''
        self.instructions.append({
            "op": "measure",
            "qubit": qubit,
            "clbit": clbit
        })

    def add_reset(self, qubit):
        '''
        Append a reset of one qubit to |0>, in source order. Its position
        in the stream is meaningful: a reset is only correct relative to
        the operations before and after it.
        '''
        self.instructions.append({
            "op": "reset",
            "qubit": qubit
        })

    def add_creg(self, name, base_index, size):
        '''
        Record a declared classical register in the flattened global clbit
        index space. Called by a frontend as it declares registers, so a
        later `conditional` can name one and resolve it to clbit indices.
        This records STRUCTURE only; it appends nothing to the instruction
        stream and does not change num_clbits (the frontend owns the
        running total). Re-declaring a name overwrites, matching the
        frontend's own "already declared" guard, which fires first.
        '''
        self._cregs[name] = (base_index, size)

    def add_conditional(self, condition_clbits, condition_value, gate_op):
        '''
        Append a classically-conditioned gate — `if (creg==N) <gate>`.

        condition_clbits : the clbit indices (flattened global space) whose
            joint value gates the body, LSB-first — clbit condition_clbits[0]
            is bit 0 of the compared value.
        condition_value  : the integer those bits must equal for the body
            to fire.
        gate_op          : the guarded operation, a single gate op dict of
            the same shape add_gate produces
            ({"op": "gate", "gate":..., "qubits":..., "params":...}).

        A block of several guarded statements sharing one condition is
        represented as several conditional ops with the same condition —
        keeping body a single op keeps the stream flat and every consumer's
        op-filter simple. The op is carried in source order like any other;
        a circuit that holds one is is_dynamic, and whether it can run is
        decided per-device at routing time (see the class docstring).
        '''
        self.instructions.append({
            "op": "conditional",
            "condition": {"clbits": list(condition_clbits),
                          "value": condition_value},
            "body": gate_op,
        })

    @property
    def measurements(self):
        '''(qubit, clbit) pairs for every measure, in order — a read-only
        view over the ordered stream, not a second stored copy.'''
        return [(i["qubit"], i["clbit"])
                for i in self.instructions if i["op"] == "measure"]

    @property
    def resets(self):
        '''Reset qubit indices, in order — a read-only view over the
        ordered stream.'''
        return [i["qubit"] for i in self.instructions if i["op"] == "reset"]

    @property
    def cregs(self):
        '''Declared classical registers as name -> (base_index, size), a
        read-only view over the recorded structure. Empty if no frontend
        declared any (a directly-built circuit using bare clbit indices).'''
        return dict(self._cregs)

    @property
    def conditionals(self):
        '''The conditional ops, in order — a read-only view over the
        ordered stream. Each is the full op dict
        ({"op": "conditional", "condition": {...}, "body": {...}}), so a
        consumer sees both the guard and the guarded gate without
        re-walking the stream.'''
        return [i for i in self.instructions if i["op"] == "conditional"]

    @property
    def is_dynamic(self):
        '''True if this circuit uses classical feedback — it holds at least
        one conditional op. This is what the kernel checks against a
        provider's supports_dynamic at routing time: a dynamic circuit is
        routed only to a device whose provider can execute feedback. A
        derived boolean over the ordered stream, so it cannot drift from
        the instructions it summarises.'''
        return any(i["op"] == "conditional" for i in self.instructions)

    @property
    def has_mid_circuit_measurement(self):
        '''True if any qubit is operated on AFTER being measured — a gate or
        reset on a qubit that was already measured, or a conditional whose
        body gate touches one. This is the boolean form of the structural
        condition find_mid_circuit_measurement detects; it exists so the
        kernel can check it against a provider's
        supports_mid_circuit_measurement at routing time, exactly as it
        checks is_dynamic against supports_dynamic.

        This is a SEPARATE capability from is_dynamic: a circuit can need
        mid-circuit measurement without any classical feedback (measure,
        reset, reuse — no conditional), and a feedback circuit need not
        reuse a measured qubit. So the two are tracked independently and a
        provider may support one without the other. A derived boolean over
        the ordered stream, so it cannot drift from the instructions it
        summarises.'''
        return self.find_mid_circuit_measurement() is not None

    def get_depth(self):
        # Depth is a property of the UNITARY gates only — measure and
        # reset are not gates and do not add circuit depth. Filter the
        # ordered stream for gate ops.
        #
        # Conditional ops are deliberately NOT counted: a guarded gate
        # (`if (creg==N) g`) may or may not fire, so its contribution to a
        # static depth figure is genuinely ambiguous, and no current
        # consumer needs it resolved (get_depth feeds cost scoring, e.g.
        # the QOS baseline, where a maybe-executed layer has no well-defined
        # weight). Skipping them is the honest default; revisit if a depth
        # consumer ever needs a defined convention for conditioned gates.
        gates = [i for i in self.instructions if i["op"] == "gate"]
        if not gates:
            return 0

        # Track the current depth level for each physical qubit
        qubit_depths = [0] * self.num_qubits

        for inst in gates:
            target_qubits = inst["qubits"]

            # Find the current max depth among qubits involved in this gate
            current_max = max(qubit_depths[q] for q in target_qubits)

            # Increment depth for all involved qubits
            for q in target_qubits:
                qubit_depths[q] = current_max + 1

        return max(qubit_depths)

    def find_mid_circuit_measurement(self):
        '''
        Return a reason string if any qubit is operated on AFTER it has
        been measured — mid-circuit measurement — else None.

        WHAT THIS MEANS NOW. Mid-circuit measurement is a per-device
        CAPABILITY, not a circuit-global rejection. A provider whose
        runtime keeps measurement non-terminal (both IBM providers: Aer and
        Heron run measure → reset/gate → reuse natively) can execute these
        circuits; the IBM lowering bakes measures inline in source order for
        them, so a later operation lands relative to the real mid-circuit
        measurement rather than a hoisted one. A provider whose model is
        terminal-measurement only (devq.simulated) declines, and the job
        routes to a capable device — or is REJECTED per-device when none is
        attached. This mirrors how classical feedback is handled.

        So this method is a DETECTOR the capability check builds on
        (has_mid_circuit_measurement), not a verdict of unrunnability. It
        returns a human-readable reason (naming the first offending qubit)
        so a per-device rejection can explain itself, and None when
        measurement is terminal throughout.

        (Historically this marked the circuit unrunnable, on the premise
        that the lowering hoists all measures to the end — true of the
        static path, but the inline path added for dynamic circuits does not
        hoist, which is what makes these circuits runnable on a capable
        provider.)

        This is a pure structural property of the ordered instruction
        stream, so it lives on CircuitRep rather than in one frontend:
        every frontend that lowers to CircuitRep gets the check, and a
        circuit built by any means is judged the same way. It does not set
        unrunnable_reason — detection and capability-routing are separate.

        Returns the reason for the FIRST offending qubit (source order),
        naming the qubit, or None if measurement is terminal throughout.
        '''
        measured = set()
        for inst in self.instructions:
            op = inst["op"]
            if op == "measure":
                measured.add(inst["qubit"])
            elif op == "gate":
                for q in inst["qubits"]:
                    if q in measured:
                        return (
                            f"mid-circuit measurement: qubit {q} is used by "
                            f"gate '{inst['gate']}' after being measured, "
                            f"which DevQ's execution model (terminal "
                            f"measurement only) cannot faithfully run")
            elif op == "conditional":
                # A conditional's guard READS a measured clbit — that is the
                # whole point of feedback and is legitimate. What is NOT
                # legitimate is the guarded gate touching a qubit that was
                # already measured: that is the same mid-circuit hazard as a
                # bare gate, just wrapped. Inspect the body gate's qubits by
                # the same rule. (The clbits the CONDITION reads are not
                # checked here — reading a measured bit is exactly what a
                # dynamic circuit is meant to do.)
                body = inst["body"]
                if body.get("op") == "gate":
                    for q in body["qubits"]:
                        if q in measured:
                            return (
                                f"mid-circuit measurement: qubit {q} is used "
                                f"by conditional gate '{body['gate']}' after "
                                f"being measured, which DevQ's execution model "
                                f"(terminal measurement only) cannot "
                                f"faithfully run")
            elif op == "reset":
                # A reset after measure is legitimate on real hardware and
                # runs correctly through the inline lowering (the measure is
                # baked in place, so the reset lands relative to the real
                # mid-circuit measurement). It is detected here as
                # mid-circuit measurement so the capability check routes it
                # to a provider that supports it, rather than to one whose
                # model is terminal-measurement only.
                if inst["qubit"] in measured:
                    return (
                        f"mid-circuit measurement: qubit {inst['qubit']} is "
                        f"reset after being measured, which DevQ's execution "
                        f"model (terminal measurement only) cannot faithfully "
                        f"run")
        return None