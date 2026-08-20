'''
Tags: Provider

IBMRealProvider — executes on REAL IBM quantum hardware via IBM Runtime.

This is the real-hardware counterpart to IBMSimulatedProvider. Where the
simulated provider builds a device from a *fake* backend's calibration and
runs circuits on a noisy Aer simulation, this one talks to a *real* backend
through QiskitRuntimeService: it pulls that backend's live calibration into
a DevQ QuantumDevice, and executes submitted circuits on the physical QPU
via SamplerV2.

WHERE THIS LIVES. It sits under providers/ibm/ beside IBMSimulatedProvider
and their shared IBMProvider base, because it is a Qiskit-family provider and
belongs next to the base it inherits. It remains a RESEARCH instrument, not a
shipped provider: it is opt-in, account-gated, and costs real QPU time, so it
is not registered in the default provider set and is exempt from the core
mutation discipline and the core test suite (which never touches a real
account). It makes ZERO core changes: it implements the same BaseProvider
contract every provider does, and the kernel, router, allocator and metrics
treat it identically to any other provider. The full proof-run harness stays
in research/ (research/run_real_hardware.py).

WHAT IT SHARES WITH THE SIMULATED PROVIDER. Real and fake backends expose the
SAME BackendV2 / Target API — that is the entire point of Qiskit's fake
backends, they are snapshots of real ones. So reading DevQ's calibration
surface out of the Target (coupling map, readout/gate/edge errors, T2,
durations) is identical work either way, and both providers inherit it from
IBMProvider rather than each carrying its own copy. This one and the
simulated provider previously duplicated those extractors line-for-line; the
shared base removes the copy so the two cannot drift apart.

THREE DELIBERATE DEVIATIONS from the simulated provider:

  1. execute() submits to REAL hardware (SamplerV2), not Aer. It honours
     the allocator's placement literally — initial_layout from v2p_map,
     optimization_level=0 — because the whole premise of DevQ is that the
     POLICY makes the placement decision and the provider executes it
     faithfully, rather than letting Qiskit's transpiler re-optimise the
     decision away. (opt_level=0 still inserts SWAPs when the allocator's
     chosen physical qubits are not adjacent for a 2-qubit gate; that is the
     allocator's consequence to own, not the provider's to hide.)

  2. reference_ideal() returns None (inherits the base default). A real QPU
     cannot produce a noiseless ideal — asking a physical machine for the
     perfect distribution is meaningless. Fidelity for a real-hardware job
     is therefore computed against an ideal from a SEPARATE capable provider
     (the simulated one), exactly the vendor-neutral inversion the base
     contract describes: the ideal is a property of the circuit, not the
     device.

  3. seed has NO effect on execution. You cannot reseed a quantum computer.
     The parameter is accepted for interface-compatibility with the provider
     contract and ignored at execution; it is not used to derive any state.

CREDENTIALS come through the workload-spec ${} placeholder mechanism, the
same one built in Phase 5.2 that resolves ${IBM_QUANTUM_TOKEN} at load and
returns (resolved, verbatim) so the token never lands in a logged manifest.
The provider receives an already-resolved token like any other config value;
nothing secret is written in code or in the repo. The token may also be left
None to fall back on a Qiskit-saved account (QiskitRuntimeService()).

Usage (see research/run_real_hardware.py for the full proof-run):

    from providers.ibm.ibm_real_provider import IBMRealProvider
    ibm = IBMRealProvider(token=os.environ["IBM_QUANTUM_TOKEN"])
    dev = ibm.get_device("ibm_sherbrooke")
'''

from providers.ibm.ibm_provider import IBMProvider
from hardware.device import QuantumDevice


class IBMRealProvider(IBMProvider):

    # Human-readable name shown by qconfig. The registry falls back to the
    # class name when absent; this matches the simulated provider's LABEL.
    LABEL = "IBM Real Hardware Provider"

    def __init__(self, seed=None, secrets=None):
        '''
        Args:
            seed : int or None — accepted for BaseProvider interface
                   compatibility and IGNORED for execution (a physical QPU
                   cannot be reseeded). Present so this provider can stand
                   wherever a provider is expected without special-casing.
            secrets : dict or None — the resolved spec `secrets` block, which
                   DevQ delivers to any provider whose __init__ names this
                   parameter. DevQ owns the resolution (${ENV_VAR} values are
                   already substituted) and the leak-safety (the block is
                   masked in the logged spec); THIS provider owns the
                   vocabulary. The keys it reads:

                       token    — IBM Quantum API token  (required to run)
                       channel  — IBM Runtime channel     (optional)
                       instance — IBM instance/CRN        (optional)

                   Another provider would name its own keys; DevQ never
                   inspects them. secrets=None (no block in the spec) leaves
                   the token unset, and the service falls back to a
                   Qiskit-saved account.

        The token reaches this constructor through the spec's top-level
        `secrets` block, resolved from the environment by ${...} placeholders
        and kept out of every logged artifact. It is NOT a device-level
        value: one authenticated service serves every backend this provider
        builds, which is why it belongs on the constructor, not get_device.
        '''
        super().__init__(seed)

        secrets = secrets or {}
        self._token    = secrets.get("token")
        self._channel  = secrets.get("channel") or "ibm_quantum_platform"
        self._instance = secrets.get("instance")

        # The QiskitRuntimeService is created LAZILY, on first backend load,
        # so merely constructing or importing this provider costs no network
        # call and no credential check — matching how the simulated provider
        # imports qiskit lazily. Building a session should never force an
        # account handshake.
        self._service = None

        # Loaded real backends, keyed by backend name. Backends are handles
        # onto a remote device and are shared across same-kind devices; the
        # per-device kernel state lives in _sessions, keyed by device index.
        self._backends = {}
        self._sessions = {}

    # ── service / backend loading ─────────────────────────────────────────

    def _get_service(self):
        '''
        Lazily construct and cache the QiskitRuntimeService. Raises a clear
        ImportError if the runtime package is absent, and lets any
        authentication error surface as-is (a bad token is the caller's to
        fix, and its message is the useful one).
        '''
        if self._service is not None:
            return self._service
        try:
            from qiskit_ibm_runtime import QiskitRuntimeService
        except ImportError:
            raise ImportError(
                "qiskit-ibm-runtime is required for IBMRealProvider.\n"
                "Install with: pip install qiskit-ibm-runtime"
            )
        kwargs = {"channel": self._channel}
        if self._token is not None:
            kwargs["token"] = self._token
        if self._instance is not None:
            kwargs["instance"] = self._instance
        self._service = QiskitRuntimeService(**kwargs)
        return self._service

    def _load_backend(self, backend_name):
        '''Fetch a real backend handle by name through the service.'''
        service = self._get_service()
        try:
            return service.backend(backend_name)
        except Exception as e:
            raise ValueError(
                f"Could not load real backend '{backend_name}': {e}\n"
                f"List available backends with:\n"
                f"  QiskitRuntimeService(...).backends()"
            )

    def least_busy(self, min_qubits=None):
        '''
        Convenience: the name of the least-busy operational real backend,
        for a run script that wants to pick a device by queue rather than by
        name. Not part of the provider contract — a helper for the proof-run.
        '''
        service = self._get_service()
        kwargs = {"operational": True, "simulator": False}
        if min_qubits is not None:
            kwargs["min_num_qubits"] = min_qubits
        return service.least_busy(**kwargs).name

    def pending_jobs(self, backend_name):
        '''
        The real backend's GLOBAL pending-job count — the world queue shared
        with every other IBM user, distinct from DevQ's local per-device
        queue. NOT read by the shipped NoiseRouter (which scores DevQ's own
        queue_depth()+running_jobs); exposed here only so a run script can
        report real congestion alongside the routing decision. Wiring this
        into the router would change the router's queue semantics and is a
        deliberate future design question, not done here.
        '''
        backend = self._backends.get(backend_name) or self._load_backend(backend_name)
        self._backends[backend_name] = backend
        try:
            return backend.status().pending_jobs
        except Exception:
            return None

    # ── device construction ───────────────────────────────────────────────

    def get_device(self, backend_name) -> QuantumDevice:
        '''
        Build a QuantumDevice from a REAL IBM backend's live calibration.

        Pulls the same fields the simulated provider pulls from a fake
        backend's Target — coupling map, readout errors, per-qubit 1q gate
        errors, per-edge 2q errors, T2 times, gate durations — but from a
        real device's current calibration snapshot. Credentials are not
        arguments here: they authenticate the whole provider and arrive via
        the constructor's `secrets` (see __init__).

        Args:
            backend_name : real backend name, e.g. "ibm_sherbrooke".

        Returns:
            QuantumDevice carrying real calibration, with self as provider.
        '''
        backend = self._backends.get(backend_name)
        if backend is None:
            backend = self._load_backend(backend_name)
            self._backends[backend_name] = backend

        num_qubits     = backend.num_qubits
        coupling_map   = self._extract_coupling_map(backend)
        basis_gates    = list(backend.operation_names)
        error_map      = self._extract_qubit_errors(backend, num_qubits)
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
        Record this device's real backend handle, keyed by its freshly
        assigned index. Unlike the simulated provider there is no noise
        model to build — the noise is the real machine — so the session
        holds only the backend handle used at execute() time.
        '''
        backend = self._backends.get(device.kind)
        if backend is None:
            return
        self._sessions[device.index] = {"backend": backend}

    # ── execution ─────────────────────────────────────────────────────────

    def execute(self, circuit, v2p_map, shots, device):
        '''
        Execute a CircuitRep on a REAL IBM backend via SamplerV2.

        The circuit is lowered by the SAME shared builder the simulated
        provider uses, so a real run and a simulated run (and the noiseless
        reference the fidelity metric compares against) walk the circuit
        through identical code — a gate or width difference between them
        would corrupt a fidelity comparison silently.

        PLACEMENT IS HONOURED LITERALLY. initial_layout is taken from the
        allocator's v2p_map and optimization_level is pinned to 0, so the
        transpiler does only what the real gate set and topology require and
        does not re-route the allocator's decision. See the module docstring.

        COUNTS. SamplerV2 returns per-classical-register BitArrays. The
        shared lowering builds a circuit with the default classical register
        named "c" (Qiskit's name for an int-sized creg), so counts come from
        result[0].data.c.get_counts(). Those bitstrings already follow the
        Option-B width and index conventions the OUTPUT CONTRACT requires,
        because the shared builder created the register that way.

        Returns:
            AsyncExecutionFuture resolving to an ExecutionResult. The worker
            thread BLOCKS on the real IBM queue, which may take minutes to
            hours on a free plan — the kernel keeps scheduling meanwhile, but
            this job's future will not resolve until the QPU runs it.
        '''
        try:
            from qiskit import transpile
            from qiskit_ibm_runtime import SamplerV2
        except ImportError:
            from circuits.execution_result import ExecutionResult, submit_async
            return submit_async(lambda: ExecutionResult(
                counts  = {},
                success = False,
                error   = ("qiskit-ibm-runtime is not installed. "
                           "Run: pip install qiskit-ibm-runtime")
            ))

        from circuits.execution_result import ExecutionResult, submit_async
        from providers.ibm.qiskit_lowering import build_qiskit_circuit, UnknownGateError

        session = self._sessions.get(device.index)
        if session is None:
            return submit_async(lambda: ExecutionResult(
                counts  = {},
                success = False,
                error   = (
                    f"No session for device {device.ref} "
                    f"(kind={device.display_kind}) on this provider "
                    f"instance. Devices must be created via get_device() on "
                    f"the same provider that executes them, and attached to "
                    f"a kernel before execution."
                )
            ))
        backend = session["backend"]

        # Option-B classical width, via the shared BaseProvider helper so the
        # rule stays identical across providers.
        num_clbits = self._counts_width(circuit)

        # Lower the gate/reset body and bake in the resolved measures — the
        # real run samples the classical register, exactly as execute() does
        # on the simulated side. A gate the lowering cannot express is a
        # visible failure (FAILED job naming the gate), never a silent drop.
        try:
            qc, measure_map = build_qiskit_circuit(circuit, num_clbits)
        except UnknownGateError as e:
            return submit_async(lambda e=e: ExecutionResult(
                counts={}, success=False, error=str(e)))
        for q, c in measure_map:
            qc.measure(q, c)

        # The allocator's physical placement, as a FULL-device-width layout:
        # virtual qubit v runs on physical v2p_map[v], every unused physical
        # qubit filled with an ancilla. Same construction as the simulated
        # provider (the shared BaseProvider helper), so the two agree on what
        # "placement" means and neither hand-rolls the padding. A partial
        # layout (only the mapped qubits) is what real transpile tolerated by
        # silently padding; building the full layout explicitly makes the
        # placement identical to the simulated path rather than relying on
        # that leniency. Mutates qc (adds an ancilla register); classical
        # width is untouched.
        initial_layout = self.full_layout(qc, v2p_map, device)

        def _run():
            try:
                # opt_level=0 + initial_layout: execute the allocator's
                # placement, do not re-optimise it. transpile to the real
                # backend's native basis and coupling.
                t_circ = transpile(qc, backend,
                                   initial_layout      = initial_layout,
                                   optimization_level  = 0)

                sampler = SamplerV2(mode=backend)
                job = sampler.run([t_circ], shots=shots)
                result = job.result()

                # SamplerV2: one PubResult per submitted circuit; its .data
                # is a DataBin whose fields are the circuit's classical
                # registers. The shared lowering's register is named "c".
                data = result[0].data
                counts = self._counts_from_databin(data)

                return ExecutionResult(counts=counts, success=True)

            except Exception as e:
                return ExecutionResult(counts={}, success=False, error=str(e))

        return submit_async(_run)

    @staticmethod
    def _counts_from_databin(data):
        '''
        Pull the counts dict out of a SamplerV2 DataBin.

        The DataBin carries one BitArray per classical register. The shared
        lowering creates a single register named "c", so that is the normal
        path; but rather than hardcode the name we take the register the
        DataBin actually exposes, which keeps this robust if the lowering
        ever names it differently. When several registers are present we
        prefer "c"; otherwise we take the sole field.
        '''
        fields = list(data.keys())
        if not fields:
            return {}
        name = "c" if "c" in fields else fields[0]
        bit_array = data[name]
        return bit_array.get_counts()

    # ── config preference ─────────────────────────────────────────────────

    def preferred_config(self) -> dict:
        '''
        Real-hardware runs default to a modest shot count: shots cost real
        QPU time on a metered account, and a fidelity number is already
        meaningful at a few hundred shots. A run script or user config still
        overrides this.
        '''
        return {"shots": 512}

    # ── calibration extraction ────────────────────────────────────────────────
    # The five-term calibration surface is read from the backend Target by
    # IBMProvider, shared with IBMSimulatedProvider: real and fake backends
    # expose the same BackendV2/Target API, so the extraction is identical.
    # See providers/ibm/ibm_provider.py.
