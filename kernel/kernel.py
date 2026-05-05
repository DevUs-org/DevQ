from kernel.process.process_table import ProcessTable
from kernel.memory.memory_manager import MemoryManager
from kernel.process.lifecycle import JobStates
from kernel.scheduler.packing_scheduler import PackingScheduler

class Kernel:
    def __init__(self, device):
        self.device = device
        self.process_table = ProcessTable()
        self.memory_manager = MemoryManager(device)
        self.scheduler = PackingScheduler(self.memory_manager, self.process_table) # TODO: make configurable, sdf, fcfs or packing

    def submit_job(self, circuit):
        qcb = self.process_table.create_job(circuit)
        self.scheduler.enqueue(qcb) 
        return qcb

    def get_job_mapping(self, job_id):
        job = self.process_table.jobs.get(job_id)
        if job is None:
            return None
        return job.v2p_map

    def step(self):
        jobs = self.scheduler.schedule()

        if not jobs:
            return []

        jobs = jobs if isinstance(jobs, list) else [jobs]

        if self.scheduler.is_batch():
            for job in jobs:
                print(f"[*] Job {job.job_id} STARTED. Mapping: {job.v2p_map}")

            for job in jobs:
                print(f"[Kernel] Executing Job {job.job_id} on {job.v2p_map}")

            for job in jobs:
                job.state = JobStates.FINISHED
                self.memory_manager.free(job.v2p_map.values())
                print(f"[Kernel] Job {job.job_id} completed")

        else:
            for job in jobs:
                self.execute(job)

        return jobs
    
    def execute(self, qcb):
        print(f"[*] Job {qcb.job_id} STARTED. Mapping: {qcb.v2p_map}")
        print(f"[Kernel] Executing Job {qcb.job_id} on {qcb.v2p_map}")
        qcb.state = JobStates.FINISHED
        self.memory_manager.free(qcb.v2p_map.values())
        print(f"[Kernel] Job {qcb.job_id} completed")

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
    
    def get_edge_error_map(self):
        return self.device.edge_error_map