'''
Tags: Main

Example entry point — launches a DevQ session on the IBM simulated
provider (FakeNairobiV2) with an example user config. Edit the
provider, backend, or config path here; everything else is handled
by the DevQ core.
'''

from devq import DevQ
from hardware.providers.ibm.ibm_simulated_provider import IBMSimulatedProvider

if __name__ == "__main__":
    DevQ(IBMSimulatedProvider().get_device("FakeNairobiV2"), config_path='./config/example_devq.config.json').start()