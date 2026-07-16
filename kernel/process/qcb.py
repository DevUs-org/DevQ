'''
Tags: Main

QCB (Quantum Control Block) — Process control block for a quantum job.

Analogous to a PCB in classical operating systems.
Tracks everything DevQ knows about a submitted job from submission
through routing, allocation, execution and completion.
'''

from .lifecycle import JobStates


class QCB:
    def __init__(self, job_id, circuit, v2p_map=None,
                 max_qubit_error=None, max_edge_error=None,
                 exec_on=None, no_exec_on=None):
        self.job_id  = job_id
        self.circuit = circuit
        self.v2p_map = v2p_map or {}
        self.state   = JobStates.READY

        # Job-level noise thresholds (None = no filtering)
        self.max_qubit_error = max_qubit_error
        self.max_edge_error  = max_edge_error

        # Job-level device constraints (None = unconstrained):
        #   exec_on    — allow-list: the job may ONLY run on these device
        #                indices; infeasible on all of them → REJECTED,
        #                never re-routed elsewhere.
        #   no_exec_on — deny-list: the job is never routed to these.
        # Mutually exclusive; enforced by the parser.
        self.exec_on    = exec_on
        self.no_exec_on = no_exec_on

        # Set by the router when the job is bound to a device
        # (sticky — a routed job is never re-routed).
        self.device_index = None

        # Set when the job is classified REJECTED — a human-readable
        # reason string (router- or allocator-generated).
        self.reject_reason = None

        # Set by the kernel when execution is dispatched
        self.future  = None

        # Set by the kernel once the future resolves
        self.result  = None

    def __repr__(self):
        return f"QCB(job_id={self.job_id}, state={self.state.value})"