from kernel.process.process_table import ProcessTable
from kernel.memory.memory_manager import MemoryManager
from kernel.scheduler.fcfs_scheduler import FCFSScheduler
from kernel.scheduler.depth_scheduler import DepthScheduler

class Kernel:
    def __init__(self, device):
        self.device = device
        self.process_table = ProcessTable()
        self.memory_manager = MemoryManager(device)
        
        # Initialize the Scheduler (Starting with FCFS)
        self.scheduler = FCFSScheduler(self.memory_manager, self.process_table)
        # self.scheduler = DepthScheduler(self.memory_manager, self.process_table)

    def submit_job(self, circuit):
        qcb = self.process_table.create_job(circuit)
        # We no longer allocate here! The scheduler will do it.
        self.scheduler.enqueue(qcb) 
        return qcb

    def get_job_mapping(self, job_id):
        job = self.process_table.jobs.get(job_id)
        if job is None:
            return None
        return job.v2p_map # Changed from virtual_to_physical_map

    def step(self):
        """
        The 'clock tick'. Calls the scheduler to move jobs from 
        READY to RUNNING.
        """
        return self.scheduler.schedule()

    # --- Passthrough methods for Shell ---
    def list_jobs(self):
        return self.process_table.list_jobs()
    
    def get_topology(self):
        return self.device.coupling_map
    
    def get_free_qubits(self):
        return self.memory_manager.pool.free_qubits
    
    def get_job_mapping(self, job_id):
        job = self.process_table.jobs.get(job_id)
        return job.v2p_map if job else None
    
    def get_error_map(self):
        return self.device.error_map