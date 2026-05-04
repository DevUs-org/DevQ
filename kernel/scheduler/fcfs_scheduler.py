from .base_scheduler import BaseScheduler

class FCFSScheduler(BaseScheduler):
    def schedule(self):
        """
        Processes the queue in strict order. 
        If the first job cannot be allocated, it blocks the queue.
        """
        if not self.queue:
            return None

        # Take the oldest job
        qcb = self.queue[0]

        if self._attempt_allocation(qcb):
            # Successfully scheduled! Remove from queue.
            return self.queue.pop(0)
        
        return None
