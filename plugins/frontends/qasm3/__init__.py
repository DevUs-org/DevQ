'''
Tags: Plugin

qasm3 frontend — an opt-in DevQ frontend for OpenQASM 3.0.

Parses OpenQASM 3.0 with the OFFICIAL openqasm3 reference parser
(`openqasm3[parser]`) and walks the AST into CircuitRep with its own,
qiskit-free walk. Registered by hand, not shipped in the built-in set:

    from plugins.frontends.qasm3.qasm3_frontend import QASM3Frontend
    devq.register_frontend("qasm3", QASM3Frontend())

after which .qasm3 sources dispatch to it, and a .qasm source can be routed
to it with an explicit --frontend (DevQ disambiguates the qasm2/qasm3
both-claim-.qasm case per job).

IMPORT DISCIPLINE. Reaches into DevQ only through plugin_bases (the
contract from base_frontend, CircuitRep from common). openqasm3 is imported
lazily inside parse(); importing the package costs nothing and needs no
openqasm3 installed.
'''
