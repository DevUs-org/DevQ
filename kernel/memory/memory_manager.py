'''
Tags: Main

MemoryManager — Manages qubit allocation and deallocation.

The allocator CLASS named by the `allocator` config key is constructed
once per device (with that device's resolved cost weights) and injected
here — components are registered as classes, never instances, so
per-device state never leaks across devices. Swap allocators via the
`allocator` config key; no code change needed. See docs/REGISTRY.md.

Also the classification entry point for allocation failures:
unsatisfiable_reason() asks the active allocator whether a job could
ever be allocated on this device (pool state aside). Callers use it to
translate an allocation failure into WAITING (transient contention)
or REJECTED (permanently unsatisfiable).
'''

from .qubit_pool import QubitPool


class AllocatorContractError(Exception):
    '''
    Raised when an allocator violates its post-condition — returning a
    mapping whose physical qubits it did not reserve in the pool. Distinct
    from AllocationError (a legitimate "cannot place"): this is a bug in the
    allocator that would cause silent double-booking, surfaced loudly.
    '''
    pass


class MemoryManager:

    def __init__(self, device, allocator):
        self.device    = device
        self.pool      = QubitPool(device.num_qubits)
        self.allocator = allocator

    def allocate(self, circuit, max_qubit_error=None, max_edge_error=None,
                 max_1q_gate_error=None):
        mapping = self.allocator.allocate(
            circuit,
            self.device,
            self.pool,
            max_qubit_error=max_qubit_error,
            max_edge_error=max_edge_error,
            max_1q_gate_error=max_1q_gate_error
        )

        # Enforce the reserve-on-success contract: every physical qubit the
        # allocator mapped to must actually be reserved in the pool (absent
        # from free_qubits). An allocator that returns a mapping without
        # reserving would let the NEXT job be handed the same physical
        # qubits — a silent double-booking. Surface it here, named, instead.
        if mapping is not None:
            unreserved = [p for p in mapping.values()
                          if p in self.pool.free_qubits]
            if unreserved:
                raise AllocatorContractError(
                    f"{type(self.allocator).__name__}.allocate() returned a "
                    f"mapping using physical qubit(s) {sorted(unreserved)} "
                    f"that it did not reserve in the pool. An allocator MUST "
                    f"call pool.allocate() on its chosen qubits before "
                    f"returning (see BaseAllocator's contract)."
                )

        return mapping

    def unsatisfiable_reason(self, circuit,
                             max_qubit_error=None, max_edge_error=None,
                             max_1q_gate_error=None):
        '''
        None if the job is satisfiable on a fully free device,
        else the allocator's human-readable reason it never can be.
        '''
        return self.allocator.feasible(
            circuit,
            self.device,
            max_qubit_error=max_qubit_error,
            max_edge_error=max_edge_error,
            max_1q_gate_error=max_1q_gate_error
        )

    def free(self, qubits):
        self.pool.free(qubits)