'''
Tags: Main

Example entry point — launches a multi-device DevQ session:

    d0 — DevQ simulated provider, random 7-qubit topology
    d1 — IBM FakeNairobiV2 (real Nairobi calibration data)
    d2 — IBM FakeLagosV2   (real Lagos calibration data)

The NoiseRouter (default) binds each job to the best feasible device
by noise quality and queue pressure; pin jobs with --exec/--no-exec.
Edit the providers, backends, or config paths here; everything else
is handled by the DevQ core.
'''

from devq import DevQ
from providers.devq.devq_simulated_provider import DevQSimulatedProvider
from providers.ibm.ibm_simulated_provider import IBMSimulatedProvider

if __name__ == "__main__":
    ibm = IBMSimulatedProvider()

    DevQ(config_path='./config/config_examples/router_only.config.json') \
        .add_device(DevQSimulatedProvider().get_device("random", 7)) \
        .add_device(ibm.get_device("FakeNairobiV2")) \
        .add_device(ibm.get_device("FakeLagosV2")) \
        .start()