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
    - On failure: raise an Exception — callers translate this into a
      WAITING or REJECTED job state. Never return None or a partial
      mapping.

    feasible(circuit, device, max_qubit_error=None, max_edge_error=None)
        -> None | str

    - Answers: could this job EVER be allocated on this device under
      these thresholds, assuming every qubit were free? Deliberately
      ignores pool state — that is what separates an unsatisfiable job
      (REJECTED) from one merely blocked on resources (WAITING).
    - Returns None if satisfiable, else a human-readable reason string.
    - A default implementation is provided (enough threshold-eligible
      qubits on the device). Override it if your allocator has stricter
      requirements — e.g. the graph allocators additionally require a
      connected block among eligible qubits.
'''

from abc import ABC, abstractmethod

from .filtering import eligible_qubits


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

    def feasible(self, circuit, device,
                 max_qubit_error=None, max_edge_error=None):
        '''
        Default feasibility check: the device must have enough
        threshold-eligible qubits, pool state aside.

        Exactly sufficient for StaticAllocator (which has no topology
        concept); a sound necessary condition for any allocator.
        '''
        required = circuit.num_qubits
        eligible = eligible_qubits(
            device, range(device.num_qubits), max_qubit_error
        )

        if len(eligible) < required:
            if max_qubit_error is None:
                return (f"circuit needs {required} qubits, "
                        f"device has {device.num_qubits}")
            return (f"circuit needs {required} qubits, only {len(eligible)} "
                    f"on this device satisfy "
                    f"max_qubit_error={max_qubit_error}")

        return None