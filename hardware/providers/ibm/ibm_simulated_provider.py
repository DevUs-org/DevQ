'''
Tags: Main, Adapter

IBMSimulatedProvider — IBM simulated hardware provider.

Uses Qiskit IBM Runtime's V2 fake backends (FakeSherbrooke, FakeTorino,
etc.) which carry real IBM device calibration data — actual gate error
rates and readout errors from real hardware. Executes circuits via
AerSimulator with a noise model built from that calibration data,
producing statistically realistic results without requiring real
hardware access or an IBM account.

All error data is extracted from the backend's Target object —
the correct API for Qiskit 2.x V2 backends. properties() is not used.

Native 2-qubit gate on IBM V2 backends is ECR (not CX).
Edge errors are extracted via target['ecr'][edge].error.
Readout errors are extracted via target['measure'][(q,)].error.

Available backends (examples):
    FakeSherbrooke  —  127 qubits
    FakeTorino      —   133 qubits
    FakeKyiv        —   127 qubits
    FakeOsaka       —   127 qubits
    FakeBrisbane    —   127 qubits
    FakeFez         —   156 qubits
    FakeNairobiV2   —     7 qubits
    FakeMumbaiV2    —    27 qubits

Usage:
    from hardware.providers.ibm.ibm_simulated_provider import IBMSimulatedProvider
    from hardware.device_loader import load_device

    device = load_device(IBMSimulatedProvider().get_device("FakeSherbrooke"))
    kernel = Kernel(device)
    shell  = QShell(kernel)
'''

from hardware.providers.base_provider import BaseProvider
from hardware.device import QuantumDevice


class IBMSimulatedProvider(BaseProvider):

    def get_device(self, backend_name="FakeSherbrooke") -> QuantumDevice:
        '''
        Build a QuantumDevice from a Qiskit IBM Runtime V2 fake backend.

        Real IBM calibration data is pulled from the backend's Target:
          - Coupling map (real device topology, deduplicated to undirected edges)
          - ECR gate error rates per edge (native IBM 2-qubit gate)
          - Readout error rates per qubit

        Args:
            backend_name: name of the V2 fake backend, e.g. "FakeSherbrooke"

        Returns:
            QuantumDevice with real IBM calibration data and self as provider
        '''
        try:
            from qiskit_ibm_runtime.fake_provider import FakeProviderForBackendV2
            from qiskit_aer.noise import NoiseModel
        except ImportError:
            raise ImportError(
                "qiskit-ibm-runtime and qiskit-aer are required for IBMSimulatedProvider.\n"
                "Install with: pip install qiskit-ibm-runtime qiskit-aer"
            )

        backend = self._load_backend(backend_name)

        # Store backend for execute() — noise model built once here
        self._backend      = backend
        self._backend_name = backend_name
        self._noise_model  = NoiseModel.from_backend(backend)

        num_qubits   = backend.num_qubits
        coupling_map = self._extract_coupling_map(backend)
        basis_gates  = list(backend.operation_names)
        error_map    = self._extract_qubit_errors(backend, num_qubits)
        edge_error_map = self._extract_edge_errors(backend, coupling_map)

        return QuantumDevice(
            name           = backend_name.lower(),
            num_qubits     = num_qubits,
            coupling_map   = coupling_map,
            basis_gates    = basis_gates,
            error_map      = error_map,
            edge_error_map = edge_error_map,
            provider       = self
        )

    def execute(self, circuit, v2p_map, shots):
        '''
        Execute a CircuitRep on AerSimulator with IBM device noise model.

        Builds a Qiskit QuantumCircuit on virtual qubit indices (0..n-1)
        and runs it on AerSimulator with the noise model built from the
        fake backend's real calibration data. Results are statistically
        representative of real IBM hardware behaviour.

        Args:
            circuit : CircuitRep — the circuit to execute
            v2p_map : dict — virtual to physical qubit mapping
            shots : number of shots

        Returns:
            ExecutionFuture wrapping an ExecutionResult
        '''
        try:
            from qiskit_aer import AerSimulator
            from qiskit import QuantumCircuit, transpile
        except ImportError:
            from circuits.execution_result import ExecutionResult, ExecutionFuture
            return ExecutionFuture(ExecutionResult(
                counts  = {},
                success = False,
                error   = "qiskit-aer is not installed. Run: pip install qiskit-aer"
            ))

        from circuits.execution_result import ExecutionResult, ExecutionFuture

        try:
            num_virtual = circuit.num_qubits
            qc = QuantumCircuit(num_virtual, num_virtual)

            for inst in circuit.instructions:
                self._add_gate(qc, inst['gate'].lower(),
                            inst['qubits'], inst.get('params', []))

            qc.measure(range(num_virtual), range(num_virtual))

            sim    = AerSimulator(noise_model=self._noise_model)
            t_circ = transpile(qc, sim)
            counts = sim.run(t_circ, shots=shots).result().get_counts()

            return ExecutionFuture(ExecutionResult(
                counts  = counts,
                success = True
            ))

        except Exception as e:
            return ExecutionFuture(ExecutionResult(
                counts  = {},
                success = False,
                error   = str(e)
            ))
        
    def preferred_config(self) -> dict:
        '''
        IBM simulated backends benefit from more shots for statistical
        accuracy given the noise model applied during execution.
        '''
        return {"shots": 2048}

    # ── Private helpers ───────────────────────────────────────────────────────

    def _load_backend(self, backend_name):
        '''Dynamically load a V2 fake backend by name.'''
        try:
            import qiskit_ibm_runtime.fake_provider as fp
            backend_class = getattr(fp, backend_name)
            return backend_class()
        except AttributeError:
            raise ValueError(
                f"Unknown fake backend '{backend_name}'.\n"
                f"Run: python3 -c \"import qiskit_ibm_runtime.fake_provider as fp; "
                f"print([x for x in dir(fp) if 'Fake' in x])\"\n"
                f"to see all available backends."
            )

    def _extract_coupling_map(self, backend) -> list:
        '''
        Extract undirected coupling map from the backend.

        V2 backends expose a directed CouplingMap. We deduplicate to
        undirected edges using sorted tuples — consistent with how
        QuantumDevice normalises edge_error_map keys.
        '''
        seen = set()
        edges = []
        for (u, v) in backend.coupling_map:
            key = tuple(sorted((u, v)))
            if key not in seen:
                seen.add(key)
                edges.append(key)
        return edges

    def _extract_qubit_errors(self, backend, num_qubits) -> dict:
        '''
        Extract per-qubit readout error rates from Target.

        Uses target['measure'][(q,)].error — the correct V2 API.
        Falls back to 0.01 if a qubit's data is unavailable.
        '''
        target    = backend.target
        error_map = {}
        for q in range(num_qubits):
            try:
                error_map[q] = target['measure'][(q,)].error
            except Exception:
                error_map[q] = 0.01
        return error_map

    def _extract_edge_errors(self, backend, coupling_map) -> dict:
        '''
        Extract per-edge ECR gate error rates from Target.

        IBM V2 backends use ECR as the native 2-qubit gate.
        Uses target['ecr'][edge].error — the correct V2 API.
        Falls back to 0.02 if an edge's data is unavailable.

        Note: edges are tried in both directions since the backend
        coupling map is directed but our coupling_map is undirected.
        '''
        target        = backend.target
        edge_error_map = {}
        for (u, v) in coupling_map:
            key = tuple(sorted((u, v)))
            err = None
            for edge in [(u, v), (v, u)]:
                try:
                    err = target['ecr'][edge].error
                    break
                except Exception:
                    continue
            edge_error_map[key] = err if err is not None else 0.02
        return edge_error_map

    def _add_gate(self, qc, gate, qubits, params):
        '''Map CircuitRep gate names to Qiskit QuantumCircuit methods.'''
        gate_map = {
            'h':    lambda: qc.h(qubits[0]),
            'x':    lambda: qc.x(qubits[0]),
            'y':    lambda: qc.y(qubits[0]),
            'z':    lambda: qc.z(qubits[0]),
            's':    lambda: qc.s(qubits[0]),
            't':    lambda: qc.t(qubits[0]),
            'sx':   lambda: qc.sx(qubits[0]),
            'cx':   lambda: qc.cx(qubits[0], qubits[1]),
            'cz':   lambda: qc.cz(qubits[0], qubits[1]),
            'ecr':  lambda: qc.ecr(qubits[0], qubits[1]),
            'swap': lambda: qc.swap(qubits[0], qubits[1]),
            'rz':   lambda: qc.rz(params[0], qubits[0]),
            'rx':   lambda: qc.rx(params[0], qubits[0]),
            'ry':   lambda: qc.ry(params[0], qubits[0]),
            'ccx':  lambda: qc.ccx(qubits[0], qubits[1], qubits[2]),
        }
        action = gate_map.get(gate)
        if action:
            action()
        else:
            print(f"[IBMSimulatedProvider] Warning: unknown gate '{gate}', skipping.")