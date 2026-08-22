'''
Tags: Main

frontends — Source-language readers that lower to CircuitRep.

A frontend parses some source representation of a circuit (OpenQASM
2.0 today; OpenQASM 3.0, Silq, Q# or a Qiskit circuit later) into
CircuitRep, the hardware-independent internal format everything
downstream operates on. Frontend is a registrable component kind, so a
new source language is reachable by registering it — no core edit.

WHY FRONTEND IS UNLIKE THE OTHER KINDS. A router, scheduler or
allocator is SELECTED: the config cascade names the one that runs. A
frontend is not selected — it is DISPATCHED per job by the source
itself. One session may take a .qasm job and a .silq job in the same
queue, each read by its own frontend. So there is no "frontend" config
key naming a single winner; instead every registered frontend is
available, and resolve_frontend() picks the right one for each job
from its extension (or an explicit override when an extension is
ambiguous). See resolver.py.
'''