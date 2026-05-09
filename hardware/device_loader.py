'''
Tags: Main

load_device — Entry point for loading a QuantumDevice into DevQ.

Every provider's get_device() returns a QuantumDevice.
load_device() validates it and returns it for use with Kernel(device).

Usage:
    from hardware.device_loader import load_device
    from hardware.providers.devq.devq_simulated_provider import DevQSimulatedProvider

    device = load_device(DevQSimulatedProvider().get_device("random", num_qubits=10))
    kernel = Kernel(device)
    shell  = QShell(kernel)
'''

from .device import QuantumDevice


def load_device(device: QuantumDevice) -> QuantumDevice:
    if not isinstance(device, QuantumDevice):
        raise TypeError(
            f"Expected a QuantumDevice, got {type(device).__name__}. "
            "Use a provider's get_device() to construct a device."
        )
    return device