'''
Tags: Main

Packing Scheduler — Greedy circuit packing scheduler.

Sorts the queue by circuit depth (ascending) then greedily batches
as many non-overlapping circuits as possible into a single execution
round using a temporary qubit reservation pool. Multiple circuits
execute simultaneously on disjoint qubit sets, maximising device
utilisation per scheduling cycle.

Jobs not picked in a given cycle are set to WAITING, indicating
they are queued but blocked on resource availability.
'''

from kernel.scheduler.base_scheduler import BaseScheduler
from kernel.process.lifecycle import JobStates


class PackingScheduler(BaseScheduler):

    def schedule(self):
        if not self.queue:
            return []

        # Sort by depth ascending — shallower circuits packed first
        self.queue.sort(key=lambda qcb: qcb.circuit.get_depth())

        batch     = []
        temp_free = set(self.memory_manager.pool.free_qubits)

        for qcb in list(self.queue):
            mapping = self._try_allocate_temp(qcb, temp_free)

            if mapping:
                qcb.v2p_map = mapping
                batch.append(qcb)

                # Reserve qubits in temp pool so subsequent
                # jobs in this cycle don't overlap
                for p in mapping.values():
                    temp_free.remove(p)

        # Commit allocations for picked jobs directly via pool —
        # do NOT re-run the allocator here, the temp pass already
        # decided which qubits to use
        for qcb in batch:
            self.memory_manager.pool.allocate(list(qcb.v2p_map.values()))
            qcb.state = JobStates.RUNNING
            self.queue.remove(qcb)

        # Jobs not picked this cycle are blocked on resource availability
        for qcb in self.queue:
            qcb.state = JobStates.WAITING

        return batch

    def _try_allocate_temp(self, qcb, temp_free):
        '''
        Attempt allocation against a temporary qubit pool without
        touching the real pool. Returns the mapping on success,
        None on failure.
        '''
        class TempPool:
            def __init__(self, free):
                self.free_qubits = free

            def allocate(self, qubits):
                for q in qubits:
                    if q not in self.free_qubits:
                        raise Exception("Not enough qubits")
                for q in qubits:
                    self.free_qubits.remove(q)

        temp_pool = TempPool(set(temp_free))

        try:
            mapping = self.memory_manager.allocator.allocate(
                qcb.circuit,
                self.memory_manager.device,
                temp_pool
            )
            return mapping
        except Exception:
            return None

    def is_batch(self):
        return True