'''
Tags: Main

DeviceContext — The federation unit of a multi-device DevQ system.

One DeviceContext exists per attached device, bundling everything that
is private to that device: the device itself, its MemoryManager (which
owns the device's QubitPool and allocator instance), its scheduler
instance, and its resolved per-device configuration.

This is what makes per-device config real — d0 can run PackingScheduler
over a NoiseGraphAllocator while d1 runs FCFS over Static. The router
decides WHICH context a job enters; from that point on the existing
single-device machinery (scheduler, allocator, pool) runs unchanged.

Classical analogue: a node in a cluster. The router is the cluster
scheduler; each DeviceContext is a node with its own local kernel
mechanisms.
'''


class DeviceContext:

    def __init__(self, index, device, memory_manager, scheduler,
                 config, provenance, name=None):
        '''
        Args:
            index:          stable device index (d0..dn, add order)
            name:           optional user-supplied device name — an
                            alias for the index, usable anywhere dN is
                            accepted. None means the device is referred
                            to by index alone.
            device:         QuantumDevice
            memory_manager: MemoryManager bound to this device
            scheduler:      scheduler INSTANCE bound to this context
            config:         resolved per-device config dict
            provenance:     per-key source labels for qconfig
        '''
        self.index          = index
        self.name           = name
        self.device         = device
        self.memory_manager = memory_manager
        self.scheduler      = scheduler
        self.config         = config
        self.provenance     = provenance

        # Jobs currently RUNNING on this device — maintained by the
        # kernel (incremented at dispatch, decremented at resolution).
        # Router input: queue_depth() + running_jobs = load pressure.
        self.running_jobs   = 0

    @property
    def label(self):
        '''
        Display form for this device: "nairobi (d1)" when the user named
        it, plain "d1" when they did not. Every user-facing print uses
        this, so naming a device changes output everywhere at once.
        '''
        return f"{self.name} (d{self.index})" if self.name else f"d{self.index}"

    @property
    def ref(self):
        '''Canonical index reference, "d1" — used where a bare token is
        wanted regardless of naming (qps columns, error messages).'''
        return f"d{self.index}"

    @property
    def shots(self):
        return self.config["shots"]

    def queue_depth(self):
        '''Number of jobs waiting in this context's scheduler queue.'''
        return len(self.scheduler.queue)

    def __repr__(self):
        return (f"DeviceContext({self.label}, {self.device.name}, "
                f"{type(self.device.provider).__name__})")