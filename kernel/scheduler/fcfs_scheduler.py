'''
Tags: Alt

FCFSScheduler — First come, first served.

Strict submission order. A WAITING head (feasible but blocked on
resources) blocks the queue — that is FCFS semantics. A REJECTED
head does not: unsatisfiable jobs are removed and the next job is
attempted in the same cycle.
'''

from .base_scheduler import BaseScheduler
from kernel.process.lifecycle import JobStates

class FCFSScheduler(BaseScheduler):

    # Human-readable name shown by qconfig. Any registered component
    # may define one; the registry falls back to the class name.
    LABEL = "First Come First Served"
    def schedule(self):
        """
        Processes the queue in strict order.

        A WAITING head (blocked on resources) still blocks the queue —
        that is FCFS semantics. A REJECTED head (unsatisfiable) does
        not: it is removed and the next job is attempted in the same
        cycle. A job that can never be served isn't in line.

        Returns a list of processed jobs (rejected and/or one
        dispatched), or None if nothing was processed.
        """
        processed = []

        while self.queue:
            qcb = self.queue[0]

            if self._attempt_allocation(qcb):
                processed.append(self.queue.pop(0))
                return processed

            if qcb.state == JobStates.REJECTED:
                processed.append(self.queue.pop(0))
                continue

            # WAITING — head-of-line blocking preserved
            break

        return processed or None