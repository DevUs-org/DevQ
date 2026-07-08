from devq import DevQ
from hardware.providers.ibm.ibm_simulated_provider import IBMSimulatedProvider

if __name__ == "__main__":
    DevQ(IBMSimulatedProvider().get_device("FakeNairobiV2"), config_path='./config/example_devq.config.json').start()