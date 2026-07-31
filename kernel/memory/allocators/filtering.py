'''
Tags: Main

Threshold filtering helpers shared by all allocators.

Implements the hard-constraint layer of the job-level threshold system:
qubits and edges whose error rates exceed the job's threshold are
excluded from allocation entirely, before any cost-based optimisation.

A threshold of None means no filtering on that dimension.
'''

from collections import deque


def eligible_qubits(device, free_qubits, max_qubit_error=None,
                    max_1q_gate_error=None):
    '''
    Return the subset of free_qubits passing every per-qubit threshold.

    A qubit is eligible only if it satisfies ALL supplied thresholds:
      - readout error <= max_qubit_error       (None -> no readout filter)
      - 1-qubit gate error <= max_1q_gate_error (None -> no gate filter)

    The two are ANDed: a qubit with a clean readout but a noisy 1-qubit
    gate (or vice versa) is excluded if it fails either. A qubit passes
    trivially on any dimension whose threshold is None.
    '''
    if max_qubit_error is None and max_1q_gate_error is None:
        return set(free_qubits)

    return {
        q for q in free_qubits
        if (max_qubit_error is None or device.qubit_error(q) <= max_qubit_error)
        and (max_1q_gate_error is None or device.gate_error(q) <= max_1q_gate_error)
    }


def edge_allowed(device, u, v, max_edge_error=None):
    '''
    True if the edge (u, v) is usable under max_edge_error.
    None threshold -> every edge passes.
    '''
    if max_edge_error is None:
        return True

    return device.edge_error(u, v) <= max_edge_error


def has_connected_block(device, eligible, required, max_edge_error=None):
    '''
    True if the device graph, restricted to `eligible` qubits and to
    edges allowed under max_edge_error, contains a connected component
    of size >= required.

    This is exactly the reachability question the graph allocators' BFS
    answers on a fully free pool, making it the feasibility test for
    both GraphAllocator and NoiseGraphAllocator: cost weighting changes
    which block is preferred, never whether one exists.
    '''
    if required <= 1:
        return len(eligible) >= required

    G    = device.graph
    seen = set()

    for start in eligible:
        if start in seen:
            continue

        component = set()
        queue     = deque([start])

        while queue:
            q = queue.popleft()
            if q in component:
                continue
            component.add(q)

            for n in G.neighbors(q):
                if (n in eligible and n not in component
                        and edge_allowed(device, q, n, max_edge_error)):
                    queue.append(n)

        seen |= component
        if len(component) >= required:
            return True

    return False