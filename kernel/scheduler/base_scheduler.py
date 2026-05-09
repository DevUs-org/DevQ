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
        '''
        pass

    def _attempt_allocation(self, qcb):
        '''
        Try to allocate qubits for a job.
        On success: sets v2p_map and state to RUNNING, returns True.
        On failure: sets state to WAITING, returns False.
        '''
        try:
            mapping     = self.memory_manager.allocate(qcb.circuit)
            qcb.v2p_map = mapping
            qcb.state   = JobStates.RUNNING
            return True
        except Exception:
            qcb.state = JobStates.WAITING
            return False

    def is_batch(self):
        return False