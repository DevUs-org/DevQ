'''
Tags: Main, Adapter

DevQSimulatedProvider — DevQ's own simulated hardware provider.

Uses DevQ's backend_factory for topology and error map generation.
get_device() consolidates backend parameters into a QuantumDevice.
execute() returns mocked results to verify allocation and scheduling
behaviour without any dependency on an external quantum framework.

Usage:
    from hardware.providers.devq.devq_simulated_provider import DevQSimulatedProvider
    from hardware.device_loader import load_device

    device = load_device(DevQSimulatedProvider().get_device("random", num_qubits=10))
    kernel = Kernel(device)
    shell  = QShell(kernel)
'''

from hardware.providers.base_provider import BaseProvider
from hardware.device import QuantumDevice
from hardware.providers.devq.backend_factory import create_backend


class DevQSimulatedProvider(BaseProvider):

    def get_device(self, kind="fully_connected", num_qubits=5) -> QuantumDevice:
        '''
        Build a simulated QuantumDevice using DevQ's backend topologies.

        Delegates topology and error map generation entirely to
        backend_factory, then consolidates the result into a QuantumDevice.

        Args:
            kind:       "fully_connected" | "linear" | "grid" | "random"
            num_qubits: number of qubits on the device

        Returns:
            QuantumDevice with all parameters populated and self as provider
        '''
        backend = create_backend(kind, num_qubits)

        return QuantumDevice(
            name           = backend["name"],
            num_qubits     = backend["num_qubits"],
            coupling_map   = backend["coupling_map"],
            basis_gates    = backend["basis_gates"],
            error_map      = backend["error_map"],
            edge_error_map = backend["edge_error_map"],
            provider       = self
        )

    def execute(self, circuit, v2p_map):
        '''
        Mock execution for DevQ's simulated provider.

        Does not execute circuits on any real or simulated backend.
        Validates the v2p_map against the circuit and returns mocked
        counts with an equal distribution across all possible outcomes,
        allowing the full pipeline including QShell to be tested without
        any quantum framework dependency.

        Args:
            circuit : CircuitRep — the circuit to execute
            v2p_map : dict — virtual to physical qubit mapping

        Returns:
            ExecutionFuture wrapping a mocked ExecutionResult
        '''
        from circuits.execution_result import ExecutionResult, ExecutionFuture

        if len(v2p_map) < circuit.num_qubits:
            return ExecutionFuture(ExecutionResult(
                counts  = {},
                success = False,
                error   = (
                    f"Insufficient qubits in v2p_map. "
                    f"Circuit needs {circuit.num_qubits}, "
                    f"got {len(v2p_map)}."
                )
            ))

        # Equal distribution across all 2^n possible measurement outcomes
        num_states  = 2 ** circuit.num_qubits
        shots       = 1024
        mock_counts = {
            format(i, f"0{circuit.num_qubits}b"): shots // num_states
            for i in range(num_states)
        }

        return ExecutionFuture(ExecutionResult(
            counts  = mock_counts,
            success = True
        ))