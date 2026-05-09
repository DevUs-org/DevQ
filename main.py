'''
Entry point for DevQ.

Initialises the device via a provider, loads it into the kernel,
and starts the QShell interactive session.

To switch providers, swap out the load_device() call:

    # DevQ simulated (default)
    device = load_device(DevQSimulatedProvider().get_device("random", num_qubits=10))

    # IBM Qiskit simulated (once implemented)
    # device = load_device(IBMQiskitSimulatedProvider().get_device("FakeNairobi"))
'''

from hardware.device_loader import load_device
from hardware.providers.devq.devq_simulated_provider import DevQSimulatedProvider
from kernel.kernel import Kernel
from shell.qshell import QShell

if __name__ == "__main__":
    device = load_device(DevQSimulatedProvider().get_device("random", num_qubits=10))
    kernel = Kernel(device)
    shell  = QShell(kernel)
    shell.cmdloop()