'''
Tags: Main

Internal Circuit Representation definition for DevQ
'''

class CircuitRep:
    def __init__(self, num_qubits):
        self.num_qubits = num_qubits
        self.instructions = []

    def add_gate(self, name, qubits, params = None):
        self.instructions.append({
            "gate": name,
            "qubits": qubits,
            "params": params or []
        })