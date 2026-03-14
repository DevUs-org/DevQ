'''
Tags: Main

QASM Loader and temporary parser
'''

from .circuit_rep import CircuitRep

def load_qasm(file_path: str):
    if not file_path.lower().endswith(".qasm"):
        raise ValueError("Need a QASM file to load instructions.")

    with open(file_path) as f:
        lines = f.readlines()

    circuit = None

    for line in lines:
        line = line.strip()

        if not line:
            continue

        if line.startswith("qreg"):
            qubits = int(line.split("[")[1].split("]")[0])
            circuit = CircuitRep(qubits)
            continue

        if line.startswith(("OPENQASM", "include", "creg", "barrier", "//")):
            continue

        if "[" not in line or circuit is None:
            continue

        line = line.replace(";", "")
        parts = line.split()

        if len(parts) < 2:
            continue

        gate = parts[0]
        args = parts[1]

        qubits = []
        for q in args.split(","):
            q = q.strip()
            if "[" not in q or "]" not in q:
                continue
            qubits.append(int(q.split("[")[1].split("]")[0]))

        circuit.add_gate(gate, qubits)

    if circuit is None:
        raise ValueError("No qreg declaration found in QASM file.")

    return circuit