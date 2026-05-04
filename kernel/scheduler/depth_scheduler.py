from kernel.scheduler.base_scheduler import BaseScheduler

class DepthScheduler(BaseScheduler):
    def schedule(self):
        """
        Sorts the queue so that circuits with the lowest depth 
        are attempted first.
        """
        if not self.queue:
            return None

        # Sort the queue by circuit depth (Ascending)
        # We do this every schedule cycle in case new jobs were added
        self.queue.sort(key=lambda qcb: qcb.circuit.get_depth())

        # Peek at the top (shortest) job
        qcb = self.queue[0]

        if self._attempt_allocation(qcb):
            return self.queue.pop(0)
        
        return None
        