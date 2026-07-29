// SURVIVOR FIXTURE for fidelity's marginalisation (see block_fidelity).
// q0 is flipped to |1>, q1 stays |0>, but the measures SWAP indices:
//   measure q0 -> c1   (q0's outcome lands on classical bit 1)
//   measure q1 -> c0   (q1's outcome lands on classical bit 0)
// So the ideal classical string is c1c0 = "10", NOT "01".
//
// A correct marginalisation follows the measure MAP (qubit -> clbit) and
// yields "10". A broken one that marginalises in QUBIT order (placing
// q0's value at position 0) yields "01" — a DIFFERENT number. The fixture
// exists precisely so those two implementations are numerically
// distinguishable: with q0's and q1's clbits aligned, a qubit-order bug
// would survive undetected (the W1 lesson). Here it cannot.
OPENQASM 2.0;
include "qelib1.inc";
qreg q[2];
creg c[2];
x q[0];
measure q[0] -> c[1];
measure q[1] -> c[0];
