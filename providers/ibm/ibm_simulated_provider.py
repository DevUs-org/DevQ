'''
Tags: Provider

IBMSimulatedProvider — IBM simulated hardware provider.

Uses Qiskit IBM Runtime's V2 fake backends (FakeSherbrooke, FakeTorino,
etc.) which carry real IBM device calibration data — actual gate error
rates and readout errors from real hardware. Executes circuits via
AerSimulator with a noise model built from that calibration data,
producing statistically realistic results without requiring real
hardware access or an IBM account.

All error data is extracted from the backend's Target object —
the correct API for Qiskit 2.x V2 backends. properties() is not used.

The native 2-qubit gate varies by backend generation (ECR on
Eagle/Heron, CX on older Falcon devices, CZ on some Heron revisions).
Edge errors are extracted by discovering 2-qubit gates from the Target.
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

from providers.base_provider import BaseProvider
from hardware.device import QuantumDevice


class IBMSimulatedProvider(BaseProvider):

    def __init__(self, seed=None):
        '''
        Args:
            seed : int or None — base seed for reproducible execution.
                   Each run derives seed + k (k = provider-local
                   submission counter) and passes it to both the
                   transpiler and the Aer simulator, so identical
                   sessions reproduce counts job-for-job while distinct
                   runs of identical circuits stay distinct.
                   None (default) preserves unseeded behaviour.
        '''
        super().__init__(seed)
        # Per-device execution state, keyed by device name — one
        # provider instance may serve multiple devices (Bug A fix).
        self._sessions = {}
        self._submission_count = 0

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

        # Per-device session for execute() — noise model built once
        # here, keyed by device name so multiple devices served by this
        # instance never share noise state.
        self._sessions[backend_name.lower()] = {
            "backend"    : backend,
            "noise_model": NoiseModel.from_backend(backend),
        }

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

    def execute(self, circuit, v2p_map, shots, device):
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
            device : QuantumDevice — selects this device's session
                     (backend + noise model)

        Returns:
            AsyncExecutionFuture resolving to an ExecutionResult
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

        from circuits.execution_result import ExecutionResult, submit_async

        session = self._sessions.get(device.name)
        if session is None:
            return submit_async(lambda: ExecutionResult(
                counts  = {},
                success = False,
                error   = (
                    f"No session for device '{device.name}' on this "
                    f"provider instance. Devices must be created via "
                    f"get_device() on the same provider that executes them."
                )
            ))
        noise_model = session["noise_model"]

        # The allocator's physical placement (Bug B fix): virtual qubit
        # v runs on physical qubit v2p_map[v] of the noise model.
        initial_layout = [v2p_map[v] for v in sorted(v2p_map)]

        # Derived per-run seed — incremented on the shell thread (all
        # dispatch happens there, so submission order is deterministic
        # and derived seeds reproduce across identical sessions).
        run_seed = None
        if self.seed is not None:
            self._submission_count += 1
            run_seed = self.seed + self._submission_count

        def _run():
            try:
                num_virtual = circuit.num_qubits
                qc = QuantumCircuit(num_virtual, num_virtual)

                for inst in circuit.instructions:
                    self._add_gate(qc, inst['gate'].lower(),
                                inst['qubits'], inst.get('params', []))

                qc.measure(range(num_virtual), range(num_virtual))

                # Pin Aer's internal parallelism. Left unset, Aer sizes
                # its thread pool from the CPU count and each thread
                # allocates its own simulation buffers — multiplied by
                # the shared executor's workers and by every session
                # alive in the process, memory grows with cores rather
                # than with work. These jobs are small; one thread each
                # is both sufficient and predictable across machines.
                sim = AerSimulator(
                    noise_model              = noise_model,
                    max_parallel_threads     = 1,
                    max_parallel_experiments = 1,
                    max_parallel_shots       = 1,
                )
                t_circ = transpile(qc, sim,
                                   initial_layout = initial_layout,
                                   seed_transpiler = run_seed)
                counts = sim.run(t_circ, shots=shots,
                                 seed_simulator=run_seed).result().get_counts()

                return ExecutionResult(counts=counts, success=True)

            except Exception as e:
                return ExecutionResult(counts={}, success=False, error=str(e))

        # Phase 4: genuinely asynchronous — the returned future resolves
        # on a worker thread while the kernel keeps scheduling/routing.
        return submit_async(_run)

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
        Extract per-edge 2-qubit gate error rates from Target.

        The native 2-qubit gate differs across IBM device generations:
        ECR on Eagle/Heron backends (Sherbrooke, Torino, ...), CX on
        older Falcon backends (NairobiV2, MumbaiV2, ...), CZ on some
        Heron revisions. Rather than hardcoding a gate name, the gate
        set is discovered from the Target: every operation acting on
        exactly 2 qubits is a candidate, and each edge takes the error
        of the first candidate gate defined on it.

        Falls back to 0.02 for an edge only if no 2-qubit gate reports
        an error for it — and warns, so bad calibration data is never
        silently fabricated.

        Note: edges are tried in both directions since the backend
        coupling map is directed but our coupling_map is undirected.
        '''
        target = backend.target

        # Discover the backend's native 2-qubit gate names
        twoq_gates = [
            name for name in target.operation_names
            if self._op_num_qubits(target, name) == 2
        ]

        if not twoq_gates:
            print(f"[IBMSimulatedProvider] Warning: no 2-qubit gates found "
                  f"in Target — edge errors will use fallback 0.02.")

        edge_error_map = {}
        for (u, v) in coupling_map:
            key = tuple(sorted((u, v)))
            err = None

            for gate in twoq_gates:
                for edge in [(u, v), (v, u)]:
                    try:
                        candidate = target[gate][edge].error
                        if candidate is not None:
                            err = candidate
                            break
                    except Exception:
                        continue
                if err is not None:
                    break

            if err is None:
                print(f"[IBMSimulatedProvider] Warning: no 2-qubit gate "
                      f"error for edge {key}, using fallback 0.02.")
                err = 0.02

            edge_error_map[key] = err

        return edge_error_map

    @staticmethod
    def _op_num_qubits(target, name):
        '''Number of qubits an operation acts on, or None if unknown.'''
        try:
            return target.operation_from_name(name).num_qubits
        except Exception:
            return None

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