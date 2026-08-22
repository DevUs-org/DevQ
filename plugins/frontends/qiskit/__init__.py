'''
Tags: Plugin

qiskit frontend — an opt-in DevQ frontend that reads Qiskit circuits.

Reads a .py source that builds a Qiskit QuantumCircuit and lowers it into
CircuitRep. Registered by hand, not shipped in the built-in set, so
importing `devq` never pulls qiskit in:

    from plugins.frontends.qiskit.qiskit_frontend import QiskitFrontend
    devq.register_frontend("qiskit", QiskitFrontend())

after which .py sources dispatch to it, with no core edit.

IMPORT DISCIPLINE. The plugin reaches into DevQ only through plugin_bases
(the contract from base_frontend, CircuitRep from common). Every qiskit
import is lazy, inside the functions that use qiskit types, so importing
the package costs nothing and needs no qiskit installed; only lowering a
circuit does.
'''
