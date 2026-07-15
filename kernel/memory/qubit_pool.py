'''
Tags: Main

QubitPool — Tracks which physical qubits are free vs allocated.

The single source of truth for qubit occupancy. Allocators reserve
qubits via allocate(); the kernel returns them via free() when a
job's future resolves.
'''
class QubitPool:
    def __init__(self, num_qubits):
        self.free_qubits = set(range(num_qubits))

    def allocate(self, qubits):
        for q in qubits:
            if q not in self.free_qubits:
                return False

        for q in qubits:
            self.free_qubits.remove(q)

        return True

    def free(self, qubits):
        for q in qubits:
            self.free_qubits.add(q)

    def available(self):
        return sorted(self.free_qubits)