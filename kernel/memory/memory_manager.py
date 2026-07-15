'''
Tags: Main

MemoryManager — Manages qubit allocation and deallocation.

The allocator is injected at construction time via the DevQ config system.
Swap allocators by passing a different instance — no other changes needed.

Also the classification entry point for allocation failures:
unsatisfiable_reason() asks the active allocator whether a job could
ever be allocated on this device (pool state aside). Callers use it to
translate an allocation failure into WAITING (transient contention)
or REJECTED (permanently unsatisfiable).
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

    def unsatisfiable_reason(self, circuit,
                             max_qubit_error=None, max_edge_error=None):
        '''
        None if the job is satisfiable on a fully free device,
        else the allocator's human-readable reason it never can be.
        '''
        return self.allocator.feasible(
            circuit,
            self.device,
            max_qubit_error=max_qubit_error,
            max_edge_error=max_edge_error
        )

    def free(self, qubits):
        self.pool.free(qubits)