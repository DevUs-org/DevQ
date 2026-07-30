'''
Tags: Main

CircuitRep — DevQ's hardware-independent internal circuit format.

An ORDERED list of operations, each tagged by kind, in source order.
Frontends produce it; allocators, schedulers, and providers consume it.

    {"op": "gate",    "gate": name, "qubits": [...], "params": [...]}
    {"op": "measure", "qubit": q, "clbit": c}
    {"op": "reset",   "qubit": q}

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

DERIVED VIEWS. `measurements` (list of (qubit, clbit) pairs) and
`resets` (list of qubit indices) are read-only properties computed from
the ordered list, so callers that only want "what was measured" keep a
simple view while the ordered list stays the single source of truth —
the two cannot drift apart. `num_clbits` is stored, not derived: it is
the DECLARED classical-register width, which is not recoverable from the
measures that happened to fire (a creg may be wider than its used bits).

UNRUNNABLE, BUT WELL-FORMED. `unrunnable_reason` marks a circuit DevQ
parsed successfully but cannot faithfully execute — classical control or
mid-circuit measurement, constructs the execution model does not support.
None for a runnable circuit. A frontend SETS it rather than raising, so
the circuit still becomes a job and the kernel rejects that job (REJECTED,
with this reason) at routing time — one uniform "declined with a reason"
outcome instead of a parse exception that aborts submission.
'''

class CircuitRep:
    def __init__(self, num_qubits, num_clbits=0):
        self.num_qubits = num_qubits
        self.num_clbits = num_clbits
        # The one ordered, op-tagged operation stream. Everything a
        # circuit does — gates, measures, resets — appends here in the
        # order the source expressed it.
        self.instructions = []

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

    def get_depth(self):
        # Depth is a property of the UNITARY gates only — measure and
        # reset are not gates and do not add circuit depth. Filter the
        # ordered stream for gate ops.
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

        WHY THIS IS UNRUNNABLE. DevQ's execution model treats measurement
        as terminal: every provider reads out at the end. A measure
        followed by a gate or reset on the SAME qubit means the later
        operation acts on the post-measurement (collapsed) state, which
        the model cannot represent — the IBM lowering hoists all measures
        to the end, so it would apply that later operation to an
        UNcollapsed qubit and silently execute a different circuit than
        the one written. Because the measured run and the noiseless
        reference share that lowering, both would be wrong identically and
        a fidelity comparison would report a high, plausible number for a
        circuit that was never actually run — the worst kind of failure,
        invisible in a green suite. Detecting it lets the kernel reject the
        job honestly instead.

        This is a pure structural property of the ordered instruction
        stream, so it lives on CircuitRep rather than in one frontend:
        every frontend that lowers to CircuitRep gets the check, and a
        circuit built by any means is judged the same way. It does not set
        unrunnable_reason itself — detection and marking are separate so a
        caller can decide (a frontend marks; a diagnostic tool might only
        report).

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
            elif op == "reset":
                # A reset after measure is legitimate on real hardware and
                # is arguably representable, but the current lowering still
                # hoists the measure, so the reset would land relative to
                # an unmeasured qubit. Treat it as mid-circuit for now:
                # honest reject over a silent wrong distribution.
                if inst["qubit"] in measured:
                    return (
                        f"mid-circuit measurement: qubit {inst['qubit']} is "
                        f"reset after being measured, which DevQ's execution "
                        f"model (terminal measurement only) cannot faithfully "
                        f"run")
        return None