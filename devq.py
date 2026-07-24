'''
Tags: Main

DevQ — Single entry point for the DevQ quantum execution system.

Attaches one or more quantum devices, resolves configuration per
device (four-level cascade) and globally (router policy), builds each
device's allocator and scheduler instances into a DeviceContext, then
on start() wires everything into the Kernel and launches QShell.

Usage:
    from devq import DevQ
    from providers.devq.devq_simulated_provider import DevQSimulatedProvider

    # Single device, default config — unchanged from Phase 0
    DevQ(DevQSimulatedProvider().get_device("random", 10)).start()

    # Multiple devices, chained
    DevQ(config_path="~/devq.config.json") \\
        .add_device(ibm_device) \\
        .add_device(sim_device, "~/sim.config.json") \\
        .start()

    # Bulk attach (no per-device configs)
    DevQ().add_devices([d0, d1, d2]).start()

Devices are indexed d0..dn in add order — stable for the session and
used by qdevices, --exec/--no-exec flags, and device-scoped commands.

Config file format (JSON) — device keys and global keys may share one file:
    {
        "scheduler":  "packing",      // any registered scheduler   (device)
        "allocator":  "noise_graph",  // any registered allocator   (device)
        "shots":      1024,           //                            (device)
        "qubit_error_weight": 0.1,    // noise cost weight α        (common)
        "edge_error_weight":  0.9,    // noise cost weight β        (common)
        "router":     "noise"         // any registered router      (global)
    }

Legal values for scheduler/allocator/router are whatever is registered
on this DevQ instance, not a fixed list. Built-ins ship registered;
third-party components are attached with register_scheduler(),
register_allocator(), register_router() and register_provider() before
build() or start() is called. A component may also declare its own
namespaced config keys, which then cascade and appear in qconfig exactly
like core keys — see docs/REGISTRY.md.

Configuration priority:
    DEVICE keys, resolved per device (later overrides earlier):
        1. DevQ core defaults
        2. That device's provider preferred_config()
        3. Global user config file   (DevQ(config_path=...))
        4. Per-device user config    (add_device(device, config_path))
    GLOBAL keys (router policy): core defaults ← global user file.
    COMMON keys (qubit_error_weight, edge_error_weight): resolved in
        BOTH scopes — the global copy steers the NoiseRouter yardstick,
        each device's copy steers that device's allocator.
'''

import re

from hardware.device_loader import load_device
from kernel.kernel import Kernel
from kernel.device_context import DeviceContext
from kernel.memory.memory_manager import MemoryManager
from shell.qshell import QShell
from config.config_loader import ConfigLoader
from registry.registry import Registry, RegistryError

from kernel.scheduler.fcfs_scheduler import FCFSScheduler
from kernel.scheduler.shortest_depth_scheduler import ShortestDepthScheduler
from kernel.scheduler.packing_scheduler import PackingScheduler
from kernel.memory.allocators.static_allocator import StaticAllocator
from kernel.memory.allocators.graph_allocator import GraphAllocator
from kernel.memory.allocators.noise_graph_allocator import NoiseGraphAllocator
from kernel.router.noise_router import NoiseRouter
from kernel.router.round_robin_router import RoundRobinRouter
from providers.devq.devq_simulated_provider import DevQSimulatedProvider

# DevQ's own components, seeded into every new DevQ instance's registry
# through the SAME public register_*() path a third party uses. Nothing
# here is privileged: if the extension path breaks, every built-in
# breaks at once and loudly, rather than the plugin path quietly rotting
# while the shipped system keeps working.
#
# PROVIDER NAMES ARE vendor.variant BY CONVENTION. Schedulers,
# allocators and routers are named for what they DO ("packing",
# "noise_graph"), so a bare name is already unambiguous. A provider is
# named for whose hardware it speaks to, and a bare vendor name claims
# the whole vendor: once "ibm" means a simulator there is no honest name
# left for real hardware, and a published workload spec saying
# "provider": "ibm" cannot tell a reader whether the results came off a
# machine or off Aer. Hence "devq.simulated", "ibm.simulated",
# "ibm.real". This is DevQ's convention for its own components and a
# suggestion for others, not a rule the registry enforces — a third
# party may name a provider whatever they like.
#
# The IBM provider is deliberately absent — importing it pulls in
# qiskit-ibm-runtime, which is an optional dependency. Register it
# yourself if you need it addressable by name:
#     devq.register_provider("ibm.simulated", IBMSimulatedProvider())
_BUILTINS = {
    "scheduler": {
        "fcfs":        FCFSScheduler,
        "sdf":         ShortestDepthScheduler,
        "packing":     PackingScheduler,
    },
    "allocator": {
        "static":      StaticAllocator,
        "graph":       GraphAllocator,
        "noise_graph": NoiseGraphAllocator,
    },
    "router": {
        "noise":       NoiseRouter,
        "round_robin": RoundRobinRouter,
    },
    "provider": {
        "devq.simulated": DevQSimulatedProvider,
    },
}


class DevQError(Exception):
    pass


# Names that would shadow a subcommand keyword or positional argument in
# the shell's device-token resolution (qerrors q|e|b, qtopology <int>).
_RESERVED_NAMES = frozenset({"q", "e", "b"})

_INDEX_NAME_RE = re.compile(r"^d\d+$")


def _validate_device_name(name, taken):
    '''
    Validate a user-supplied device name and return its canonical
    (lowercased) form.

    Names are aliases for device indices and are resolved wherever a dN
    token is accepted, so they must not be ambiguous with an index, with
    each other, or with a shell subcommand keyword.

    Raises:
        DevQError: on any invalid or conflicting name.
    '''
    if not isinstance(name, str):
        raise DevQError(f"Device name must be a string, got {type(name).__name__}.")

    cleaned = name.strip().lower()

    if not cleaned:
        raise DevQError("Device name cannot be empty or whitespace only.")

    if _INDEX_NAME_RE.match(cleaned):
        raise DevQError(
            f"Device name '{name}' is reserved — names matching d<number> "
            f"would be ambiguous with device indices. Devices always keep "
            f"their index reference (d0, d1, ...) alongside any name."
        )

    if cleaned in _RESERVED_NAMES:
        raise DevQError(
            f"Device name '{name}' is reserved — it would shadow a shell "
            f"subcommand argument (e.g. 'qerrors q d1'). "
            f"Reserved: {', '.join(sorted(_RESERVED_NAMES))}."
        )

    if any(c.isspace() for c in cleaned) or ',' in cleaned:
        raise DevQError(
            f"Device name '{name}' cannot contain whitespace or commas — "
            f"names appear in comma-separated lists such as --exec=a,b."
        )

    if cleaned in taken:
        raise DevQError(
            f"Duplicate device name '{name}' — names must be unique "
            f"(comparison is case-insensitive)."
        )

    return cleaned


class DevQ:

    def __init__(self, device=None, config_path=None):
        '''
        Args:
            device:      optional QuantumDevice — from any provider's
                         get_device(). Equivalent to calling
                         add_device(device) immediately.
            config_path: optional path to the GLOBAL user JSON config
                         file — applies to all attached devices (level 3
                         of the device cascade) and carries global keys
                         (router policy).
        '''
        self._global_config_path = config_path
        self._devices = []   # list of (QuantumDevice, config_path, name)
        self._names   = set()   # canonical names taken, for uniqueness
        self._built   = False

        # Registry and loader are per-DevQ-instance, never module state:
        # two DevQ objects in one process must not share registrations,
        # and a test that registers a mock component must not leak into
        # the next one.
        self._registry = Registry()
        self._seed_builtins()
        self._config = ConfigLoader(self._registry)

        if device is not None:
            self.add_device(device)

    def _seed_builtins(self):
        '''Register DevQ's own components through the public path.'''
        for kind, entries in _BUILTINS.items():
            for name, cls in entries.items():
                self._registry.register(kind, name, cls)

    # ── Extension ─────────────────────────────────────────────────────────────

    def register_scheduler(self, name, scheduler):
        '''
        Register a scheduler class under a name usable as the value of
        the "scheduler" config key. Returns self for chaining.

        Must be a CLASS, not an instance: DevQ constructs one scheduler
        per attached device, each bound to that device's own memory
        manager and queue.
        '''
        return self._register("scheduler", name, scheduler)

    def register_allocator(self, name, allocator):
        '''
        Register an allocator class under a name usable as the value of
        the "allocator" config key. Returns self for chaining.

        Must be a CLASS, not an instance — one allocator is constructed
        per attached device.
        '''
        return self._register("allocator", name, allocator)

    def register_router(self, name, router):
        '''
        Register a router under a name usable as the value of the
        "router" config key. Returns self for chaining.

        A class or a ready-made instance: there is exactly one router
        for the whole system, so a shared instance is safe.
        '''
        return self._register("router", name, router)

    def register_provider(self, name, provider):
        '''
        Register a provider under a name, so that devices can be named
        declaratively (e.g. in a benchmark workload spec) rather than
        constructed in code. Returns self for chaining.

        Register the CLASS, never an instance. Registration establishes
        only that a name is legal and what type it denotes; CONSTRUCTING
        the provider is the caller's business, so anything DevQ knows
        nothing about — credentials, endpoints, a seed — is passed
        by the caller to the object they build themselves:

            devq.register_provider("ionq", IonQProvider)
            devq.add_device(IonQProvider(api_key=KEY).get_device(...))

        A spec naming this provider gets one constructed by DevQ with
        the spec's seed. Registration is also what makes a device
        attachable at all: add_device() refuses a device whose provider
        class was never registered.
        '''
        return self._register("provider", name, provider)

    def _register(self, kind, name, component):
        '''
        Shared body of the register_*() methods.

        RegistryError is re-raised as DevQError so that callers of the
        DevQ facade see one exception type regardless of which layer
        rejected the component.
        '''
        if self._built:
            raise DevQError(
                f"cannot register {kind} '{name}' — build() has already run "
                "and the configuration has been read. Register all components "
                "before calling build() or start()."
            )

        try:
            self._registry.register(kind, name, component)
        except RegistryError as e:
            raise DevQError(str(e)) from None
        return self

    # ── Device attachment ─────────────────────────────────────────────────────

    def add_device(self, device, config_path=None, name=None):
        '''
        Attach a device. Returns self for chaining.

        Args:
            device:      QuantumDevice from any provider's get_device()
            config_path: optional per-device user JSON config file —
                         highest-priority level of the cascade, applies
                         to this device only.
            name:        optional alias for this device, usable anywhere
                         a dN token is accepted (--exec, --no-exec, and
                         device-scoped commands). The index reference
                         always keeps working; a name is an addition,
                         never a replacement. Case-insensitive, must be
                         unique, and may not look like an index or
                         shadow a shell keyword.
        '''
        self._require_registered_provider(device)

        resolved = None
        if name is not None:
            resolved = _validate_device_name(name, self._names)
            self._names.add(resolved)

        self._devices.append((load_device(device), config_path, resolved))
        return self

    def _require_registered_provider(self, device):
        '''
        Refuse a device whose provider was never registered.

        Nothing enters DevQ from an unknown component. Registration is
        the single gate every component passes through, and a device
        attached in Python bypassed it entirely until this check
        existed — so a session could run on a provider the system had
        no record of, and only a spec-driven session was ever forced to
        declare what it was using.

        REGISTERING AND CONSTRUCTING ARE SEPARATE ACTS, so this costs a
        line and never a credential. Register the CLASS, construct the
        instance yourself with whatever it needs, attach the device it
        builds:

            devq.register_provider("ionq", IonQProvider)
            devq.add_device(IonQProvider(api_key=KEY).get_device(...))

        The check is by type and yields only pass/fail. It deliberately
        does not recover the registered name: names address components
        in specs and config files, and the kernel deals in objects from
        attach time onward. Handing it a name here would be a layer
        violation.
        '''
        provider = getattr(device, "provider", None)
        if provider is None:
            raise DevQError(
                "device has no provider — add_device() expects a device "
                "built by a provider's get_device()."
            )

        cls = type(provider)
        if self._registry.is_registered("provider", cls):
            return

        known = ", ".join(sorted(self._registry.names("provider"))) or "none"
        raise DevQError(
            f"provider {cls.__name__} is not registered, so the device it "
            f"built cannot be attached. Register the class first:\n"
            f"    devq.register_provider(\"<name>\", {cls.__name__})\n"
            f"Registered providers: {known}. Register the CLASS, then "
            f"construct it yourself with any seed or credentials it "
            f"needs — DevQ never constructs a provider you attach by hand."
        )

    def add_devices(self, devices):
        '''
        Attach several devices at once. Returns self for chaining.

        Each entry is either a bare device or a (device, name) tuple;
        the two forms may be mixed freely:

            .add_devices([(d0, "nairobi"), (d1, "lagos"), d2, d3])

        Per-device config paths are not available here — use
        add_device(device, config_path, name) when a device needs one.
        '''
        for entry in devices:
            if isinstance(entry, tuple):
                if len(entry) != 2:
                    raise DevQError(
                        f"add_devices entries are either a device or a "
                        f"(device, name) tuple — got a {len(entry)}-tuple. "
                        f"Per-device config paths need add_device()."
                    )
                device, name = entry
                self.add_device(device, name=name)
            else:
                self.add_device(entry)
        return self

    # ── Session start ─────────────────────────────────────────────────────────

    def start(self):
        '''
        Build the session and hand control to the interactive shell.
        Blocks until the user exits.

        Raises:
            DevQError: if no devices are attached.
        '''
        self.build(interactive=True).cmdloop()

    def build(self, interactive=False):
        '''
        Resolve configs, build one DeviceContext per attached device
        and the configured router, wire everything into the Kernel and
        return the QShell — WITHOUT starting the command loop.

        Everything start() does except blocking on input, so a session
        can be driven programmatically via shell.onecmd(...). Used by
        run_tests.py; also the hook for any non-interactive front end.

        Args:
            interactive: True only when a human will drive this shell at
                         a terminal (start() sets it). Programmatic
                         callers leave it False, which skips readline
                         history setup — see QShell.__init__.

        Returns:
            QShell, fully wired and ready to accept commands.

        Raises:
            DevQError: if no devices are attached.
        '''
        if not self._devices:
            raise DevQError(
                "no devices attached — pass a device to DevQ(...) or call "
                "add_device()/add_devices() before start()."
            )

        # Configuration has now been read, so no further registration
        # could affect the system being built. Refusing it is better
        # than accepting it and silently doing nothing.
        self._registry.freeze()
        self._built = True

        global_config, global_provenance = self._config.load_global(
            self._global_config_path
        )

        contexts = []
        for index, (device, device_config_path, name) in enumerate(self._devices):
            # Stamp session identity BEFORE anything else touches the
            # device: providers key their per-device state on index, and
            # on_attach() is where they create it. Nothing downstream may
            # assume a device knows its index until this has run.
            device.attach(index, name)
            device.provider.on_attach(device)

            config, provenance = self._config.load_device(
                device.provider,
                index,
                global_config_path=self._global_config_path,
                device_config_path=device_config_path
            )

            allocator = self._registry.get("allocator", config["allocator"])(
                qubit_error_weight = config["qubit_error_weight"],
                edge_error_weight  = config["edge_error_weight"]
            )
            memory    = MemoryManager(device, allocator)
            scheduler = self._registry.get("scheduler", config["scheduler"])(
                memory, None   # process_table injected below by Kernel wiring
            )

            contexts.append(DeviceContext(
                index          = index,
                name           = name,
                device         = device,
                memory_manager = memory,
                scheduler      = scheduler,
                config         = config,
                provenance     = provenance
            ))

        router = self._build_router(global_config)
        kernel = Kernel(contexts, router)

        # Schedulers share the kernel's global process table
        for ctx in contexts:
            ctx.scheduler.process_table = kernel.process_table

        return QShell(
            kernel            = kernel,
            global_config     = global_config,
            global_provenance = global_provenance,
            labels            = self._config.labels(),
            interactive       = interactive
        )

    def _build_router(self, global_config):
        '''
        Construct the configured router, or return a registered instance.

        A router registered as a ready-made instance was built by the
        user with arguments DevQ knows nothing about, so its weights are
        left exactly as the user set them rather than being overwritten
        from config.
        '''
        router = self._registry.get("router", global_config["router"])

        if not isinstance(router, type):
            return router

        return router(
            router_queue_weight = global_config["router_queue_weight"],
            router_noise_weight = global_config["router_noise_weight"],
            qubit_error_weight = global_config["qubit_error_weight"],
            edge_error_weight = global_config["edge_error_weight"]
        )