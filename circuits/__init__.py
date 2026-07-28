'''
Tags: Main

circuits — Circuit representation and execution results.

CircuitRep (hardware-independent internal format) and ExecutionResult /
ExecutionFuture.

Frontends — the registrable source-language readers that lower source
into CircuitRep — live in the top-level frontends/ package, including
the OpenQASM 2.0 parser (frontends/qasm2/). Nothing in circuits/ reads
source; this package holds only the format circuits are lowered into and
the result types they execute to.
'''