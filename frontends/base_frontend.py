'''
Tags: Main

BaseFrontend — Abstract base class for all DevQ circuit frontends.

A frontend lowers a source representation of a circuit into CircuitRep,
DevQ's hardware-independent internal format. It is the 5th registrable
component kind (after scheduler, allocator, router, provider).

Every frontend must implement:
  - parse(source) : read the source file and return a CircuitRep

Every frontend must declare:
  - EXTENSIONS    : the file extensions it claims, lowercase, with the
                    leading dot (e.g. (".qasm",)). DevQ builds an
                    extension -> frontend map from these at build time,
                    so registering a frontend makes its sources
                    dispatchable with no further edit.

PARAMETERLESS BY DESIGN. Unlike the other kinds, a frontend takes no
constructor arguments. The reason the others do is that DevQ INJECTS
resolved state at construction — a scheduler receives its device's
memory manager, a router receives cascade-resolved weights. A frontend
has no such injected state: it is a pure source -> CircuitRep function
with a class around it, so there is nothing for DevQ to pass. A
frontend that later needs a knob declares a namespaced CONFIG_SCHEMA
key (the router precedent), which cascades and shows in qconfig, rather
than taking a constructor argument that would hide from it.

NOT SELECTED, DISPATCHED. There is deliberately no config key that
names one frontend as "the" frontend. A frontend is chosen per job from
the source's extension; see frontends/resolver.py. This is what lets a
single session read several source languages at once.
'''

from abc import ABC, abstractmethod


class BaseFrontend(ABC):

    # File extensions this frontend claims, lowercase and dotted. DevQ
    # builds the extension -> frontend dispatch map from this attribute
    # across every registered frontend. A frontend that left this empty
    # would register successfully but never be reachable by extension —
    # only via an explicit --frontend override — so a non-empty value is
    # the norm. Declared as a class attribute (not an __init__ argument)
    # so the map can be built without constructing anything.
    EXTENSIONS: tuple = ()

    @abstractmethod
    def parse(self, source):
        '''
        Lower a source file into a CircuitRep.

        Args:
            source: path to the source file to read.

        Returns:
            CircuitRep — the flat, hardware-independent instruction list
            (gates with parameters, and — once Piece 3 lands —
            measure/reset instructions and classical bits) that
            allocators, schedulers and providers consume.

        Raises:
            The frontend raises on malformed source. Callers (the shell,
            the spec runner) wrap that in their own error type with the
            job's provenance; a frontend need not know who called it.
        '''
        pass