from collections import deque

from .base_allocator import BaseAllocator
from .filtering import eligible_qubits, edge_allowed, has_connected_block


class GraphAllocator(BaseAllocator):

    def allocate(self, circuit, device, pool,
                 max_qubit_error=None, max_edge_error=None):

        required = circuit.num_qubits
        usable   = eligible_qubits(device, pool.free_qubits, max_qubit_error)
        G        = device.graph

        for start in usable:

            visited = []
            queue = deque([start])

            while queue and len(visited) < required:
                q = queue.popleft()

                if q not in visited and q in usable:
                    visited.append(q)

                    for neighbor in G.neighbors(q):
                        if (neighbor in usable
                                and neighbor not in visited
                                and edge_allowed(device, q, neighbor,
                                                 max_edge_error)):
                            queue.append(neighbor)

            if len(visited) >= required:
                selected = visited[:required]
                pool.allocate(selected)
                return {v: p for v, p in enumerate(selected)}

        raise Exception("No connected qubit block available")
    
    def feasible(self, circuit, device,
                 max_qubit_error=None, max_edge_error=None):
        reason = super().feasible(circuit, device,
                                  max_qubit_error, max_edge_error)
        if reason:
            return reason

        eligible = eligible_qubits(
            device, range(device.num_qubits), max_qubit_error
        )

        if not has_connected_block(device, eligible,
                                   circuit.num_qubits, max_edge_error):
            return (f"no connected block of {circuit.num_qubits} qubits "
                    f"exists on this device under "
                    f"max_qubit_error={max_qubit_error}, "
                    f"max_edge_error={max_edge_error}")

        return None