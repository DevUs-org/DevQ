// x flips to 1, then reset returns to 0, then measure — a reset honoured
// in source position yields ~all-zero; a dropped reset would yield ~all-one.
OPENQASM 2.0;
include "qelib1.inc";
qreg q[1];
creg c[1];
x q[0];
reset q[0];
measure q[0] -> c[0];
