'''
Tags: Main

CircuitRep — DevQ's hardware-independent internal circuit format.

A flat list of gate instructions with virtual qubit indices, gate
names, and parameters. Frontends produce it; allocators, schedulers,
and providers consume it. get_depth() computes circuit depth via
per-qubit depth tracking — used by the SDF and Packing schedulers
for depth-based ordering.

MEASURE AND RESET LIVE IN SEPARATE CHANNELS, not in the gate list.
`instructions` holds unitary gates only, so every existing consumer —
the allocators sizing on num_qubits, get_depth(), and both providers'
gate iteration — sees exactly what it saw before richer frontends
existed. measure and reset are captured in `measurements` and `resets`,
which nothing iterates yet. This is deliberate isolation: a full 2.0
frontend can record what a circuit measures without changing what any
provider executes today. Teaching the providers to honour these
channels (and to distribute over the measured bits rather than
auto-measuring everything) is a later, execution-path change; until
then the fields are inert data, and a fidelity number that later looks
wrong stays cleanly attributable to parser-vs-provider.

`num_clbits` is the declared classical-register width. Providers ignore
it for now; it exists so the measurement channel can name a real
classical-bit target (measure q[i] -> c[j]) rather than only which
qubits were measured — the maximal-information form, and what a
conditional (parsed but rejected by the 2.0 frontend) would read.
'''

class CircuitRep:
    def __init__(self, num_qubits, num_clbits=0):
        self.num_qubits = num_qubits
        self.num_clbits = num_clbits
        self.instructions = []
        # (qubit, clbit) pairs — a measure maps one qubit onto one
        # classical bit. Kept out of `instructions` so gate consumers are
        # untouched; see the module docstring.
        self.measurements = []
        # qubit indices reset to |0>. Same separation rationale.
        self.resets = []

    def add_gate(self, name, qubits, params = None):
        self.instructions.append({
            "gate": name,
            "qubits": qubits,
            "params": params or []
        })

    def add_measure(self, qubit, clbit):
        '''
        Record a measurement of one qubit onto one classical bit.

        Stored in the separate `measurements` channel, NOT the gate list,
        so nothing that iterates gates observes it. num_clbits should be
        wide enough to contain clbit; the frontend that produces the
        circuit is responsible for having declared the classical register.
        '''
        self.measurements.append((qubit, clbit))

    def add_reset(self, qubit):
        '''
        Record a reset of one qubit to |0>.

        Stored in the separate `resets` channel for the same reason as
        add_measure — reset is not a unitary gate and must not enter the
        gate list that providers execute.
        '''
        self.resets.append(qubit)

    def get_depth(self):
        if not self.instructions:
            return 0
        
        # Track the current depth level for each physical qubit
        qubit_depths = [0] * self.num_qubits
        
        for inst in self.instructions:
            target_qubits = inst["qubits"]
            
            # Find the current max depth among qubits involved in this gate
            current_max = max(qubit_depths[q] for q in target_qubits)
            
            # Increment depth for all involved qubits
            for q in target_qubits:
                qubit_depths[q] = current_max + 1
                
        return max(qubit_depths)