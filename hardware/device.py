'''
Tags: Main

DevQ Device Definition

QuantumDevice is a pure data container for a quantum hardware device.
All error map generation and device parameter provision is the
responsibility of the provider — not this class.

Execution is delegated to the provider via device.execute(), keeping
the kernel decoupled from any provider-specific logic.
'''

from .topology_graph import build_graph


class QuantumDevice:
    def __init__(self, name, num_qubits, coupling_map, basis_gates,
                 error_map, edge_error_map, provider):

        self.name         = name
        self.num_qubits   = num_qubits
        self.coupling_map = coupling_map
        self.basis_gates  = basis_gates
        self.provider     = provider
        self.graph        = build_graph(coupling_map, num_qubits)
        self.error_map = error_map
        self.edge_error_map = {
            tuple(sorted((u, v))): err
            for (u, v), err in edge_error_map.items()
        }

    def execute(self, circuit, v2p_map):
        '''
        Delegate execution to the provider.
        Returns an ExecutionFuture.
        '''
        return self.provider.execute(circuit, v2p_map)

    def qubit_error(self, q):
        return self.error_map.get(q, 0.01)

    def edge_error(self, u, v):
        return self.edge_error_map.get(tuple(sorted((u, v))), 0.05)

    def __repr__(self):
        return (f"QuantumDevice(name={self.name}, "
                f"num_qubits={self.num_qubits}, "
                f"provider={type(self.provider).__name__})")