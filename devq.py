'''
Tags: Main

DevQ — Single entry point for the DevQ quantum execution system.

Resolves configuration, builds the allocator and scheduler instances,
then on start() wires everything into the Kernel and launches QShell.

Usage:
    from devq import DevQ
    from hardware.providers.devq.devq_simulated_provider import DevQSimulatedProvider

    # Default config (DevQ core defaults + provider preferences)
    DevQ(DevQSimulatedProvider().get_device("random", 10)).start()

    # With user config file
    DevQ(DevQSimulatedProvider().get_device("random", 10), "~/devq.config.json").start()

Config file format (JSON):
    {
        "scheduler":  "packing",      // fcfs | sdf | packing
        "allocator":  "noise_graph",  // static | graph | noise_graph
        "shots":      1024
    }

Configuration priority (later overrides earlier):
    1. DevQ core defaults
    2. Provider preferred_config()
    3. User local config file
'''

from hardware.device_loader import load_device
from kernel.kernel import Kernel
from shell.qshell import QShell
from config.config_loader import load_config

from kernel.scheduler.fcfs_scheduler import FCFSScheduler
from kernel.scheduler.shortest_depth_scheduler import ShortestDepthScheduler
from kernel.scheduler.packing_scheduler import PackingScheduler
from kernel.memory.allocators.static_allocator import StaticAllocator
from kernel.memory.allocators.graph_allocator import GraphAllocator
from kernel.memory.allocators.noise_graph_allocator import NoiseGraphAllocator

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


class DevQ:

    def __init__(self, device, config_path=None):
        '''
        Resolve config and build allocator + scheduler instances.

        Args:
            device:      QuantumDevice — from any provider's get_device()
            config_path: optional path to a JSON config file
        '''
        self._device     = load_device(device)
        self._config, self._provenance = load_config(
            self._device.provider, config_path
        )

        # Resolve instances from merged config
        self._allocator       = _ALLOCATOR_MAP[self._config["allocator"]]()
        self._scheduler_class = _SCHEDULER_MAP[self._config["scheduler"]]
        self._shots           = self._config["shots"]

    def start(self):
        '''
        Wire allocator and scheduler into the Kernel, attach QShell, start session.
        '''
        kernel = Kernel(
            device          = self._device,
            scheduler_class = self._scheduler_class,
            allocator       = self._allocator,
            shots           = self._shots
        )
        shell = QShell(
            kernel     = kernel,
            config     = self._config,
            provenance = self._provenance
        )
        shell.cmdloop()