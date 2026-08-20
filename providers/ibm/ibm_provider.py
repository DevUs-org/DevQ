'''
Tags: Provider

IBMProvider — shared base for DevQ's IBM/Qiskit-family providers.

Both IBM providers — IBMSimulatedProvider (Qiskit V2 fake backends +
AerSimulator noise-model execution) and IBMRealProvider (live hardware via
QiskitRuntimeService) — read the SAME kind of backend: a Qiskit BackendV2
carrying a Target. Reading DevQ's five-term calibration surface out of that
Target (coupling map, readout error, 2q-edge error, 1q-gate error, T2, gate
durations) is therefore identical work regardless of whether the Target came
from a fake backend or a real one — the extraction touches only the Target
API, never the execution seam. This base owns that shared work so the two
providers cannot drift apart by re-deriving it slightly differently, the same
single-source-of-truth discipline as BaseProvider._counts_width.

It also owns `full_layout` — the full-device-width `initial_layout` builder
(used qubits at their allocated positions, every unused physical qubit as an
ancilla). That construction is Qiskit-specific: it returns a qiskit
`Layout` and only makes sense when a provider's placement API is a Qiskit
`initial_layout`. It therefore lives HERE, on the Qiskit-family base, not on
BaseProvider — a non-Qiskit provider (Braket, photonic) never inherits it and
BaseProvider carries no Qiskit dependency, not even a deferred one.

Qiskit is imported lazily inside the methods that need it (as in the two
concrete providers), so importing this module — or subclassing it — does not
require qiskit to be installed. A subclass that never calls these methods
never triggers the import.

What stays on the SUBCLASS (this base deliberately does not touch it):
  - __init__            : sim takes seed only; real takes seed + secrets.
  - _load_backend       : fake-backend class vs QiskitRuntimeService.
  - get_device/on_attach: how a device and its per-device session are built.
  - execute             : how a circuit is run (AerSimulator vs SamplerV2).
  - preferred_config    : per-provider config preferences.
Everything a subclass builds a QuantumDevice from can still call the shared
_extract_* helpers here.
'''

from providers.base_provider import BaseProvider


class IBMProvider(BaseProvider):
    '''
    Base for IBM/Qiskit-family providers. Not registered or instantiated
    directly — IBMSimulatedProvider and IBMRealProvider subclass it. It
    provides the shared Target-reading calibration extractors and the
    Qiskit full-device-width layout builder; it leaves device construction
    and execution to the subclass.
    '''

    # ── Qiskit full-device-width layout ───────────────────────────────────────

    def full_layout(self, qc, v2p_map, device):
        '''
        Build a FULL-DEVICE-WIDTH Qiskit initial_layout for a circuit that
        occupies only a few of the device's physical qubits.

        The allocator places virtual qubit v on physical qubit v2p_map[v].
        A Qiskit `initial_layout` built from ONLY those mapped qubits is a
        PARTIAL layout. Qiskit's Aer path rejects a partial layout outright
        ("The 'layout' must be full (with ancilla)."); the real-hardware
        transpile path happens to pad it silently. Both want the same thing:
        a layout that accounts for EVERY physical qubit on the device — the
        used ones at their allocated positions, and every unused physical
        qubit occupied by an ancilla. This helper builds that, so neither
        IBM provider hand-rolls (and mis-copies) the padding.

        HOW: an ancilla QuantumRegister sized to the unused qubits is ADDED
        to `qc` in place, and a qiskit `Layout` is returned that maps the
        circuit's real qubits to their allocated physical indices and each
        ancilla to one remaining physical index. The transpile target must
        know the device's true width (transpile against the backend, or an
        Aer simulator that carries the device's coupling map) for the padded
        layout to validate — the partial-layout crash is precisely the
        target NOT knowing the full width.

        This lives on the Qiskit-FAMILY base, not on BaseProvider: it
        returns a qiskit `Layout` and is meaningful only when a provider's
        placement API is a Qiskit `initial_layout`. A non-Qiskit provider
        never inherits it, so BaseProvider stays free of any qiskit
        dependency. It mirrors `flatten_key`: one shared source of truth for
        a construction the two IBM providers would otherwise duplicate.

        Args:
            qc       : the Qiskit QuantumCircuit being executed. MUTATED —
                       an ancilla register is appended so its qubits exist
                       for the returned Layout to reference. The circuit's
                       CLASSICAL width is untouched, so the counts width
                       (see _counts_width) does not change.
            v2p_map  : dict — virtual qubit index -> physical qubit index,
                       the allocator's placement.
            device   : QuantumDevice — supplies num_qubits, the device's
                       full physical width.

        Returns:
            qiskit.transpiler.Layout — a full-width layout ready to pass as
            transpile(..., initial_layout=...). When the device is already
            fully occupied (no unused qubits) no ancilla register is added
            and the layout maps only the circuit's own qubits.
        '''
        from qiskit.transpiler import Layout
        from qiskit.circuit import QuantumRegister

        n_phys = device.num_qubits
        used   = {v2p_map[v] for v in v2p_map}

        layout = Layout()
        for v in v2p_map:
            layout[qc.qubits[v]] = v2p_map[v]

        unused = [p for p in range(n_phys) if p not in used]
        if unused:
            ancilla = QuantumRegister(len(unused), 'ancilla')
            qc.add_register(ancilla)
            for slot, p in zip(ancilla, unused):
                layout[slot] = p

        return layout

    # ── Calibration extraction (shared Target reading) ────────────────────────
    #
    # These read DevQ's five-term calibration surface out of a Qiskit
    # BackendV2 Target. The work is identical for a fake backend and a real
    # one — it touches only the Target API — so it lives here rather than
    # being copy-pasted into each provider. Warning messages are prefixed
    # with the concrete subclass name (type(self).__name__), so a fake-backend
    # warning still reads [IBMSimulatedProvider] and a live one [IBMRealProvider].

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
        Per-edge 2-qubit gate error rates from Target.

        The native 2-qubit gate differs across IBM device generations:
        ECR on Eagle/Heron backends (Sherbrooke, Torino, ...), CX on older
        Falcon backends (NairobiV2, MumbaiV2, ...), CZ on some Heron
        revisions. Rather than hardcoding a gate name, the gate set is
        discovered from the Target: every operation acting on exactly 2
        qubits is a candidate, and each edge takes the error of the first
        candidate gate defined on it. Falls back to 0.02 for an edge only if
        no 2-qubit gate reports an error — and warns — so bad calibration is
        never silently fabricated. Edges are tried in both directions since
        the backend coupling map is directed but ours is undirected.
        '''
        target = backend.target
        prefix = type(self).__name__

        twoq_gates = [
            name for name in target.operation_names
            if self._op_num_qubits(target, name) == 2
        ]

        if not twoq_gates:
            print(f"[{prefix}] Warning: no 2-qubit gates found "
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
                print(f"[{prefix}] Warning: no 2-qubit gate "
                      f"error for edge {key}, using fallback 0.02.")
                err = 0.02

            edge_error_map[key] = err

        return edge_error_map

    def _extract_gate_errors(self, backend, num_qubits) -> dict:
        '''
        Per-qubit single-qubit GATE error rates from Target.

        A Target reports several 1-qubit operations that are NOT unitary
        gate errors: `measure` carries the READOUT error, `delay`/`reset`
        carry none, `rz` is virtual (error 0), and `id` is an idle. Taking
        "the first 1-qubit operation" would wrongly pick up readout error,
        so extraction is restricted to the physical single-qubit gates a
        device actually calibrates — sx and x — in preference order. Falls
        back to 5e-4 — and warns — only if none reports an error for a qubit,
        so bad calibration is never silently fabricated (mirrors
        _extract_edge_errors).

        ⚠ These values come from the pinned qiskit-ibm-runtime calibration;
        a version bump changes them, like every reference number in
        docs/TEST_BLOCKS.md.
        '''
        target = backend.target
        prefix = type(self).__name__

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
                print(f"[{prefix}] Warning: no physical 1-qubit "
                      f"gate error for qubit {q}, using fallback 5e-4.")
                err = 5e-4
            gate_error_map[q] = err

        return gate_error_map

    def _extract_t2_times(self, backend, num_qubits) -> dict:
        '''
        Per-qubit T2 in microseconds (Target reports SECONDS, scaled 1e6).
        Falls back to 100.0 µs — and warns — when unavailable or None.

        ⚠ Pinned-calibration value; see _extract_gate_errors.
        '''
        target = backend.target
        prefix = type(self).__name__
        t2_map = {}
        for q in range(num_qubits):
            t2 = None
            try:
                props = target.qubit_properties[q]
                if props is not None and props.t2 is not None:
                    t2 = props.t2 * 1e6      # seconds -> microseconds
            except Exception:
                t2 = None
            if t2 is None:
                print(f"[{prefix}] Warning: no T2 for qubit "
                      f"{q}, using fallback 100.0 µs.")
                t2 = 100.0
            t2_map[q] = t2

        return t2_map

    def _extract_gate_durations(self, backend) -> tuple:
        '''
        Representative 1q and 2q gate durations in nanoseconds (Target
        reports SECONDS, scaled 1e9), taken as the MEDIAN across all
        instances of each arity's physical native gate — stable against a
        single outlier qubit/edge. Physical gates only, so measure's ~µs
        duration does not dominate the 1q median. Falls back to 40 ns (1q) /
        400 ns (2q) — and warns — when no duration is available.

        ⚠ Pinned-calibration value; see _extract_gate_errors.
        '''
        target = backend.target
        prefix = type(self).__name__

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
                            durs.append(inst.duration * 1e9)   # s -> ns
                    except Exception:
                        continue
            if not durs:
                print(f"[{prefix}] Warning: no {arity}-qubit "
                      f"gate duration in Target, using fallback {fallback} ns.")
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
