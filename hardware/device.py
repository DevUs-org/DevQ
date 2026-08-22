'''
Tags: Main

DevQ Device Definition

QuantumDevice is a pure data container for a quantum hardware device.
All error map generation and device parameter provision is the
responsibility of the provider — not this class.

Execution is delegated to the provider via device.execute(), keeping
the kernel decoupled from any provider-specific logic.

THREE IDENTITY FIELDS, three distinct concepts — do not conflate them:

  kind   HARDWARE identity, reported by the provider: "FakeNairobiV2",
         "random_backend". Says WHAT this device is. Never unique — a
         session may hold four devices of the same kind. Caller casing
         is preserved. May be None on providers that cannot know it
         until a connection resolves; renders as "-" until set.

  index  SESSION identity, assigned by the kernel in add order: 0, 1, 2.
         Says WHICH device this is. Always unique, always present once
         attached. This is the correct key for any per-device state a
         provider holds.

  name   Optional user ALIAS for the index, lowercased and unique per
         session. Says what the USER calls it. None when unnamed; the
         index reference always keeps working regardless.

index and name are None until the kernel calls attach(). Constructing a
device does not attach it — providers build devices before the kernel
has assigned anything.
'''

from hardware.topology_graph import build_graph


class QuantumDevice:
    def __init__(self, kind, num_qubits, coupling_map, basis_gates,
                 error_map, edge_error_map, provider,
                 gate_error_map=None, t2_map=None,
                 gate_1q_duration=None, gate_2q_duration=None):
        '''
        Args:
            kind: hardware identity string, or None if the provider
                  cannot resolve it yet (see set_kind).

        Calibration model. A device carries the five terms that
        characterise a NISQ backend, matching the standard IBM Target
        surface:

          - readout error, per qubit           -> error_map / qubit_error()
          - single-qubit gate error, per qubit -> gate_error_map / gate_error()
          - two-qubit gate error, per edge     -> edge_error_map / edge_error()
          - T2 coherence, per qubit (µs)        -> t2_map / t2()
          - gate duration, per gate arity (ns)  -> gate_*_duration / gate_duration()

        The three later fields are optional at construction so a device
        built by older code (or a test) still constructs; each accessor
        falls back to a fixed typical value when its map is absent or a
        key is missing, exactly as qubit_error/edge_error already do.
        Durations are per-ARITY scalars (one for 1-qubit gates, one for
        2-qubit gates), not per-qubit maps: execution-time estimation sums
        durations along a circuit's critical path by gate arity, so that
        is the granularity the consumers actually use. Adding a spatially
        resolved or per-gate-type duration later is the same additive
        accessor pattern.
        '''
        self.kind         = kind
        self.num_qubits   = num_qubits
        self.coupling_map = coupling_map
        self.basis_gates  = basis_gates
        self.provider     = provider
        self.graph        = build_graph(coupling_map, num_qubits)
        self.error_map = error_map
        self.edge_error_map = {
            tuple(sorted((u, v))): err
            for (u, v), err in edge_error_map.items()
        }

        # Extended calibration (see class docstring). None = unpopulated,
        # in which case the accessors return typical fallbacks.
        self.gate_error_map   = gate_error_map or {}
        self.t2_map           = t2_map or {}
        self.gate_1q_duration = gate_1q_duration
        self.gate_2q_duration = gate_2q_duration

        # Session identity — assigned by the kernel at attach time.
        self.index = None
        self.name  = None

    # ── Identity ──────────────────────────────────────────────────────────────

    def attach(self, index, name=None):
        '''
        Stamp this device with its session identity. Called ONCE by the
        kernel from DevQ.add_device(), which is the first moment an
        index exists — providers construct devices before that.

        Providers keying per-device state should key on index: it is
        always present and always unique, whereas kind is shared across
        same-kind devices and name is optional.

        Args:
            index: int — stable device index (d0..dn, add order)
            name:  str or None — validated, lowercased alias

        Raises:
            RuntimeError: if called twice. Session identity is assigned
                          once; a second call means two kernels are
                          claiming the same device object.
        '''
        if self.index is not None:
            raise RuntimeError(
                f"Device (kind={self.display_kind}) is already attached as "
                f"d{self.index}. A device object belongs to one session — "
                f"build a second device rather than re-attaching this one."
            )
        self.index = index
        self.name  = name

    def set_kind(self, kind):
        '''
        Set the hardware identity after construction.

        For providers that cannot know what hardware they are talking to
        until a connection resolves — a cloud provider selecting a
        backend by API key, for example. Until this is called, kind is
        None and renders as "-".

        NEVER put a credential here. This value is displayed by qdevices
        and written to every event-log record.
        '''
        self.kind = kind

    @property
    def display_kind(self):
        '''Hardware identity for display, "-" when unresolved. Mirrors
        the convention used for unnamed devices in qdevices output.'''
        return self.kind if self.kind else "-"

    @property
    def ref(self):
        '''Canonical index reference, "d1", or "(unattached)" before the
        kernel has assigned one.'''
        return f"d{self.index}" if self.index is not None else "(unattached)"

    # ── Execution ─────────────────────────────────────────────────────────────

    def execute(self, circuit, v2p_map, shots):
        '''
        Delegate execution to the provider, passing self so providers
        serving multiple devices can select per-device state.
        Returns an ExecutionFuture.
        '''
        return self.provider.execute(circuit, v2p_map, shots, self)

    def qubit_error(self, q):
        return self.error_map.get(q, 0.01)

    def edge_error(self, u, v):
        return self.edge_error_map.get(tuple(sorted((u, v))), 0.05)

    def edges(self):
        '''
        The device's coupling edges as canonical (u, v) tuples with u < v.

        The edge-enumeration accessor: cost-scoring components (the noise
        router, the noise-graph allocator) need to iterate edges, and this
        is the interface for it — so no consumer reaches into the raw
        `edge_error_map` and the storage can change without touching them
        (the same accessor discipline `qubit_error`/`edge_error` give for
        scalar lookups). Pair each edge with `edge_error(u, v)` for its rate.
        '''
        return list(self.edge_error_map.keys())

    def gate_error(self, q):
        '''
        Single-qubit gate error for qubit q. Fallback 5e-4 — a typical
        superconducting 1-qubit gate error (IBM devices report ~1e-4 to
        ~1e-3) — when the map is unpopulated or q is absent.
        '''
        return self.gate_error_map.get(q, 5e-4)

    def t2(self, q):
        '''
        T2 coherence time for qubit q, in microseconds. Fallback 100.0 —
        a typical superconducting T2 (IBM devices report ~50–300 µs) —
        when the map is unpopulated or q is absent.
        '''
        return self.t2_map.get(q, 100.0)

    def gate_duration(self, arity):
        '''
        Gate duration in nanoseconds for a gate of the given ARITY
        (1 = single-qubit, 2 = two-qubit). Per-arity, not per-qubit:
        execution-time estimation sums along a circuit's critical path by
        gate arity. Fallbacks — 40 ns (1q) and 400 ns (2q) — are typical
        superconducting values (IBM 1q ~20–60 ns, 2q ~200–660 ns) used
        when the device did not supply a duration.
        '''
        if arity == 1:
            return self.gate_1q_duration if self.gate_1q_duration is not None else 40.0
        if arity == 2:
            return self.gate_2q_duration if self.gate_2q_duration is not None else 400.0
        raise ValueError(
            f"gate_duration: arity must be 1 or 2, got {arity!r}. DevQ's "
            f"duration model is per-arity (1q / 2q); higher-arity gates "
            f"decompose to these before scheduling."
        )

    def __repr__(self):
        return (f"QuantumDevice(kind={self.display_kind}, "
                f"ref={self.ref}, "
                f"num_qubits={self.num_qubits}, "
                f"provider={type(self.provider).__name__})")