from .qubit_pool import QubitPool
from .allocators.noise_graph_allocator import NoiseGraphAllocator

class MemoryManager:

    def __init__(self, device):
        self.device = device
        self.pool = QubitPool(device.num_qubits)
        self.allocator = NoiseGraphAllocator() # TODO: Make Configurable, or GraphAllocator(), or StaticAloocator()

    def allocate(self, circuit):
        return self.allocator.allocate(
            circuit,
            self.device,
            self.pool
        )
    
    def free(self, qubits):
        self.pool.free(qubits)