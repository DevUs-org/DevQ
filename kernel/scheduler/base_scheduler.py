'''
Tags: Main

BaseScheduler — Abstract base class for all job schedulers.

Defines the enqueue/schedule contract the kernel depends on.
schedule() returns the jobs processed in a cycle — dispatched
(RUNNING) and/or rejected (REJECTED). _attempt_allocation() provides
the shared allocation-and-classification step: transient failure →
WAITING; unsatisfiable per the allocator's feasible() → REJECTED
(terminal; the caller removes it from the queue).
'''
from abc import ABC, abstractmethod
from kernel.process.lifecycle import JobStates

from kernel.sweep import Sweepable


class BaseScheduler(Sweepable, ABC):
    '''
    Base for all schedulers. Inherits the Sweepable contract at the same
    scope as routers and allocators: a scheduler that scores its queue on a
    tunable parameter (dispatch order, a cost-aware policy) implements the
    three sweep hooks and becomes explainable and weight-sweepable for
    free, exactly like NoiseRouter.

    The shipped schedulers (FCFS, SDF, Packing) have no scoring parameter —
    FCFS is arrival order, SDF is circuit depth, Packing is greedy
    geometry — so they DO NOT implement the hooks and report not-sweepable
    honestly, the same silence as RoundRobinRouter. The contract is present
    so the first scored scheduler (the QOS baseline, Phase 5.6) is
    sweepable without any base-class change; nothing here fakes a parameter
    to exercise it.
    '''

    def __init__(self, memory_manager, process_table):
        self.memory_manager = memory_manager
        self.process_table  = process_table
        self.queue          = []  # List of QCBs

    def enqueue(self, qcb):
        '''Adds a job to the scheduler queue.'''
        self.queue.append(qcb)

    @abstractmethod
    def schedule(self):
        '''
        Logic to decide which job(s) from the queue to allocate next.
        Must be implemented by subclasses.

        Returns processed jobs — dispatched (RUNNING) and/or rejected
        (REJECTED). Callers must not assume every returned job was
        dispatched; check qcb.state.
        '''
        pass

    def _attempt_allocation(self, qcb):
        '''
        Try to allocate qubits for a job.
        On success: sets v2p_map and state to RUNNING, returns True.
        On failure: classifies the failure and returns False —
          - unsatisfiable (could never be allocated on this device
            under the job's thresholds): state REJECTED, reason stored
            on the QCB. Terminal; caller must remove it from the queue.
          - otherwise (transient resource contention): state WAITING.
        '''
        try:
            mapping     = self.memory_manager.allocate(
                qcb.circuit,
                max_qubit_error=qcb.max_qubit_error,
                max_edge_error=qcb.max_edge_error
            )
            qcb.v2p_map = mapping
            qcb.state   = JobStates.RUNNING
            # Capture the allocation decision onto THIS job immediately —
            # the allocator's stash is per-instance and would be clobbered
            # by the next job's allocate(), so a batch scheduler that
            # allocates several jobs before any dispatch must pin each
            # job's decision here, at the call that produced it. None for a
            # non-scoring allocator. The kernel reads this on dispatch.
            qcb.alloc_decision = getattr(
                self.memory_manager.allocator, "_last_decision", None)
            return True
        except Exception:
            reason = self.memory_manager.unsatisfiable_reason(
                qcb.circuit,
                max_qubit_error=qcb.max_qubit_error,
                max_edge_error=qcb.max_edge_error
            )
            if reason:
                qcb.state         = JobStates.REJECTED
                qcb.reject_reason = reason
            else:
                qcb.state = JobStates.WAITING
            return False

    def is_batch(self):
        return False