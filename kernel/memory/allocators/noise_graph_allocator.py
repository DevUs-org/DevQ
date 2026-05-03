from collections import deque

class NoiseGraphAllocator:

    def allocate(self, circuit, device, pool):
        ALPHA = 0.1 # Node Cost Factor
        BETA = 0.9 # Edge Cost Factor
        best_score = float("inf")
        
        required = circuit.num_qubits
        free_qubits = pool.free_qubits
        G = device.graph

        if required == 2:
            best_edge = None

            for (u, v) in device.edge_error_map.keys():
                if u in free_qubits and v in free_qubits:

                    node_cost = device.qubit_error(u) + device.qubit_error(v)
                    edge_cost = device.edge_error(u, v)

                    score = ALPHA * node_cost + BETA * edge_cost

                    if score < best_score:
                        best_score = score
                        best_edge = (u, v)

            if best_edge is None:
                raise Exception("No connected qubit pair available")

            pool.allocate(list(best_edge))

            return {0: best_edge[0], 1: best_edge[1]}

        best_block = None

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
                score = 0

                # Node cost
                for q in candidate:
                    score += ALPHA * device.qubit_error(q)

                # Edge cost
                for u in candidate:
                    for v in G.neighbors(u):
                        if v in candidate and u < v:
                            score += BETA * device.edge_error(u, v)

                # Block Choosing
                if score < best_score:
                    best_score = score
                    best_block = sorted(candidate)

        if best_block is None:
            raise Exception("No connected qubit block available")

        pool.allocate(best_block)

        return {v: p for v, p in enumerate(best_block)}