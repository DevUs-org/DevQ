from collections import deque


class NoiseGraphAllocator:

    def allocate(self, circuit, device, pool):

        required = circuit.num_qubits
        free_qubits = pool.free_qubits
        G = device.graph

        best_block = None
        best_score = float("inf")  # lower is better

        for start in sorted(free_qubits):

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

                candidate = visited[:required]

                # --- SCORE BLOCK ---
                score = 0
                for q in candidate:
                    score += device.qubit_error(q)

                # keep best block
                if score < best_score:
                    best_score = score
                    best_block = candidate

        if best_block is None:
            raise Exception("No connected qubit block available")

        pool.allocate(best_block)

        return {v: p for v, p in enumerate(best_block)}