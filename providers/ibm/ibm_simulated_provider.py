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
    from devq import DevQ
    from providers.ibm.ibm_simulated_provider import IBMSimulatedProvider

    ibm = IBMSimulatedProvider(seed=42)
    DevQ() \
        .register_provider("ibm.simulated", IBMSimulatedProvider) \
        .add_device(ibm.get_device("FakeSherbrooke")) \
        .start()
'''

from providers.ibm.ibm_provider import IBMProvider
from providers.ibm.qiskit_lowering import build_qiskit_circuit
from hardware.device import QuantumDevice


class IBMSimulatedProvider(IBMProvider):

    # Human-readable name shown by qconfig. Any registered component
    # may define one; the registry falls back to the class name.
    LABEL = "IBM Simulated Provider"

    # Below this magnitude, a density-matrix probability is floating-point
    # dust around a true zero (observed ~1e-17, sometimes negative from
    # cancellation), not real mass. _marginalise drops anything under it so
    # the reference ideal is a clean, non-negative distribution. Nine
    # orders above the observed residue, far below any meaningful
    # probability. See _marginalise for the full reasoning.
    _DUST = 1e-12

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
        # Per-device execution state, keyed by DEVICE INDEX — one
        # provider instance may serve multiple devices (Bug A fix), and
        # several of them may be the SAME KIND. Keying by kind collapses
        # them onto one shared session; keying by index cannot.
        # Populated in on_attach(), which is the first moment an index
        # exists — get_device() runs before the kernel assigns one.
        self._sessions = {}
        # Loaded Qiskit backends, keyed by kind. Backends are immutable
        # and expensive to load, so same-kind devices share one; the
        # mutable per-device state lives in _sessions, not here.
        self._backends = {}
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

        backend = self._backends.get(backend_name)
        if backend is None:
            backend = self._load_backend(backend_name)
            self._backends[backend_name] = backend

        num_qubits   = backend.num_qubits
        coupling_map = self._extract_coupling_map(backend)
        basis_gates  = list(backend.operation_names)
        error_map    = self._extract_qubit_errors(backend, num_qubits)
        edge_error_map = self._extract_edge_errors(backend, coupling_map)
        gate_error_map = self._extract_gate_errors(backend, num_qubits)
        t2_map         = self._extract_t2_times(backend, num_qubits)
        gate_1q_dur, gate_2q_dur = self._extract_gate_durations(backend)

        self._devices_created += 1
        return QuantumDevice(
            kind           = backend_name,
            num_qubits     = num_qubits,
            coupling_map   = coupling_map,
            basis_gates    = basis_gates,
            error_map      = error_map,
            edge_error_map = edge_error_map,
            gate_error_map = gate_error_map,
            t2_map         = t2_map,
            gate_1q_duration = gate_1q_dur,
            gate_2q_duration = gate_2q_dur,
            provider       = self
        )

    def on_attach(self, device):
        '''
        Build this device's execution session, keyed by its freshly
        assigned index. The Qiskit backend is shared with any same-kind
        device; the noise model is built per device so two devices of
        the same kind never share noise state.
        '''
        try:
            from qiskit_aer.noise import NoiseModel
        except ImportError:
            return

        backend = self._backends.get(device.kind)
        if backend is None:
            return

        self._sessions[device.index] = {
            "backend"    : backend,
            "noise_model": NoiseModel.from_backend(backend),
        }

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
            from qiskit import transpile
        except ImportError:
            from circuits.execution_result import ExecutionResult, ExecutionFuture
            return ExecutionFuture(ExecutionResult(
                counts  = {},
                success = False,
                error   = "qiskit-aer is not installed. Run: pip install qiskit-aer"
            ))

        from circuits.execution_result import ExecutionResult, submit_async

        session = self._sessions.get(device.index)
        if session is None:
            return submit_async(lambda: ExecutionResult(
                counts  = {},
                success = False,
                error   = (
                    f"No session for device {device.ref} "
                    f"(kind={device.display_kind}) on this provider "
                    f"instance. Devices must be created via get_device() "
                    f"on the same provider that executes them, and "
                    f"attached to a kernel before execution."
                )
            ))
        noise_model = session["noise_model"]
        # The fake backend carries the device's full physical width and
        # coupling map. The noiseless-model AerSimulator alone does not,
        # so the full-device-width layout is validated by transpiling
        # against this backend; the noisy run still happens on the
        # noise-model simulator below.
        backend = session["backend"]

        # Derived per-run seed — incremented on the shell thread (all
        # dispatch happens there, so submission order is deterministic
        # and derived seeds reproduce across identical sessions).
        run_seed = None
        if self.seed is not None:
            self._submission_count += 1
            run_seed = self.seed + self._submission_count

        def _run():
            try:
                # Classical-register width (Option B): the DECLARED creg
                # width, so a measured bit sits at its own index and the
                # bitstring position IS the classical-bit index — no side
                # map, matching how real hardware reports a creg. Unmeasured
                # bits stay 0. Falls back to the qubit count when no creg
                # is declared. Computed by the shared BaseProvider helper
                # so the rule stays identical across providers.
                num_clbits = self._counts_width(circuit)

                # Lower via the shared builder so this measured run and the
                # noiseless reference (reference_ideal) walk the circuit
                # through identical code — a gate, reset-ordering, or width
                # difference between them would break a fidelity comparison
                # silently. The builder returns the gate/reset body plus the
                # RESOLVED measure map (explicit measures, or the
                # measure-all fallback); execute bakes those measures in to
                # sample the classical register.
                qc, measure_map = build_qiskit_circuit(circuit, num_clbits)
                for q, c in measure_map:
                    qc.measure(q, c)

                # The allocator's physical placement, as a FULL-device-width
                # layout: virtual qubit v runs on physical v2p_map[v], and
                # every unused physical qubit is filled with an ancilla. A
                # partial layout (only the mapped qubits) is rejected by Aer
                # on a large device — "must be full (with ancilla)". Built by
                # the shared BaseProvider helper so both IBM providers pad
                # identically. Mutates qc (adds an ancilla register); the
                # classical width is untouched, so counts width is unchanged.
                initial_layout = self.full_layout(qc, v2p_map, device)

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
                # Transpile against the fake BACKEND, not the bare noise-model
                # simulator: the backend carries the device's full width and
                # coupling map, which is what makes the full (with-ancilla)
                # layout validate. optimization_level=0 executes the
                # allocator's placement without re-optimising it. The noisy
                # sampling itself still runs on the noise-model simulator.
                t_circ = transpile(qc, backend,
                                   initial_layout = initial_layout,
                                   optimization_level = 0,
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

    # ── calibration extraction ────────────────────────────────────────────────
    # The five-term calibration surface (coupling map, readout error, 2q-edge
    # error, 1q-gate error, T2, gate durations) is read from the backend Target
    # by IBMProvider, shared with IBMRealProvider — the extraction is identical
    # for a fake and a real Target. See providers/ibm/ibm_provider.py.

    # ── reference ideal (the noiseless yardstick fidelity compares to) ──

    def reference_ideal(self, circuit):
        '''
        The IDEAL measured-bit distribution for a circuit — what a perfect,
        noiseless machine would produce — as {bitstring: probability} at
        Option-B classical width.

        This is the noiseless yardstick the fidelity metric compares a
        real noisy run against. It is a property of the CIRCUIT, not of any
        device: the same circuit has one ideal regardless of which backend
        ran it, which is exactly what makes a cross-device fidelity
        comparison meaningful. A capable provider offers this so the
        shipped, vendor-neutral reference orchestrator can obtain ideals
        without DevQ core depending on any provider — the default on
        BaseProvider declines (returns None), and this override supplies
        the Qiskit-backed answer.

        HOW THE IDEAL IS COMPUTED. The circuit's gate/reset body is lowered
        by the SAME shared builder execute() uses — so the ideal and the
        measured run cannot lower differently — and run on a NOISELESS Aer
        density-matrix simulation. We read EXACT probabilities
        (save_probabilities), not sampled counts: an exact ideal has no
        sampling noise and no reference seed to pin, so metrics.json stays
        byte-reproducible. Density-matrix (not statevector) is used because
        it honours mid-circuit reset, a non-unitary op statevector
        evolution cannot represent.

        MARGINALISATION. Aer returns probabilities over ALL qubit basis
        states, little-endian in qubit index. Fidelity compares
        classical-register bitstrings, so we fold those qubit
        probabilities down onto the classical register using the SAME
        resolved measure map the measured run used (`measure q -> c[j]`
        places qubit q's outcome at clbit j). A qubit measured onto clbit j
        contributes its value to position j of the classical string;
        unmeasured classical bits stay 0. This is the step where a naive
        "just use qubit order" implementation goes silently wrong on a
        circuit whose qubit and clbit indices differ, so the map is
        authoritative, not the qubit order.

        Args:
            circuit : CircuitRep

        Returns:
            dict {bitstring: probability} summing to 1 over the classical
            register, or None if the Qiskit/Aer path is unavailable (the
            same honest degrade the execute path uses when qiskit-aer is
            missing).
        '''
        try:
            from qiskit_aer import AerSimulator
        except ImportError:
            return None

        from providers.ibm.qiskit_lowering import (
            build_qiskit_circuit, resolve_measure_map, UnknownGateError)

        width = self._counts_width(circuit)

        # Same lowering as execute(): gate/reset body, no measures baked
        # in. The reference reads probabilities off the unmeasured state
        # and marginalises, so it must NOT measure into the circuit.
        #
        # A gate the lowering cannot express means this provider cannot
        # produce an ideal for this circuit, which is a None — the same
        # honest degrade as a missing Aer, and exactly what
        # benchmark/reference.compute_ideals() documents it may receive
        # ("cannot simulate it — a gate it does not know"). It must NOT
        # propagate: compute_ideals() has no handler, so a raise here would
        # abort a whole multi-circuit run over one unlowerable circuit
        # instead of costing that circuit its fidelity number. The
        # corresponding job still FAILS visibly via execute(), so the
        # condition is never silent.
        try:
            qc, measure_map = build_qiskit_circuit(circuit, width)
        except UnknownGateError:
            return None
        qc.save_probabilities()

        sim = AerSimulator(
            method                   = "density_matrix",
            max_parallel_threads     = 1,
            max_parallel_experiments = 1,
            max_parallel_shots       = 1,
        )
        result = sim.run(qc, shots=1).result()
        probs = result.data(0)["probabilities"]

        return self._marginalise(probs, measure_map, width,
                                  circuit.num_qubits)

    @staticmethod
    def _marginalise(probs, measure_map, width, num_qubits):
        '''
        Fold full-qubit probabilities onto the classical register.

        `probs` is indexed by the integer whose bit q (little-endian) is
        qubit q's value. For each basis index with non-zero probability,
        read each measured qubit's bit and place it at its clbit position
        to form the classical bitstring, then accumulate the probability
        under that string. Unmeasured classical bits stay 0.

        Kept as pure arithmetic (no qiskit) and separate from the sim call
        so a test can hand it a synthetic probability vector and a map and
        assert exact numbers — the marginalisation is where a wrong index
        mapping hides, so it is the piece worth isolating and pinning.

        Bitstrings follow the Qiskit convention: position 0 of the STRING
        is the highest clbit index (c[width-1] … c[0]), so a measured bit
        at clbit j sits `width-1-j` characters from the left — matching how
        get_counts() renders the same register, so measured and ideal keys
        align character-for-character.

        DUST CLAMP. A density-matrix probability is never exactly 0.0 — the
        simulator returns tiny residues (±1e-17-ish) around true zeros,
        including small NEGATIVE values from floating-point cancellation. A
        `p == 0` skip lets those through, and a negative probability is not
        merely ugly: it is a malformed distribution. It breaks any consumer
        that takes sqrt (Hellinger fidelity raises "math domain error", or
        yields a complex value from a hand-rolled sqrt) and it would sit in
        the recorded reference ideal for every downstream reader — 5.5's
        comparison modes, any future metric — not just this one. Bell and
        GHZ never exposed it because their exact rational amplitudes land
        on clean zeros; QASMBench circuits do not. So we clamp at the
        SOURCE: any |p| below `_DUST` is treated as zero and dropped, the
        same outcome the old `p == 0` meant to produce. The threshold is
        nine orders of magnitude above the observed residue and far below
        any physically meaningful probability, so it removes numerical dust
        without touching real mass. No renormalisation: the surviving
        probabilities already sum to 1.0 within 1e-15, and dividing would
        add a second numerical operation to reason about for no gain.
        '''
        out = {}
        for index, p in enumerate(probs):
            if abs(p) < IBMSimulatedProvider._DUST:
                continue
            bits = ["0"] * width
            for qubit, clbit in measure_map:
                qubit_val = (index >> qubit) & 1
                # Qiskit renders clbit j at string position width-1-j.
                bits[width - 1 - clbit] = str(qubit_val)
            key = "".join(bits)
            out[key] = out.get(key, 0.0) + float(p)
        return out