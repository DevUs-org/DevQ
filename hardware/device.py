'''
Tags: Main

DevQ Device Definition
'''

from .topology_graph import build_graph
import random

class QuantumDevice:
    def __init__(self, name, num_qubits, coupling_map, basis_gates, error_map=None):

        self.name = name
        self.num_qubits = num_qubits
        self.coupling_map = coupling_map
        self.basis_gates = basis_gates
        self.graph = build_graph(coupling_map, num_qubits)

        self.error_map = {
            q: random.uniform(0.001, 0.5)
            for q in range(self.num_qubits)
        } if not error_map else error_map

    def qubit_error(self, q):
        return self.error_map.get(q, 0.01)

    def __repr__(self):
        return f"QuantumDevice(name={self.name}, num_qubits={self.num_qubits})"