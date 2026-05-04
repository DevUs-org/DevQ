class CircuitRep:
    def __init__(self, num_qubits):
        self.num_qubits = num_qubits
        self.instructions = []

    def add_gate(self, name, qubits, params = None):
        self.instructions.append({
            "gate": name,
            "qubits": qubits,
            "params": params or []
        })

    def get_depth(self):
        """
        Calculates the circuit depth.
        Tracks the current 'time layer' each qubit is occupied until.
        """
        if not self.instructions:
            return 0
        
        # Track the current depth level for each physical qubit
        qubit_depths = [0] * self.num_qubits
        
        for inst in self.instructions:
            target_qubits = inst["qubits"]
            
            # Find the current max depth among qubits involved in this gate
            current_max = max(qubit_depths[q] for q in target_qubits)
            
            # Increment depth for all involved qubits
            for q in target_qubits:
                qubit_depths[q] = current_max + 1
                
        return max(qubit_depths)