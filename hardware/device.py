'''
ID: Main

DevQ Device Definition
'''

from hardware.topology_graph import build_graph

class QuantumDevice:
    def __init__(self, name, num_qubits, coupling_map, basis_gates):
        self.name = name
        self.num_qubits = num_qubits
        self.coupling_map = coupling_map
        self.basis_gates = basis_gates
        self.graph = build_graph(coupling_map)

    def __repr__(self):
        return f"QuantumDevice(name={self.name}, num_qubits={self.num_qubits})"