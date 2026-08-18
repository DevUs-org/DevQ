'''
Tags: Main

engine — DevQ's native, Qiskit-free simulation engine.

A pure-Python (numpy) statevector simulator that interprets a CircuitRep's
gate vocabulary directly, so DevQ can compute a circuit's noiseless ideal —
the fidelity yardstick — WITHOUT requiring the user to attach a
reference-capable provider (an Aer-backed IBM device) and then exclude it
from routing on every job. The engine touches no provider and no kernel
state; it is a leaf that consumes a CircuitRep and returns probabilities or
sampled counts.

This package is built in layers:

  gates.py       — the gate vocabulary: base unitary matrices (constants and
                   parameterised builders) and the alias resolution, plus the
                   guarantee that this vocabulary matches the qasm2 frontend's
                   _BUILTIN_GATES exactly. NOTHING here applies a gate to a
                   state — this is the locked matrix set the rest builds on.
  statevector.py — the state core: gate application by bit-indexing, reset,
                   terminal measurement, and simulate()/run().

Turn A ships gates.py alone: the matrices are locked and verified against
Qiskit before any engine code consumes them.
'''