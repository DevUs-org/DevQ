'''
Tags: Main

QCB (Quantum Control Block) — Process control block for a quantum job.

Analogous to a PCB in classical operating systems.
Tracks everything DevQ knows about a submitted job from submission
through allocation, execution and completion.
'''

from .lifecycle import JobStates


class QCB:
    def __init__(self, job_id, circuit, v2p_map=None,
                 max_qubit_error=None, max_edge_error=None):
        self.job_id  = job_id
        self.circuit = circuit
        self.v2p_map = v2p_map or {}
        self.state   = JobStates.READY

        # Job-level noise thresholds (None = no filtering)
        self.max_qubit_error = max_qubit_error
        self.max_edge_error  = max_edge_error

        # Set when the job is classified unsatisfiable (state REJECTED):
        # a human-readable, allocator-generated reason string.
        self.reject_reason = None

        # Set by the kernel when execution is dispatched
        self.future  = None

        # Set by the kernel once the future resolves
        self.result  = None

    def __repr__(self):
        return f"QCB(job_id={self.job_id}, state={self.state.value})"