from abc import ABC, abstractmethod
from kernel.process.lifecycle import JobStates
class BaseScheduler(ABC):
    def __init__(self, memory_manager, process_table):
        self.memory_manager = memory_manager
        self.process_table = process_table
        self.queue = [] # List of QCBs

    def enqueue(self, qcb):
        """Adds a job to the waitlist."""
        self.queue.append(qcb)

    @abstractmethod
    def schedule(self):
        """
        Logic to decide which job(s) from the queue to allocate next.
        Must be implemented by subclasses.
        """
        pass

    def _attempt_allocation(self, qcb):
        """Helper to try and move a job from READY to RUNNING."""
        try:
            mapping = self.memory_manager.allocate(qcb.circuit)
            qcb.v2p_map = mapping
            qcb.state = JobStates.RUNNING
            return True
        except Exception:
            # Allocation failed (likely due to insufficient free qubits)
            print(f"[Scheduler] Allocation failed for job {qcb.job_id}")
            return False
        
    def is_batch(self):
        return False
