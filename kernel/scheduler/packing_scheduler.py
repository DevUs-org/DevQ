from kernel.scheduler.base_scheduler import BaseScheduler
from kernel.process.lifecycle import JobStates

class PackingScheduler(BaseScheduler):

    def schedule(self):
        if not self.queue:
            return []

        # same ordering as SDF
        self.queue.sort(key=lambda q: q.circuit.get_depth())

        batch = []
        temp_free = set(self.memory_manager.pool.free_qubits)

        for qcb in list(self.queue):
            mapping = self._try_allocate_temp(qcb, temp_free)

            if mapping:
                qcb.v2p_map = mapping
                batch.append(qcb)

                # reserve qubits in temp pool
                for p in mapping.values():
                    temp_free.remove(p)

        # commit allocations for selected jobs
        for qcb in batch:
            self.memory_manager.allocate(qcb.circuit)
            qcb.state = JobStates.RUNNING
            self.queue.remove(qcb)

        return batch

    def _try_allocate_temp(self, qcb, temp_free):
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