from .base_scheduler import BaseScheduler

class PackingScheduler(BaseScheduler):
    def schedule(self):
        """
        Attempts to pack as many jobs as possible onto the device
        in a single cycle.
        """
        if not self.queue:
            return []

        # Optional: Sort by depth first to maximize successful packing
        self.queue.sort(key=lambda qcb: qcb.circuit.get_depth())

        scheduled_jobs = []
        remaining_queue = []

        for qcb in self.queue:
            # Try to allocate using the current state of the QubitPool
            if self._attempt_allocation(qcb):
                scheduled_jobs.append(qcb)
            else:
                # If it doesn't fit, keep it in the queue for the next cycle
                remaining_queue.append(qcb)

        self.queue = remaining_queue
        return scheduled_jobs