from .qubit_pool import QubitPool
from .allocators.static_allocator import StaticAllocator

class MemoryManager:

    def __init__(self, device):
        self.device = device
        self.pool = QubitPool(device.num_qubits())
        self.allocator = StaticAllocator() # Configurable

    def allocate(self, circuit):
        return self.allocator.allocate(
            circuit,
            self.device,
            self.pool
        )