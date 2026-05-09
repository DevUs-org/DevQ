'''
Tags: Main

DevQ Kernel — Core execution engine.

Responsibilities:
  - Accept job submissions and create QCBs via the process table
  - Drive the scheduler via step()
  - Dispatch execution to the device provider via device.execute()
  - Resolve pending futures and update QCB state accordingly
  - Expose device metadata to QShell

The kernel never knows which provider backs the device.
It only speaks to QuantumDevice, which delegates to the provider.
Scheduler type (FCFS, SDF, Packing) is transparent to the kernel —
step() normalises all scheduler output to a list and handles it
identically regardless of batch or sequential scheduling.
'''

from kernel.process.process_table import ProcessTable
from kernel.memory.memory_manager import MemoryManager
from kernel.process.lifecycle import JobStates
from kernel.scheduler.packing_scheduler import PackingScheduler


class Kernel:
    def __init__(self, device):
        self.device         = device
        self.process_table  = ProcessTable()
        self.memory_manager = MemoryManager(device)
        self.scheduler      = PackingScheduler(self.memory_manager, self.process_table)  # TODO: make configurable
        self._pending       = []   # QCBs dispatched but not yet resolved

    # ── Job submission ────────────────────────────────────────────────────────

    def submit_job(self, circuit):
        '''Create a QCB and enqueue it in the scheduler. Does not execute.'''
        qcb = self.process_table.create_job(circuit)
        self.scheduler.enqueue(qcb)
        return qcb

    # ── Execution cycle ───────────────────────────────────────────────────────

    def step(self):
        '''
        One scheduling cycle:
          1. Resolve any pending futures from previous dispatches
          2. Ask the scheduler for the next job(s)
          3. Dispatch each job to the device for execution

        Scheduler type is transparent here — FCFS and SDF return a single
        QCB, Packing returns a list. Both are normalised to a list and
        handled identically.
        '''
        self._resolve_pending()

        jobs = self.scheduler.schedule()

        if not jobs:
            return []

        # Normalise — single QCB or list, handle identically
        jobs = jobs if isinstance(jobs, list) else [jobs]

        for job in jobs:
            self._execute(job)

        return jobs
    
    def run_job(self, qcb):
        '''
        Immediate priority execution for a single job, bypassing the
        scheduler queue. Used by qrun to execute a job ahead of any
        queued jobs. If allocation fails, job stays WAITING in the queue
        for qrunpack to pick up later.
        '''
        try:
            mapping     = self.memory_manager.allocate(qcb.circuit)
            qcb.v2p_map = mapping
            qcb.state   = JobStates.RUNNING
            self.scheduler.queue.remove(qcb)
            self._execute(qcb)
            self._resolve_pending()
        except Exception:
            qcb.state = JobStates.WAITING

    def _execute(self, qcb):
        '''
        Dispatch a single job to the device and store the future on the QCB.
        '''
        print(f"[Kernel] Dispatching job {qcb.job_id} → qubits {qcb.v2p_map}")
        qcb.future = self.device.execute(qcb.circuit, qcb.v2p_map)
        qcb.state  = JobStates.RUNNING
        self._pending.append(qcb)

    def _resolve_pending(self):
        '''
        Check all pending futures and finalise any that are done.
        Frees allocated qubits and sets final job state on completion.
        '''
        still_pending = []

        for qcb in self._pending:
            if qcb.future and qcb.future.done():
                result     = qcb.future.result()
                qcb.result = result

                if result.success:
                    qcb.state = JobStates.FINISHED
                    self.memory_manager.free(list(qcb.v2p_map.values()))
                    print(f"[Kernel] Job {qcb.job_id} FINISHED. Counts: {result.counts}")
                else:
                    qcb.state = JobStates.FAILED
                    self.memory_manager.free(list(qcb.v2p_map.values()))
                    print(f"[Kernel] Job {qcb.job_id} FAILED. Error: {result.error}")
            else:
                still_pending.append(qcb)

        self._pending = still_pending

    # ── QShell API ────────────────────────────────────────────────────────────

    def list_jobs(self):
        return self.process_table.list_jobs()

    def get_job_mapping(self, job_id):
        job = self.process_table.jobs.get(job_id)
        return job.v2p_map if job else None

    def get_job_result(self, job_id):
        job = self.process_table.jobs.get(job_id)
        return job.result if job else None

    def get_topology(self):
        return self.device.coupling_map

    def get_free_qubits(self):
        return self.memory_manager.pool.free_qubits

    def get_error_map(self):
        return self.device.error_map

    def get_edge_error_map(self):
        return self.device.edge_error_map