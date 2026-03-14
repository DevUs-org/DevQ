'''
Tags: Main

Create the QCB or the process control block for the jobs.
'''

from .lifecycle import JobStates

class QCB:
    def __init__(self, job_id, circuit, v2p_map = None):
        self.job_id = job_id
        self.circuit = circuit
        self.v2p_map = v2p_map or {}
        self.state = JobStates.READY