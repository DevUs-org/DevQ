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

        # ── Timing ────────────────────────────────────────────────────
        # TWO clocks, deliberately. They answer different questions and
        # neither substitutes for the other.
        #
        # *_seq   monotonic event-sequence positions (see Kernel._emit).
        #         DETERMINISTIC: identical seeded runs make identical
        #         decisions in identical order, so these are stable and
        #         comparable across runs. Use them to compare WHAT
        #         happened.
        #
        # *_at    wall-clock (time.time()) at each transition. NOT
        #         deterministic and not meant to be — completion order
        #         belongs to the executor, and on real hardware to the
        #         provider's queue. Use them to measure HOW LONG things
        #         took: queue latency, throughput, utilisation.
        #
        # A determinism check compares *_seq and ignores *_at; a metrics
        # pass does the reverse.
        #
        # CAVEAT for Phase 5.3: under simulation these measure Aer on
        # the host CPU, not quantum runtime. They are valid for
        # comparing policies under identical conditions, and must not be
        # reported as device timings.
        self.submitted_seq  = None
        self.dispatched_seq = None
        self.resolved_seq   = None

        self.submitted_at   = None
        self.dispatched_at  = None
        self.resolved_at    = None

    # ── Derived timings ───────────────────────────────────────────────────────
    # None when the job has not reached the relevant transition, so a
    # metrics pass can skip incomplete jobs rather than treating a
    # missing timestamp as zero.

    @property
    def queue_latency(self):
        '''Seconds between submission and dispatch.'''
        if self.submitted_at is None or self.dispatched_at is None:
            return None
        return self.dispatched_at - self.submitted_at

    @property
    def execution_time(self):
        '''Seconds between dispatch and resolution.'''
        if self.dispatched_at is None or self.resolved_at is None:
            return None
        return self.resolved_at - self.dispatched_at

    @property
    def turnaround_time(self):
        '''Seconds from submission to resolution — the user-visible wait.'''
        if self.submitted_at is None or self.resolved_at is None:
            return None
        return self.resolved_at - self.submitted_at

    def __repr__(self):
        return f"QCB(job_id={self.job_id}, state={self.state.value})"