'''
Tags: Fake

Allows user to create fake Qiskit based backends and then load as a device onto the DevQ hardware layer for testing.
'''

from qiskit.providers.backend import BackendV2
from qiskit.transpiler import Target
from qiskit.circuit.library import CXGate, RZGate, SXGate
import random
from qiskit.providers.options import Options
from qiskit.circuit import Parameter

def create_backend(kind="fully_connected", num_qubits=5):
    if num_qubits < 2:
        raise ValueError("Number of qubits must be at least 2.")

    kind = kind.lower()
    if(kind == "fully_connected"):
        return FullyConnectedBackend(num_qubits)
    elif kind == "linear":
        return LinearBackend(num_qubits)
    elif kind == "grid":
        return GridBackend(num_qubits)
    elif kind == "random":
        return RandomBackend(num_qubits)
    else:
        raise ValueError(f"Unknown backend kind: {kind}")
    
def createTargetForBackend(num_qubits, coupling_map) -> Target:
    target = Target(num_qubits=num_qubits)

    # single-qubit gates allowed on all qubits
    phi = Parameter("phi")
    target.add_instruction(RZGate(phi), {(i,): None for i in range(num_qubits)})
    target.add_instruction(SXGate(), {(i,): None for i in range(num_qubits)})

    # CX allowed only on connected qubits
    target.add_instruction(CXGate(), {(q1, q2): None for (q1, q2) in coupling_map})

    return target

class LinearBackend(BackendV2):
    def __init__(self, num_qubits=5):
        super().__init__(name="linear_backend")
        self._num_qubits = num_qubits
        
        self._coupling_map = [(i, i+1) for i in range(num_qubits - 1)]

        self._target = createTargetForBackend(num_qubits, self._coupling_map)

    def __repr__(self):
        return f"{self.name} ({self.num_qubits} qubits)"

    @property
    def num_qubits(self):
        return self._num_qubits

    @property
    def target(self):
        return self._target

    @property
    def coupling_map(self):
        return self._coupling_map
    
    @property
    def max_circuits(self):
        return None
    
    def _default_options(cls):
        return Options()

    def run(self, circuits, **kwargs):
        raise NotImplementedError("This backend cannot execute circuits.")

class FullyConnectedBackend(BackendV2):
    def __init__(self, num_qubits=9):
        super().__init__(name="fully_connected_backend")
        self._num_qubits = num_qubits

        self._coupling_map = [(i, j) for i in range(num_qubits) for j in range(i + 1, num_qubits)]
        
        self._target = createTargetForBackend(num_qubits, self._coupling_map)
    
    def __repr__(self):
        return f"{self.name} ({self.num_qubits} qubits)"

    @property
    def num_qubits(self):
        return self._num_qubits

    @property
    def target(self):
        return self._target

    @property
    def coupling_map(self):
        return self._coupling_map
    
    @property
    def max_circuits(self):
        return None
    
    def _default_options(cls):
        return Options()
    
    def run(self, circuits, **kwargs):
        raise NotImplementedError("This backend cannot execute circuits.")

def isPerfectSquare(n: int) -> bool:
    return int(n**0.5)**2 == n

class GridBackend(BackendV2):
    def __init__(self, num_qubits=9):
        if num_qubits < 4 or not isPerfectSquare(num_qubits):
            raise ValueError("Grid backend requires a perfect square number of qubits greater than or equal to 4.")
        super().__init__(name="grid_backend")

        self._num_qubits = num_qubits
        self._coupling_map = []
        grid_size = int(num_qubits**0.5)
        for i in range(grid_size):
            for j in range(grid_size):
                qubit_index = i * grid_size + j
                if j < grid_size - 1:
                    self._coupling_map.append((qubit_index, qubit_index + 1))
                if i < grid_size - 1:
                    self._coupling_map.append((qubit_index, qubit_index + grid_size))
        
        self._target = createTargetForBackend(num_qubits, self._coupling_map)
    
    def __repr__(self):
        return f"{self.name} ({self.num_qubits} qubits)"

    @property
    def num_qubits(self):
        return self._num_qubits

    @property
    def target(self):
        return self._target

    @property
    def coupling_map(self):
        return self._coupling_map
    
    @property
    def max_circuits(self):
        return None
    
    def _default_options(cls):
        return Options()
    
    def run(self, circuits, **kwargs):
        raise NotImplementedError("This backend cannot execute circuits.")

class RandomBackend(BackendV2):
    def __init__(self, num_qubits=10, edge_probability=0.3):
        super().__init__(name="random_backend")

        self._num_qubits = num_qubits
        self._coupling_map = []

        for i in range(num_qubits):
            for j in range(i + 1, num_qubits):
                if random.random() < edge_probability:
                    self._coupling_map.append((i, j))
        
        if len(self._coupling_map) == 0 and num_qubits > 1:
            self._coupling_map.append((0,1))

        self._target = createTargetForBackend(num_qubits, self._coupling_map)

    def __repr__(self):
        return f"{self.name} ({self.num_qubits} qubits)"

    @property
    def num_qubits(self):
        return self._num_qubits

    @property
    def target(self):
        return self._target

    @property
    def coupling_map(self):
        return self._coupling_map
    
    @property
    def max_circuits(self):
        return None
    
    def _default_options(cls):
        return Options()
    
    def run(self, circuits, **kwargs):
        raise NotImplementedError("This backend cannot execute circuits.")