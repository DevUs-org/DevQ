// Parameterised rotations — the rx(pi/2) case the old reader broke.
OPENQASM 2.0;
include "qelib1.inc";
qreg q[2];
rx(pi/2) q[0];
ry(pi/4) q[1];
rz(2*pi) q[0];
cx q[0], q[1];
