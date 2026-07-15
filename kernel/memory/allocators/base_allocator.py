'''
Tags: Main

BaseAllocator — Abstract base class for all qubit allocators.

Defines the allocation contract that MemoryManager and the schedulers
depend on. Any allocator (built-in or third-party, e.g. via qbench)
must implement allocate() with this exact signature.

Contract:
    allocate(circuit, device, pool, max_qubit_error=None, max_edge_error=None)
        -> v2p_map (dict: virtual qubit index -> physical qubit index)

    - Thresholds are hard constraints: qubits/edges exceeding them must
      be excluded from consideration entirely (None = no filtering).
    - On success: the allocator MUST call pool.allocate() on the selected
      physical qubits before returning the mapping.
    - On failure: raise an Exception — callers translate this into the
      WAITING job state. Never return None or a partial mapping.
'''

from abc import ABC, abstractmethod


class BaseAllocator(ABC):

    @abstractmethod
    def allocate(self, circuit, device, pool,
                 max_qubit_error=None, max_edge_error=None):
        '''
        Select physical qubits for the circuit and reserve them in the pool.

        Returns:
            dict mapping virtual qubit index -> physical qubit index

        Raises:
            Exception: if no valid allocation exists under the current
            pool state and thresholds.
        '''
        pass