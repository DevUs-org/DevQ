'''
Tags: Fake

DevQ Backend Factory — Generates simulated quantum device parameters.

Provides topology generation and realistic error map simulation for
DevQ's own simulated backends. All outputs are plain Python — no
dependency on any external quantum framework.

Supported topologies:
    - fully_connected : every qubit connected to every other
    - linear          : qubits connected in a chain
    - grid            : qubits connected in a 2D grid (perfect square only)
    - random          : randomly connected qubits

Called internally by DevQSimulatedProvider.get_device().
The user never calls this directly.
'''

import random


BASIS_GATES = ["cx", "rz", "sx", "x", "measure"]


def create_backend(kind="fully_connected", num_qubits=5) -> dict:
    if num_qubits < 2:
        raise ValueError("Number of qubits must be at least 2.")

    kind = kind.lower()

    if kind == "fully_connected":
        coupling_map = _fully_connected(num_qubits)
    elif kind == "linear":
        coupling_map = _linear(num_qubits)
    elif kind == "grid":
        coupling_map = _grid(num_qubits)
    elif kind == "random":
        coupling_map = _random(num_qubits)
    else:
        raise ValueError(
            f"Unknown backend kind: '{kind}'. "
            "Choose from: fully_connected, linear, grid, random."
        )

    error_map      = _generate_qubit_errors(num_qubits)
    edge_error_map = _generate_edge_errors(coupling_map)

    return {
        "name"         : f"{kind}_backend",
        "num_qubits"   : num_qubits,
        "coupling_map" : coupling_map,
        "basis_gates"  : BASIS_GATES,
        "error_map"    : error_map,
        "edge_error_map": edge_error_map
    }


# ── Topology generators ───────────────────────────────────────────────────────

def _fully_connected(num_qubits) -> list:
    return [
        (i, j)
        for i in range(num_qubits)
        for j in range(i + 1, num_qubits)
    ]


def _linear(num_qubits) -> list:
    return [(i, i + 1) for i in range(num_qubits - 1)]


def _grid(num_qubits) -> list:
    if num_qubits < 4 or int(num_qubits ** 0.5) ** 2 != num_qubits:
        raise ValueError(
            "Grid backend requires a perfect square number of qubits >= 4."
        )
    coupling_map = []
    grid_size    = int(num_qubits ** 0.5)

    for i in range(grid_size):
        for j in range(grid_size):
            qubit = i * grid_size + j
            if j < grid_size - 1:
                coupling_map.append((qubit, qubit + 1))
            if i < grid_size - 1:
                coupling_map.append((qubit, qubit + grid_size))

    return coupling_map


def _random(num_qubits, edge_probability=0.3) -> list:
    coupling_map = [
        (i, j)
        for i in range(num_qubits)
        for j in range(i + 1, num_qubits)
        if random.random() < edge_probability
    ]

    # Guarantee at least one edge so the device is never fully disconnected
    if not coupling_map:
        coupling_map.append((0, 1))

    return coupling_map


# ── Error map generators ──────────────────────────────────────────────────────

def _generate_qubit_errors(num_qubits) -> dict:
    '''
    Simulate per-qubit readout error rates.
    Range 0.1% to 5% — consistent with real NISQ device calibration data.
    '''
    return {
        q: random.uniform(0.001, 0.05)
        for q in range(num_qubits)
    }


def _generate_edge_errors(coupling_map) -> dict:
    '''
    Simulate per-edge two-qubit gate error rates.
    Range 0.5% to 5% — consistent with real NISQ device calibration data.
    Keys are raw tuples here — QuantumDevice normalises them to sorted tuples.
    '''
    return {
        (u, v): random.uniform(0.005, 0.05)
        for (u, v) in coupling_map
    }