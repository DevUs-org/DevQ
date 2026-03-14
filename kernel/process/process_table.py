'''
Tags: Main

Creates the Job Process Table.
'''

from .qcb import QCB

class ProcessTable:
    def __init__(self):
        self.jobs = {}
        self.next_pid = 1

    def create_job(self, circuit):
        pid = self.next_pid
        job = QCB(pid, circuit)
        self.jobs[pid] = job
        self.next_pid += 1

        return job
    
    def list_jobs(self):
        return list(self.jobs.values())