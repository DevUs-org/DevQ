'''
Tags: Main

Threshold filtering helpers shared by all allocators.

Implements the hard-constraint layer of the two-level threshold system:
qubits and edges whose error rates exceed the effective threshold are
excluded from allocation entirely, before any cost-based optimisation.

A threshold of None means no filtering on that dimension.
'''


def eligible_qubits(device, free_qubits, max_qubit_error=None):
    '''
    Return the subset of free_qubits whose readout error does not
    exceed max_qubit_error. None threshold -> all free qubits pass.
    '''
    if max_qubit_error is None:
        return set(free_qubits)

    return {
        q for q in free_qubits
        if device.qubit_error(q) <= max_qubit_error
    }


def edge_allowed(device, u, v, max_edge_error=None):
    '''
    True if the edge (u, v) is usable under max_edge_error.
    None threshold -> every edge passes.
    '''
    if max_edge_error is None:
        return True

    return device.edge_error(u, v) <= max_edge_error