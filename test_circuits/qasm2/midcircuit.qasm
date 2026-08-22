// Mid-circuit measurement — a gate acts on a qubit AFTER it is measured.
// DevQ's execution model (and the IBM lowering, which hoists measures to
// the end) cannot faithfully run this on ANY backend, so it is a
// circuit-global unrunnable_reason: REJECTED, not routed per-device. This
// is distinct from a dynamic circuit, which is runnable on a capable one.
OPENQASM 2.0;
include "qelib1.inc";
qreg q[1];
creg c[1];
h q[0];
measure q[0] -> c[0];
x q[0];
