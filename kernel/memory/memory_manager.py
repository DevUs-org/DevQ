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
        else a human-readable reason it never can be.

        Two independent reasons a device can never run a circuit, checked
        in order:

        1. CAPABILITY (execution model). A dynamic circuit — one using
           classical feedback (is_dynamic) — can only run on a provider
           whose runtime honours feedback. This is a property of the
           PROVIDER's execution model, not of the allocator (whose
           feasible() contract is purely about whether the device's qubits
           and error rates can host the circuit). So it is checked HERE,
           against the device's provider, before delegating: the allocator
           answers "can these qubits host it", the provider answers "can my
           runtime run its control flow", and this method composes both.
           Keeping the two apart means the allocator never learns about
           execution-model capability and the provider never learns about
           qubit placement.

        2. ALLOCATION. Otherwise, ask the active allocator whether the
           circuit could ever be placed on this device (pool state aside).

        The router calls this per candidate device and keeps only the ones
        that return None, so a dynamic circuit is routed to a capable
        device when one is attached and REJECTED — with a per-device reason
        — only when none is. This is the same per-candidate feasibility the
        router already used for allocation; capability is one more reason a
        candidate can be infeasible, expressed entirely in DevQ's terms.
        '''
        if circuit.is_dynamic and not self.device.provider.supports_dynamic(
                circuit):
            return (
                f"provider {type(self.device.provider).__name__} does not "
                f"support classical feedback (dynamic circuit); its execution "
                f"model cannot run a gate conditioned on a mid-circuit "
                f"measurement")

        return self.allocator.feasible(
            circuit,
            self.device,
            max_qubit_error=max_qubit_error,
            max_edge_error=max_edge_error,
            max_1q_gate_error=max_1q_gate_error
        )

    def free(self, qubits):
        self.pool.free(qubits)