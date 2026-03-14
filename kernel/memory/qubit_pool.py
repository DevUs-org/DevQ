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

    def release(self, qubits):
        for q in qubits:
            self.free_qubits.add(q)

    def available(self):
        return sorted(self.free_qubits)