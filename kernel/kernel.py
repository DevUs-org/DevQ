from kernel.process.process_table import ProcessTable
from kernel.memory.memory_manager import MemoryManager

class Kernel:

    def __init__(self, device):
        self.device = device

        self.process_table = ProcessTable()
        self.memory_manager = MemoryManager(device)

    def submit_job(self, circuit):
        qcb = self.process_table.create_job(circuit)
        mapping = self.memory_manager.allocate(circuit)
        qcb.virtual_to_physical_map = mapping

        return qcb

    def list_jobs(self):
        return self.process_table.list_jobs()
    
    def get_topology(self):
        return self.device.coupling_map
    
    def get_free_qubits(self):
        return self.memory_manager.pool.available()