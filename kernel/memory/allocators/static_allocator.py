class StaticAllocator:

    def allocate(self, circuit, device, pool):
        required = circuit.num_qubits
        free = pool.available()
        if len(free) < required:
            raise Exception("Not enough qubits available")

        selected = free[:required]
        pool.allocate(selected)
        return {v: p for v, p in enumerate(selected)}