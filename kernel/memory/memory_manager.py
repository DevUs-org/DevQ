from .qubit_pool import QubitPool
from .allocators.graph_allocator import GraphAllocator

class MemoryManager:

    def __init__(self, device):
        self.device = device
        self.pool = QubitPool(device.num_qubits)
        self.allocator = GraphAllocator() # TODO: Make Configurable

    def allocate(self, circuit):
        return self.allocator.allocate(
            circuit,
            self.device,
            self.pool
        )