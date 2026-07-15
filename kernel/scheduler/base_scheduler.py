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


class BaseScheduler(ABC):
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