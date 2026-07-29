'''
Tags: Main

DevQ — an operating system layer for quantum execution.

DevQ sits between a circuit and the hardware and makes two pluggable
decisions: which device a job runs on (the router, one per system) and
which physical qubits it uses (the allocator, one per device), with a
scheduler per device deciding queue order — two-level scheduling over a
federation of heterogeneous backends. Its purpose is to let competing
scheduling and allocation algorithms run as plugins in one system on
identical workloads, so they can finally be compared.

See AGENTS.md (the LLM entry point) and README.md for orientation, and
docs/ for the authoritative reference. The research/ package holds
paper and benchmark tooling built on top of DevQ; it is not part of the
system itself.
'''