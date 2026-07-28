'''
Tags: Main

QASM2Frontend — DevQ's built-in OpenQASM 2.0 frontend.

The reference frontend, shipped registered so that DevQ works out of
the box with no third-party dependency: qregistry shows one `frontend`
entry and .qasm sources are dispatchable immediately.

⚠ PIECE 1 STATE. This class currently delegates to load_qasm() — the
existing whitespace-splitting 2.0 reader — UNCHANGED. That reader is
known-incomplete (drops gate parameters, mishandles spacing, treats
measure as a bogus gate); replacing its internals with a real
tokenizer, expression evaluator and custom-gate inlining is Piece 2,
and making measure/reset first-class is Piece 3. Piece 1 only
establishes the contract and dispatch around it, with zero change to
what a circuit parses to today. Do not read the delegation as an
endorsement of the current parser.
'''

from .base_frontend import BaseFrontend
from circuits.qasm_loader import load_qasm


class QASM2Frontend(BaseFrontend):
    LABEL = "OpenQASM 2.0"
    EXTENSIONS = (".qasm",)

    def parse(self, source):
        '''Lower an OpenQASM 2.0 source file into a CircuitRep.'''
        return load_qasm(source)