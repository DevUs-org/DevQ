'''
Tags: Main

BaseProvider — Abstract base class for all DevQ hardware providers.

Every provider must implement:
  - get_device()  : construct and return a fully formed QuantumDevice
  - execute()     : run a circuit on the underlying backend

Optionally override:
  - preferred_config() : express provider-level config preferences
    These sit between DevQ core defaults and the user config file.
    Return only the keys you want to override — omit the rest.
'''

from abc import ABC, abstractmethod


class BaseProvider(ABC):

    def __init__(self, seed=None):
        '''
        Args:
            seed : int or None — base seed for reproducible behaviour.
                   Providers that have stochastic behaviour (topology or
                   error map generation, noisy simulation) derive their
                   randomness from this seed; providers with none inherit
                   and ignore it. None (default) preserves unseeded,
                   non-deterministic behaviour.
        '''
        self.seed = seed

    @abstractmethod
    def get_device(self, *args, **kwargs):
        '''
        Construct and return a fully formed QuantumDevice for this provider.
        The returned device must have self set as device.provider.
        A single provider instance may serve multiple devices; any
        per-device state must be keyed by device name.
        '''
        pass

    @abstractmethod
    def execute(self, circuit, v2p_map, shots, device):
        '''
        Execute a circuit on the underlying backend.

        Args:
            circuit  : CircuitRep
            v2p_map  : dict — virtual to physical qubit mapping
            shots    : number of shots
            device   : QuantumDevice — the device this job was allocated
                       to. Providers serving multiple devices use this to
                       select per-device state (backend, noise model).

        Returns:
            ExecutionFuture
        '''
        pass

    def preferred_config(self) -> dict:
        '''
        Override to express provider-level configuration preferences.

        These override DevQ core defaults but are themselves overridden
        by the user's local config file. Return only the keys you want
        to set — omit keys you are happy to leave at core defaults.

        Example:
            return {"allocator": "static", "shots": 2048}

        Valid keys: "scheduler", "allocator", "shots"
        '''
        return {}