// SURVIVOR FIXTURE for the noiseless reference method (see block_fidelity).
// A Bell pair, then RESET q0. After entanglement, resetting one qubit
// leaves the other in a genuinely MIXED state (50/50) — a classical
// mixture, not a pure superposition. Only a density-matrix simulation can
// represent that; a statevector simulation collapses it and reports the
// WRONG ideal.
//
//   correct (density-matrix) ideal:  {"00": 0.5, "10": 0.5}
//   wrong   (statevector)    ideal:  {"00": 1.0}
//
// This fixture exists so the choice of density_matrix for the reference
// run is ASSERTED, not merely intended: a fixture without a mid-circuit
// reset (Bell, GHZ) cannot tell the two methods apart, and a reset on an
// UNentangled qubit still cannot (its reduced state stays pure). The
// reset must follow entanglement for the mixed state to appear.
OPENQASM 2.0;
include "qelib1.inc";
qreg q[2];
creg c[2];
h q[0];
cx q[0], q[1];
reset q[0];
measure q[0] -> c[0];
measure q[1] -> c[1];
