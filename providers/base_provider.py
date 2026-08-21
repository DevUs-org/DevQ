'''
Tags: Main

BaseProvider — Abstract base class for all DevQ hardware providers.

Every provider must implement:
  - get_device()  : construct and return a fully formed QuantumDevice
  - execute()     : run a circuit on the underlying backend, returning
                    counts that satisfy the OUTPUT CONTRACT documented on
                    execute() — the bitstring width and index conventions
                    every provider must agree on. Use _counts_width().

Optionally override:
  - preferred_config() : express provider-level config preferences
    These sit between DevQ core defaults and the user config file.
    Return only the keys you want to override — omit the rest.
'''

from abc import ABC, abstractmethod


class BaseProvider(ABC):

    def __init__(self, seed=None):
        '''
        Args:
            seed : int or None — base seed for reproducible behaviour.
                   Providers that have stochastic behaviour (topology or
                   error map generation, noisy simulation) derive their
                   randomness from this seed; providers with none inherit
                   and ignore it. None (default) preserves unseeded,
                   non-deterministic behaviour.
        '''
        self.seed = seed
        # Incremented by get_device()/get_device_from_spec() in
        # concrete providers so set_seed() can refuse to run late.
        self._devices_created = 0

    @abstractmethod
    def get_device(self, *args, **kwargs):
        '''
        Construct and return a fully formed QuantumDevice for this provider.
        The returned device must have self set as device.provider, and
        must report its hardware identity as `kind`.

        A single provider instance may serve multiple devices, and
        several of those may be the SAME KIND. Per-device state must
        therefore be keyed by device.index, not by kind — and must be
        created in on_attach(), since no index exists yet here.
        '''
        pass

    def get_device_from_spec(self, spec):
        '''
        Construct a QuantumDevice from a declarative spec dictionary.

        This is the entry point used when devices are described in data
        rather than in code — a benchmark workload spec naming its
        devices, for example:

            {"provider": "ibm.simulated",
             "backend": {"backend_name": "FakeNairobiV2"}}
            {"provider": "devq.simulated",
             "backend": {"kind": "random", "num_qubits": 7}}

        The `backend` object is passed here as `spec`. The default
        implementation splats it into get_device(), which works for any
        provider whose get_device() parameters are named the same as the
        spec keys. Providers wanting a different spec vocabulary, or
        validation with better errors than a bare TypeError, override
        this.

        Deliberately NOT abstract: it has a working default, and making
        it abstract would break every provider written before it
        existed.

        Args:
            spec: dict of construction arguments for this provider

        Returns:
            QuantumDevice
        '''
        if not isinstance(spec, dict):
            raise TypeError(
                f"{type(self).__name__}.get_device_from_spec() expects a "
                f"dict, got {type(spec).__name__}."
            )
        return self.get_device(**spec)

    def set_seed(self, seed):
        '''
        Set the base seed BEFORE any device has been created.

        Called by the workload-spec parser when a provider was
        registered as an unseeded INSTANCE and the spec supplies a seed.
        It is never called on a provider that already carries a seed —
        that case is a conflict the instance wins — so there is no
        seed-derived state to unwind here.

        ⚠ OVERRIDE THIS if your provider derives state from the seed AT
        CONSTRUCTION. The default sets self.seed, which is enough for a
        provider that reads self.seed when it executes, and NOT enough
        for one that builds a random.Random in __init__ — that generator
        would keep its original state and the spec's seed would be
        silently ignored. The parser detects a provider that ignores the
        call and warns, but it cannot fix it.

        Args:
            seed: int — the base seed to adopt

        Raises:
            RuntimeError: if devices have already been created, since
                          the contract above no longer holds.
        '''
        if getattr(self, "_devices_created", 0):
            raise RuntimeError(
                f"{type(self).__name__}.set_seed() called after "
                f"{self._devices_created} device(s) were already created. "
                f"Seeding must precede device construction — the devices "
                f"already built carry error maps derived from the old seed."
            )
        self.seed = seed

    def on_attach(self, device):
        '''
        Called by the kernel once, when a device built by this provider
        is attached to a session and has just received its index.

        This is the correct place to create per-device state. It cannot
        be done in get_device(): devices are constructed before the
        kernel exists, so at that point the device has no index, and
        keying state by kind silently collapses several same-kind
        devices onto one shared slot.

        Providers keying state here must key on device.index — it is
        always present and unique, whereas kind is shared and name is
        optional.

        Default is a no-op, so providers with no per-device state (and
        every provider written before this hook existed) need not
        implement it.

        Args:
            device: QuantumDevice — already stamped with index and name
        '''
        pass

    @abstractmethod
    def execute(self, circuit, v2p_map, shots, device):
        '''
        Execute a circuit on the underlying backend.

        Args:
            circuit  : CircuitRep
            v2p_map  : dict — virtual to physical qubit mapping
            shots    : number of shots
            device   : QuantumDevice — the device this job was allocated
                       to. Providers serving multiple devices use this to
                       select per-device state (backend, noise model).

        Returns:
            ExecutionFuture resolving to an ExecutionResult.

        OUTPUT CONTRACT — the counts every provider must agree on.
        The result's `counts` maps measured BITSTRINGS to shot counts,
        and every provider must report them the same way, because
        cross-provider comparison (the fidelity metric, baseline sweeps)
        compares these strings directly and a disagreement is silent:

          - WIDTH is the declared classical register: len(bitstring) ==
            circuit.num_clbits, falling back to circuit.num_qubits when no
            creg is declared. Use _counts_width(circuit) — do not
            re-derive it, so the rule cannot drift between providers.
          - A measured bit sits at ITS OWN INDEX: `measure q -> c[j]`
            places that qubit's outcome at position j of the string, so
            the string position is the classical-bit index and needs no
            side map. An unmeasured bit reads 0.
          - Explicit measures are HONOURED; a circuit with no measures
            falls back to measuring all qubits (see the measurement
            blocks in run_tests.py). `reset` is applied at its source
            position in circuit.instructions, not lumped at the end.

        A provider that ignores gate semantics (a uniform mock) still
        owes the WIDTH and index conventions — only the distribution is
        its own business.
        '''
        pass

    @staticmethod
    def _counts_width(circuit):
        '''
        The bitstring width a provider must report counts over: the
        declared classical-register width, or the qubit count when no
        classical register was declared.

        This is the single source of the Option B width rule (see
        execute()'s output contract). Both built-in providers call it;
        a new provider that calls it inherits the convention for free and
        cannot drift from the others by re-deriving the rule slightly
        differently.

        Args:
            circuit: CircuitRep

        Returns:
            int — the number of classical bits the result strings span.
        '''
        return circuit.num_clbits or circuit.num_qubits

    def reference_ideal(self, circuit):
        '''
        The IDEAL, noiseless measured-bit distribution for a circuit —
        {bitstring: probability} at the Option-B classical width — or None
        if this provider cannot produce one.

        This is the yardstick the fidelity metric compares a real noisy run
        against. It is an OPTIONAL capability: the ideal is a property of
        the circuit, not of any device, and computing it means simulating
        the circuit noiselessly, which not every provider can do. The
        default declines by returning None — a provider whose "execution"
        is a uniform mock (DevQSimulatedProvider) has no meaningful ideal
        to offer and correctly inherits this default, so it is never used
        as a reference. A provider that CAN simulate the circuit faithfully
        (IBMSimulatedProvider, via a noiseless Aer path) overrides this.

        The shipped, vendor-neutral reference orchestrator asks a capable
        provider for ideals through this method, so DevQ core obtains them
        without depending on any particular provider — the same inversion
        the rest of the provider contract uses. When no attached provider
        implements it, fidelity has no ideal to compare against and is
        reported as None (the population rule: undefined, never a fake 0).

        Args:
            circuit : CircuitRep

        Returns:
            dict {bitstring: probability} over the classical register, or
            None if unsupported.
        '''
        return None

    def supports_dynamic(self, circuit) -> bool:
        '''
        Whether this provider can faithfully EXECUTE a dynamic circuit — one
        whose later operations depend on the classical outcome of an earlier
        measurement within the same run (an `if (creg==N)` classical control,
        the feedback loop mid-circuit measurement is the primitive for).

        An OPTIONAL capability, the sibling of reference_ideal(): where that
        asks "can you give me an ideal?", this asks "can your runtime honour
        this circuit's classical feedback?". It is expressed in DevQ's own
        terms — a plain predicate — so the kernel can ask through it without
        learning anything about how a provider does it. The default DECLINES
        by returning False: DevQ's own execution model is terminal-measurement
        with no classical feedback, so DevQSimulatedProvider correctly inherits
        this and honestly declines. A provider whose runtime supports dynamic
        circuits (the IBM providers — real Heron hardware runs feedback
        natively, Aer runs it in simulation) overrides this to return True.

        The circuit is passed, not just a bare flag, so a provider may later
        answer with finer granularity (e.g. single-bit conditions but not
        multi-bit register comparisons) without a contract change; a provider
        that answers uniformly simply ignores it.

        The kernel reads this at routing time, through the memory manager's
        feasibility verdict, to keep a dynamic job off a device whose provider
        cannot run it — so a job needing feedback routes to a capable device
        when one is attached, and is REJECTED with a per-device reason only
        when none is. See docs/ROADMAP.md (Phase 6) for the execution-model
        context.

        Args:
            circuit : CircuitRep

        Returns:
            bool — True if this provider's runtime can execute the circuit's
            classical feedback; False (the default) to decline.
        '''
        return False

    def preferred_config(self) -> dict:
        '''
        Override to express provider-level configuration preferences.

        These override DevQ core defaults but are themselves overridden
        by the user's local config file. Return only the keys you want
        to set — omit keys you are happy to leave at core defaults.

        Example:
            return {"allocator": "static", "shots": 2048}

        Valid keys: "scheduler", "allocator", "shots"
        '''
        return {}