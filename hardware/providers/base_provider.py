'''
Tags: Main

BaseProvider — Abstract base class for all DevQ hardware providers.

Every provider must implement:
  - get_device()  : construct and return a fully formed QuantumDevice
  - execute()     : run a circuit on the underlying backend and return an ExecutionFuture

The kernel and QShell never interact with providers directly.
They only speak to QuantumDevice, which holds a reference to its provider
and delegates execution via device.execute() → device.provider.execute().
'''

from abc import ABC, abstractmethod

class BaseProvider(ABC):

    @abstractmethod
    def get_device(self, *args, **kwargs):
        '''
        Construct and return a fully formed QuantumDevice for this provider.

        Each provider defines its own parameters — DevQ takes a topology
        kind and qubit count, IBM takes a backend name, etc.

        The returned QuantumDevice must have:
          - All metadata populated (coupling_map, error_map, edge_error_map, etc.)
          - self set as device.provider
        '''
        pass

    @abstractmethod
    def execute(self, circuit, v2p_map):
        '''
        Execute a circuit on the underlying backend.

        Args:
            circuit  : CircuitRep — the circuit to execute
            v2p_map  : dict — virtual to physical qubit mapping e.g. {0: 3, 1: 7}

        Returns:
            ExecutionFuture — call .result() to get the ExecutionResult
        '''
        pass