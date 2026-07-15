'''
Tags: Main

MemoryManager — Manages qubit allocation and deallocation.

The allocator is injected at construction time via the DevQ config system.
Swap allocators by passing a different instance — no other changes needed.
'''

from .qubit_pool import QubitPool

class MemoryManager:

    def __init__(self, device, allocator):
        self.device    = device
        self.pool      = QubitPool(device.num_qubits)
        self.allocator = allocator

    def allocate(self, circuit, max_qubit_error=None, max_edge_error=None):
        return self.allocator.allocate(
            circuit,
            self.device,
            self.pool,
            max_qubit_error=max_qubit_error,
            max_edge_error=max_edge_error
        )

    def free(self, qubits):
        self.pool.free(qubits)