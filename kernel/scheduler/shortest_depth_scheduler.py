'''
Tags: Alt

ShortestDepthScheduler (SDF) — Shallowest circuit first.

Re-sorts the queue by circuit depth (ascending) every cycle and
attempts the shallowest job. Shallow circuits complete faster and
free qubits sooner — better throughput under mixed-depth workloads,
and less decoherence exposure in the NISQ era. REJECTED jobs are
removed and skipped; a WAITING head ends the cycle.
'''

from .base_scheduler import BaseScheduler
from kernel.process.lifecycle import JobStates

class ShortestDepthScheduler(BaseScheduler):

    # Human-readable name shown by qconfig. Any registered component
    # may define one; the registry falls back to the class name.
    LABEL = "Shortest Depth First"
    def schedule(self):
        """
        Sorts the queue so that circuits with the lowest depth
        are attempted first. REJECTED (unsatisfiable) jobs are
        removed and skipped; a WAITING head ends the cycle.

        Returns a list of processed jobs (rejected and/or one
        dispatched), or None if nothing was processed.
        """
        if not self.queue:
            return None

        # Sort the queue by circuit depth (Ascending)
        # We do this every schedule cycle in case new jobs were added
        self.queue.sort(key=lambda qcb: qcb.circuit.get_depth())

        processed = []

        while self.queue:
            qcb = self.queue[0]

            if self._attempt_allocation(qcb):
                processed.append(self.queue.pop(0))
                return processed

            if qcb.state == JobStates.REJECTED:
                processed.append(self.queue.pop(0))
                continue

            break

        return processed or None