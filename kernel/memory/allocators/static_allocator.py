'''
Tags: Alt

StaticAllocator — First available qubits, no topology or noise
awareness.

Pipeline-correctness baseline, and a sensible choice for
fully-connected hardware (e.g. IonQ trapped ions) where connectivity
is unconstrained. Applies the qubit threshold only — it has no
topology concept, so the edge threshold is ignored by design, and
the inherited base feasible() (eligible-qubit count) is exact.
'''

from .base_allocator import BaseAllocator
from .filtering import eligible_qubits

class StaticAllocator(BaseAllocator):

    def allocate(self, circuit, device, pool,
                 max_qubit_error=None, max_edge_error=None):
        # Static allocation has no topology concept — the edge threshold
        # is not applicable here and is ignored by design. Suitable for
        # fully-connected hardware (e.g. IonQ) where edges are uniform.
        required = circuit.num_qubits
        free     = pool.available()
        allowed  = eligible_qubits(device, free, max_qubit_error)
        usable   = [q for q in free if q in allowed]  # preserve pool order

        if len(usable) < required:
            raise Exception("Not enough qubits available")

        selected = usable[:required]
        pool.allocate(selected)
        return {v: p for v, p in enumerate(selected)}