// Custom gate with a parameter and two qubits, plus a recursive custom.
OPENQASM 2.0;
include "qelib1.inc";
qreg q[3];
gate entangle(theta) a, b {
  rz(theta) a;
  cx a, b;
}
gate double(theta) a, b {
  entangle(theta) a, b;
  entangle(theta) b, a;
}
entangle(pi/2) q[0], q[1];
double(pi/4) q[1], q[2];
