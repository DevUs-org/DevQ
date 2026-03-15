import networkx as nx

class GraphAllocator:
    def allocate(slef, circuit, device, pool):
        required = circuit.num_qubits
        free_qubits = pool.free_qubits

        G = nx.Graph()
        G.add_nodes_from(range(device.num_qubits))
        G.add_edges_from(device.coupling_map)

        for start in free_qubits:
            visited = []
            queue = [start]

            while queue and len(visited) < required:
                q = queue.pop(0)
                if q not in visited and q in free_qubits:
                    visited.append(q)

                    for neighbor in G.neighbors(q):
                        queue.append(neighbor)

            if len(visited) >= required:
                selected = visited[:required]
                pool.allocate(selected)
                return {v: p for v, p in enumerate(selected)}

        raise Exception("No connected qubit block available")