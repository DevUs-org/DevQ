'''
Tags: Research

IBMRealProvider — executes on REAL IBM quantum hardware via IBM Runtime.

This is the real-hardware counterpart to IBMSimulatedProvider. Where the
simulated provider builds a device from a *fake* backend's calibration and
runs circuits on a noisy Aer simulation, this one talks to a *real* backend
through QiskitRuntimeService: it pulls that backend's live calibration into
a DevQ QuantumDevice, and executes submitted circuits on the physical QPU
via SamplerV2.

WHY THIS LIVES IN research/. It is opt-in, account-gated, and costs real
QPU time — none of which belongs in DevQ core or its test suite. It is a
research instrument for gathering real-hardware results, not a shipped
provider, so it sits alongside the baselines under research/ and is exempt
from the core mutation discipline. It makes ZERO core changes: it implements
the same BaseProvider contract every provider does, and the kernel, router,
allocator and metrics treat it identically to any other provider.

WHAT IT SHARES WITH THE SIMULATED PROVIDER, AND WHY IT DUPLICATES. Real and
fake backends expose the SAME BackendV2 / Target API — that is the entire
point of Qiskit's fake backends, they are snapshots of real ones. So the
calibration extractors here (coupling map, readout/gate/edge errors, T2,
durations) are line-for-line the same logic as IBMSimulatedProvider's. They
are COPIED rather than imported on purpose: this is a research/ file and
should not couple a research instrument to a core module's private helpers,
where a refactor in core could silently break an account-gated script no CI
run exercises. Self-contained is the safer trade for something that runs
rarely and by hand.

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

    from research.providers.ibm_real_provider import IBMRealProvider
    ibm = IBMRealProvider(token=os.environ["IBM_QUANTUM_TOKEN"])
    dev = ibm.get_device("ibm_sherbrooke")
'''

from providers.base_provider import BaseProvider
from hardware.device import QuantumDevice


class IBMRealProvider(BaseProvider):

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

        # The allocator's physical placement: virtual qubit v runs on
        # physical qubit v2p_map[v]. Same construction as the simulated
        # provider so the two agree on what "placement" means.
        initial_layout = [v2p_map[v] for v in sorted(v2p_map)]

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

    # ── calibration extractors ────────────────────────────────────────────
    #
    # Copied from IBMSimulatedProvider on purpose (see module docstring):
    # real and fake backends share the BackendV2/Target API, so the logic is
    # identical, but a research instrument should not couple to a core
    # module's private helpers. Kept self-contained.

    def _extract_coupling_map(self, backend) -> list:
        '''
        Undirected coupling map from the backend's directed CouplingMap,
        deduplicated to sorted-tuple edges — consistent with how
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
        Per-qubit readout error via target['measure'][(q,)].error (the V2
        API). Falls back to 0.01 when a qubit's datum is unavailable.
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
        Per-edge 2-qubit gate error. The native 2q gate differs by device
        generation (ECR on Eagle/Heron, CX on Falcon, CZ on some Heron
        revisions), so the 2q gate set is DISCOVERED from the Target rather
        than hardcoded: every op acting on exactly 2 qubits is a candidate,
        and each edge takes the error of the first candidate defined on it.
        Falls back to 0.02 — and warns — only if no 2q gate reports an error,
        so bad calibration is never silently fabricated.
        '''
        target = backend.target
        twoq_gates = [
            name for name in target.operation_names
            if self._op_num_qubits(target, name) == 2
        ]
        if not twoq_gates:
            print(f"[IBMRealProvider] Warning: no 2-qubit gates found in "
                  f"Target — edge errors will use fallback 0.02.")

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
                print(f"[IBMRealProvider] Warning: no 2-qubit gate error for "
                      f"edge {key}, using fallback 0.02.")
                err = 0.02
            edge_error_map[key] = err
        return edge_error_map

    def _extract_gate_errors(self, backend, num_qubits) -> dict:
        '''
        Per-qubit single-qubit GATE error, restricted to physical 1q gates
        (sx, x, ...) so it never picks up readout error from `measure`, the
        virtual `rz` (0), or an idle `id`. Falls back to 5e-4 — and warns —
        only when none reports an error for a qubit.
        '''
        target = backend.target
        PHYSICAL_1Q = ("sx", "x", "sxdg", "rx", "ry", "u", "u3")
        oneq_gates = [g for g in PHYSICAL_1Q if g in target.operation_names]

        gate_error_map = {}
        for q in range(num_qubits):
            err = None
            for gate in oneq_gates:
                try:
                    candidate = target[gate][(q,)].error
                    if candidate is not None:
                        err = candidate
                        break
                except Exception:
                    continue
            if err is None:
                print(f"[IBMRealProvider] Warning: no physical 1-qubit gate "
                      f"error for qubit {q}, using fallback 5e-4.")
                err = 5e-4
            gate_error_map[q] = err
        return gate_error_map

    def _extract_t2_times(self, backend, num_qubits) -> dict:
        '''
        Per-qubit T2 in microseconds (Target reports SECONDS, scaled 1e6).
        Falls back to 100.0 µs — and warns — when unavailable or None.
        '''
        target = backend.target
        t2_map = {}
        for q in range(num_qubits):
            t2 = None
            try:
                props = target.qubit_properties[q]
                if props is not None and props.t2 is not None:
                    t2 = props.t2 * 1e6
            except Exception:
                t2 = None
            if t2 is None:
                print(f"[IBMRealProvider] Warning: no T2 for qubit {q}, "
                      f"using fallback 100.0 µs.")
                t2 = 100.0
            t2_map[q] = t2
        return t2_map

    def _extract_gate_durations(self, backend) -> tuple:
        '''
        Representative 1q and 2q gate durations in nanoseconds (Target
        reports SECONDS, scaled 1e9), taken as the MEDIAN across all
        instances of each arity's physical native gate — stable against a
        single outlier qubit/edge. Physical gates only, so measure's ~µs
        duration never dominates the 1q median. Falls back to 40 ns (1q) /
        400 ns (2q) — and warns — when no duration is available.
        '''
        target = backend.target
        PHYSICAL = {
            1: ("sx", "x", "sxdg", "rx", "ry", "u", "u3"),
            2: ("ecr", "cx", "cz", "cnot"),
        }

        def _median_duration(arity, fallback):
            names = [g for g in PHYSICAL[arity] if g in target.operation_names]
            durs = []
            for name in names:
                try:
                    props = target[name]
                except Exception:
                    continue
                for key, inst in props.items():
                    try:
                        if inst is not None and inst.duration is not None:
                            durs.append(inst.duration * 1e9)
                    except Exception:
                        continue
            if not durs:
                print(f"[IBMRealProvider] Warning: no {arity}-qubit gate "
                      f"duration in Target, using fallback {fallback} ns.")
                return fallback
            durs.sort()
            return durs[len(durs) // 2]

        return _median_duration(1, 40.0), _median_duration(2, 400.0)

    @staticmethod
    def _op_num_qubits(target, name):
        '''Number of qubits an operation acts on, or None if unknown.'''
        try:
            return target.operation_from_name(name).num_qubits
        except Exception:
            return None