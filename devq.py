'''
Tags: Main

DevQ — Single entry point for the DevQ quantum execution system.

Attaches one or more quantum devices, resolves configuration per
device (four-level cascade) and globally (router policy), builds each
device's allocator and scheduler instances into a DeviceContext, then
on start() wires everything into the Kernel and launches QShell.

Usage:
    from devq import DevQ
    from hardware.providers.devq.devq_simulated_provider import DevQSimulatedProvider

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
        "scheduler":  "packing",      // fcfs | sdf | packing      (device)
        "allocator":  "noise_graph",  // static | graph | noise_graph (device)
        "shots":      1024,           //                            (device)
        "router":     "noise"         // noise | round_robin       (global)
    }

Configuration priority:
    DEVICE keys, resolved per device (later overrides earlier):
        1. DevQ core defaults
        2. That device's provider preferred_config()
        3. Global user config file   (DevQ(config_path=...))
        4. Per-device user config    (add_device(device, config_path))
    GLOBAL keys (router policy): core defaults ← global user file.
'''

from hardware.device_loader import load_device
from kernel.kernel import Kernel
from kernel.device_context import DeviceContext
from kernel.memory.memory_manager import MemoryManager
from shell.qshell import QShell
from config.config_loader import load_global_config, load_device_config

from kernel.scheduler.fcfs_scheduler import FCFSScheduler
from kernel.scheduler.shortest_depth_scheduler import ShortestDepthScheduler
from kernel.scheduler.packing_scheduler import PackingScheduler
from kernel.memory.allocators.static_allocator import StaticAllocator
from kernel.memory.allocators.graph_allocator import GraphAllocator
from kernel.memory.allocators.noise_graph_allocator import NoiseGraphAllocator
from kernel.router.noise_router import NoiseRouter
from kernel.router.round_robin_router import RoundRobinRouter

_SCHEDULER_MAP = {
    "fcfs":        FCFSScheduler,
    "sdf":         ShortestDepthScheduler,
    "packing":     PackingScheduler,
}

_ALLOCATOR_MAP = {
    "static":      StaticAllocator,
    "graph":       GraphAllocator,
    "noise_graph": NoiseGraphAllocator,
}


class DevQError(Exception):
    pass


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
        self._devices = []   # list of (QuantumDevice, device_config_path)

        if device is not None:
            self.add_device(device)

    # ── Device attachment ─────────────────────────────────────────────────────

    def add_device(self, device, config_path=None):
        '''
        Attach a device. Returns self for chaining.

        Args:
            device:      QuantumDevice from any provider's get_device()
            config_path: optional per-device user JSON config file —
                         highest-priority level of the cascade, applies
                         to this device only.
        '''
        self._devices.append((load_device(device), config_path))
        return self

    def add_devices(self, devices):
        '''
        Attach several devices at once (no per-device configs).
        Returns self for chaining.
        '''
        for device in devices:
            self.add_device(device)
        return self

    # ── Session start ─────────────────────────────────────────────────────────

    def start(self):
        '''
        Resolve configs, build one DeviceContext per attached device
        and the configured router, wire everything into the Kernel,
        attach QShell, start the session.

        Raises:
            DevQError: if no devices are attached.
        '''
        if not self._devices:
            raise DevQError(
                "no devices attached — pass a device to DevQ(...) or call "
                "add_device()/add_devices() before start()."
            )

        global_config, global_provenance = load_global_config(
            self._global_config_path
        )

        contexts = []
        for index, (device, device_config_path) in enumerate(self._devices):
            config, provenance = load_device_config(
                device.provider,
                index,
                global_config_path=self._global_config_path,
                device_config_path=device_config_path
            )

            allocator = _ALLOCATOR_MAP[config["allocator"]]()
            memory    = MemoryManager(device, allocator)
            scheduler = _SCHEDULER_MAP[config["scheduler"]](
                memory, None   # process_table injected below by Kernel wiring
            )

            contexts.append(DeviceContext(
                index          = index,
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

        shell = QShell(
            kernel            = kernel,
            global_config     = global_config,
            global_provenance = global_provenance
        )
        shell.cmdloop()

    def _build_router(self, global_config):
        if global_config["router"] == "round_robin":
            return RoundRobinRouter()
        return NoiseRouter(
            queue_weight = global_config["router_queue_weight"],
            noise_weight = global_config["router_noise_weight"]
        )