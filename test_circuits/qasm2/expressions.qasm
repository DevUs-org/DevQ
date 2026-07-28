// Expression evaluator: precedence, functions, unary minus, power,
// and binary subtraction/addition (distinct from unary minus).
OPENQASM 2.0;
include "qelib1.inc";
qreg q[1];
rx(2^3) q[0];
ry(sin(0) + cos(0)) q[0];
rz(-pi/2) q[0];
rx(3 - 1) q[0];
ry(5 + 2*3) q[0];
