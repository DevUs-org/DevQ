'''
Tags: Main

Creates the Job Process Table.
'''

from .qcb import QCB

class ProcessTable:
    def __init__(self):
        self.jobs = {}
        self.next_pid = 1

    def create_job(self, circuit, max_qubit_error=None, max_edge_error=None):
        pid = self.next_pid
        job = QCB(pid, circuit,
                  max_qubit_error=max_qubit_error,
                  max_edge_error=max_edge_error)
        self.jobs[pid] = job
        self.next_pid += 1

        return job

    def list_jobs(self):
        return list(self.jobs.values())