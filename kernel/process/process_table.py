'''
Tags: Main

ProcessTable — Registry of every job DevQ has seen.

Creates QCBs with monotonically increasing job IDs and retains them
for the lifetime of the session — including terminal jobs (FINISHED,
FAILED, REJECTED), so qps always shows the full execution history.

Job IDs are global across all attached devices — a job's identity is
system-wide; which device it ran on is recorded on the QCB
(device_index) by the router.
'''

from .qcb import QCB

class ProcessTable:
    def __init__(self):
        self.jobs = {}
        self.next_pid = 1

    def create_job(self, circuit, max_qubit_error=None, max_edge_error=None,
                   exec_on=None, no_exec_on=None):
        pid = self.next_pid
        job = QCB(pid, circuit,
                  max_qubit_error=max_qubit_error,
                  max_edge_error=max_edge_error,
                  exec_on=exec_on,
                  no_exec_on=no_exec_on)
        self.jobs[pid] = job
        self.next_pid += 1

        return job

    def list_jobs(self):
        return list(self.jobs.values())