'''
Tags: Main

DevQ Shell definition file
'''

import cmd
import os
import readline
import atexit
from circuits.qasm_loader import load_qasm

class QShell(cmd.Cmd):
    prompt = "devq> "
    def __init__(self, kernel):
        super().__init__()
        self.kernel = kernel
        self._last_command = None
        readline.parse_and_bind("tab: complete")
        self._history_file = os.path.expanduser("~/.devq_history")

        try:
            readline.read_history_file(self._history_file)
        except FileNotFoundError:
            pass

        atexit.register(self._save_history)

    def _save_history(self):
        try:
            readline.write_history_file(self._history_file)
        except Exception:
            pass

    def precmd(self, line):
        cmd_line = line.strip()
        if cmd_line and cmd_line != "!!":
            self._last_command = cmd_line
        return line

    def do_qrun(self, arg):
        try:
            if not arg:
                print("Usage: qrun <qasm_file>")
                return
            
            circuit = load_qasm(arg)
            qcb = self.kernel.submit_job(circuit)      
            print(f"Job {qcb.job_id} submitted")
            print(f"Allocated qubits: {qcb.v2p_map}")

        except Exception as e:
            print(f"[DevQ Error] {e}")

    def do_qps(self, arg):
        try:
            jobs = self.kernel.list_jobs()
            
            if not jobs:
                print("No jobs in queue.")
                return

            for job in jobs:
                print(f"{job.job_id} | {job.state.value}")

        except Exception as e:
            print(f"[DevQ Error] {e}")

    def do_exit(self, arg):
        print("Exiting DevQ.")
        return True

    def do_EOF(self, arg):
        print()
        return self.do_exit(arg)

    def default(self, line):
        if line.strip() == "!!":
            if not self._last_command:
                print("No previous command to repeat.")
                return
            print(self._last_command)
            return self.onecmd(self._last_command)
        return super().default(line)

    def emptyline(self):
        pass

    def do_qtopology(self, arg):
        topology = self.kernel.get_topology()
        total = self.kernel.device.num_qubits

        if not arg:
            print("\nDevice topology:")
            for q1, q2 in topology:
                print(f"{q1} -- {q2}")

            print()
            return
        
        try:
            requested = [int(i) for i in arg.split()]

            print("\nRequested device topology:")
            for q in requested:
                if q < 0 or q >= total:
                    print(f"{q} -- Doesn't exist")

            for q1, q2 in topology:
                if q1 in requested or q2 in requested:
                    print(f"{q1} -- {q2}")

            print()

        except ValueError:
            print("Invalid Argument for qtopology")


    def do_qmem(self, arg):
        free_set = self.kernel.get_free_qubits()
        total = self.kernel.device.num_qubits

        for qubit in range(total):
            if qubit in free_set:
                print(qubit, "[]")
            else:
                print(qubit, "[X]")

    def do_qmap(self, arg):
        try:
            job_id = int(arg.strip())
        except ValueError:
            print("Invalid job id")
            return

        mapping = self.kernel.get_job_mapping(job_id)

        if mapping is None:
            print(f"Job {job_id} does not exist")
            return

        print(f"\nJob {job_id} mapping\n")
        print("virtual → physical\n")

        for v, p in mapping.items():
            print(f"{v} → {p}")

        print()

    def do_qerrors(self, arg='b'):
        flag = arg.strip()[0] if len(arg) > 0 else 'b'
        if flag == 'q' or flag == 'b':
            print("\nQubit Error Map:\n")
            errors = self.kernel.get_error_map()
            for q in sorted(errors):
                print(f"{q} -> {errors[q]:.4f}")

        if flag == 'e' or flag == 'b':
            print("\nQubit Edge Map:\n")
            errors = self.kernel.get_edge_error_map()
            for q in sorted(errors):
                print(f"{q} -> {errors[q]:.4f}")

        if flag not in ['e', 'q', 'b']:
            print('Invalid flag given for qerrors.')

        print()

    def do_qrun(self, arg):
        # ... (loading logic) ...
        qcb = self.kernel.submit_job(circuit)      
        print(f"Job {qcb.job_id} submitted to queue.")
        
        # Trigger the kernel to try and schedule the job
        scheduled_job = self.kernel.step()
        if scheduled_job:
            print(f"Job {scheduled_job.job_id} allocated: {scheduled_job.v2p_map}")
        else:
            print(f"Job {qcb.job_id} is WAITING for resources.")