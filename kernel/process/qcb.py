'''
Tags: Main

QCB (Quantum Control Block) — Process control block for a quantum job.

Analogous to a PCB in classical operating systems.
Tracks everything DevQ knows about a submitted job from submission
through allocation, execution and completion.
'''

from .lifecycle import JobStates


class QCB:
    def __init__(self, job_id, circuit, v2p_map=None):
        self.job_id  = job_id
        self.circuit = circuit
        self.v2p_map = v2p_map or {}
        self.state   = JobStates.READY

        # Set by the kernel when execution is dispatched
        self.future  = None

        # Set by the kernel once the future resolves
        self.result  = None

    def __repr__(self):
        return f"QCB(job_id={self.job_id}, state={self.state.value})"