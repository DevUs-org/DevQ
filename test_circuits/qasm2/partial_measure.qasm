// Three qubits, but only two measured — exercises Option B width:
// results are 3-bit (the creg width), the unmeasured bit c[2] pinned 0.
OPENQASM 2.0;
include "qelib1.inc";
qreg q[3];
creg c[3];
h q[0];
cx q[0], q[1];
measure q[0] -> c[0];
measure q[1] -> c[1];
