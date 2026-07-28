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
'''

class CircuitRep:
    def __init__(self, num_qubits, num_clbits=0):
        self.num_qubits = num_qubits
        self.num_clbits = num_clbits
        # The one ordered, op-tagged operation stream. Everything a
        # circuit does — gates, measures, resets — appends here in the
        # order the source expressed it.
        self.instructions = []

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