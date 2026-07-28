'''
Tags: Main

frontends.qasm2 — the OpenQASM 2.0 parser.

A real tokenizer, expression evaluator, and recursive custom-gate
inliner that lower OpenQASM 2.0 source into CircuitRep. This replaces
the original whitespace-splitting reader, which dropped every gate
parameter, mishandled spacing inside argument lists, and treated
measure as a bogus gate.

parse(source_text, source_name) is the entry point. The built-in
qasm2 frontend (frontends/qasm2_frontend.py) reads a file and calls it.
'''