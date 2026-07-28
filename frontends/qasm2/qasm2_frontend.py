'''
Tags: Main

QASM2Frontend — DevQ's built-in OpenQASM 2.0 frontend.

The reference frontend, shipped registered so DevQ works out of the box
with no third-party dependency: qregistry shows one `frontend` entry and
.qasm sources are dispatchable immediately.

Parses source with the full 2.0 parser in frontends/qasm2/ — a real
tokenizer, an expression evaluator that keeps gate parameters, and
recursive custom-gate inlining. It records measure and reset in
CircuitRep's separate channels (not the gate list), so what the
providers execute is unchanged until a later execution-path phase teaches
them to honour those channels.
'''

from ..base_frontend import BaseFrontend
from .parser import parse, QASMError


class QASM2Frontend(BaseFrontend):
    LABEL = "OpenQASM 2.0"
    EXTENSIONS = (".qasm",)

    def parse(self, source):
        '''
        Lower an OpenQASM 2.0 source file into a CircuitRep.

        Raises:
            ValueError: on a malformed circuit, with the file name and
                        source line. Callers (the shell, the spec runner)
                        already wrap load failures in their own error
                        types; raising a plain ValueError keeps this
                        frontend agnostic about who called it.
        '''
        try:
            with open(source) as handle:
                text = handle.read()
        except OSError as e:
            # Let a missing file surface as-is: the shell prints it and
            # the spec runner maps FileNotFoundError to its own message.
            raise
        try:
            return parse(text, source_name=source)
        except QASMError as e:
            raise ValueError(f"{source}: {e}") from None