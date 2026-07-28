// Several qregs flatten into one global index space.
OPENQASM 2.0;
include "qelib1.inc";
qreg a[2];
qreg b[3];
creg c[5];
h a[0];
cx a[1], b[0];
measure b[2] -> c[4];
