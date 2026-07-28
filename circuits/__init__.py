'''
Tags: Main

circuits — Circuit representation and execution results.

CircuitRep (hardware-independent internal format), the OpenQASM
reference reader (qasm_loader), and ExecutionResult / ExecutionFuture.

Frontends — the registrable source-language readers that lower to
CircuitRep — live in the top-level frontends/ package. qasm_loader is
the reader the built-in qasm2 frontend currently delegates to.
'''