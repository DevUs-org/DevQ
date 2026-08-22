// if(creg==N) — classical feedback, now a first-class dynamic circuit.
// The guarded gate acts on a DIFFERENT qubit than the measured one, so it
// is genuine feedback (not mid-circuit reuse): runnable on a device whose
// provider supports_dynamic, declined per-device on one that does not.
OPENQASM 2.0;
include "qelib1.inc";
qreg q[2];
creg c[1];
h q[0];
measure q[0] -> c[0];
if (c==1) x q[1];
