'''
Tags: Main

DevQ Shell definition file
'''

import cmd
import os
import readline
import atexit
from circuits.qasm_loader import load_qasm
from kernel.process.process_table import ProcessTable

class QShell(cmd.Cmd):
    prompt = "devq> "
    def __init__(self):
        super().__init__()
        self.process_table = ProcessTable()
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
            job = self.process_table.create_job(circuit)
            print(f"Job {job.job_id} submitted")

        except Exception as e:
            print(f"[DevQ Error] {e}")

    def do_qps(self, arg):
        try:
            jobs = self.process_table.list_jobs()
            
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