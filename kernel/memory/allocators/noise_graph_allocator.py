from collections import deque

from .base_allocator import BaseAllocator
from .filtering import eligible_qubits, edge_allowed


class NoiseGraphAllocator(BaseAllocator):

    def allocate(self, circuit, device, pool,
                 max_qubit_error=None, max_edge_error=None):
        ALPHA = 0.1 # Node Cost Factor
        BETA = 0.9 # Edge Cost Factor
        best_score = float("inf")

        required = circuit.num_qubits
        usable   = eligible_qubits(device, pool.free_qubits, max_qubit_error)
        G        = device.graph

        if required == 2:
            best_edge = None

            for (u, v) in device.edge_error_map.keys():
                if (u in usable and v in usable
                        and edge_allowed(device, u, v, max_edge_error)):

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

        for start in sorted(usable):

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