'''
Tags: Main

BaseProvider — Abstract base class for all DevQ hardware providers.

Every provider must implement:
  - get_device()  : construct and return a fully formed QuantumDevice
  - execute()     : run a circuit on the underlying backend

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

            {"provider": "ibm", "backend": {"backend_name": "FakeNairobiV2"}}
            {"provider": "devq", "backend": {"kind": "random",
                                             "num_qubits": 7}}

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
            ExecutionFuture
        '''
        pass

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