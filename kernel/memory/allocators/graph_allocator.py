from collections import deque

class GraphAllocator:

    def allocate(self, circuit, device, pool):

        required = circuit.num_qubits
        free_qubits = pool.free_qubits
        G = device.graph

        for start in free_qubits:

            visited = []
            queue = deque([start])

            while queue and len(visited) < required:
                q = queue.popleft()

                if q not in visited and q in free_qubits:
                    visited.append(q)

                    for neighbor in G.neighbors(q):
                        if neighbor in free_qubits and neighbor not in visited:
                            queue.append(neighbor)

            if len(visited) >= required:
                selected = visited[:required]
                pool.allocate(selected)
                return {v: p for v, p in enumerate(selected)}

        raise Exception("No connected qubit block available")