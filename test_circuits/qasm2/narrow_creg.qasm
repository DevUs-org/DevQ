// 3 qubits but a 2-bit creg: width == num_clbits (2), NOT num_qubits (3).
// Distinguishes Option B (creg width) from measuring all qubits.
OPENQASM 2.0;
include "qelib1.inc";
qreg q[3];
creg c[2];
h q[0];
cx q[0], q[1];
measure q[0] -> c[0];
measure q[1] -> c[1];
