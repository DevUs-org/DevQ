'''
Tags: Main, Adapter

Allows user to load the device onto the DevQ hardware layer.
'''

from .device import QuantumDevice

# Current support is for Qiskit Backends only, can be updated to have other providers, real hardware
# and frameworks in the future.
# This function definition may change in the future depending on how other providers configure their hardware
# and backends.
def load_device(backend):
    return QuantumDevice(
        name=backend.name,
        num_qubits=backend.num_qubits,
        coupling_map=backend.coupling_map,
        basis_gates=backend.target.operation_names
    )