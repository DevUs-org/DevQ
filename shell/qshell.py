'''
Tags: Main

DevQ Shell definition file
'''

import cmd
import os
import readline
import atexit
from circuits.qasm_loader import load_qasm
from config.config_loader import SCHEDULER_LABELS, ALLOCATOR_LABELS
from shell.parser import parse_job_args


class QShell(cmd.Cmd):
    prompt = "devq> "

    def __init__(self, kernel, config=None, provenance=None):
        super().__init__()
        self.kernel     = kernel
        self._config    = config     or {}
        self._provenance = provenance or {}
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

    def emptyline(self):
        pass

    def default(self, line):
        if line.strip() == "!!":
            if not self._last_command:
                print("No previous command to repeat.")
                return
            print(self._last_command)
            return self.onecmd(self._last_command)
        return super().default(line)

    def do_exit(self, arg):
        print("Exiting DevQ.")
        return True

    def do_EOF(self, arg):
        print()
        return self.do_exit(arg)

    # ── Job commands ──────────────────────────────────────────────────────────

    def do_qrun(self, arg):
        try:
            if not arg:
                print("Usage: qrun <qasm_file> [--max-qubit-error=X] [--max-edge-error=Y]")
                return

            specs = parse_job_args(arg)

            if len(specs) > 1:
                print("[DevQ Error] qrun accepts exactly one job. "
                      "Use qsubmit + qrunpack for multiple jobs.")
                return

            spec    = specs[0]
            circuit = load_qasm(spec.file_path)
            qcb     = self.kernel.submit_job(
                circuit,
                max_qubit_error=spec.max_qubit_error,
                max_edge_error=spec.max_edge_error
            )
            print(f"Job {qcb.job_id} submitted to queue.")

            self.kernel.run_job(qcb)

            if qcb.state.value == "FINISHED":
                print(f"[+] Job {qcb.job_id} FINISHED.")
            elif qcb.state.value == "FAILED":
                print(f"[-] Job {qcb.job_id} failed. See above for details.")
            elif qcb.state.value == "WAITING":
                print(f"[~] Job {qcb.job_id} is WAITING for resources.")

        except Exception as e:
            print(f"[DevQ Error] {e}")

    def do_qrunpack(self, arg):
        try:
            total = 0

            while True:
                jobs = self.kernel.step()

                if not jobs:
                    break

                for job in jobs:
                    if job.state.value == "FINISHED":
                        print(f"[+] Job {job.job_id} FINISHED. Counts: {job.result.counts}")
                    elif job.state.value == "FAILED":
                        print(f"[-] Job {job.job_id} FAILED. Error: {job.result.error}")
                    total += 1

            if total == 0:
                print("No jobs in queue.")

        except Exception as e:
            print(f"[DevQ Error] {e}")

    # ── Inspection commands ───────────────────────────────────────────────────

    def do_qps(self, arg):
        try:
            jobs = self.kernel.list_jobs()

            if not jobs:
                print("No jobs.")
                return

            for job in jobs:
                print(f"{job.job_id} | {job.state.value}")

        except Exception as e:
            print(f"[DevQ Error] {e}")

    def do_qmap(self, arg):
        try:
            job_id = int(arg.strip())
        except ValueError:
            print("Invalid job id.")
            return

        mapping = self.kernel.get_job_mapping(job_id)

        if mapping is None:
            print(f"Job {job_id} does not exist.")
            return

        print(f"\nJob {job_id} mapping\n")
        print("virtual → physical\n")
        for v, p in mapping.items():
            print(f"  {v} → {p}")
        print()

    def do_qmem(self, arg):
        free_set = self.kernel.get_free_qubits()
        total    = self.kernel.device.num_qubits

        print()
        for qubit in range(total):
            status = "[]" if qubit in free_set else "[X]"
            print(f"  {qubit} {status}")
        print()

    def do_qtopology(self, arg):
        topology = self.kernel.get_topology()
        total    = self.kernel.device.num_qubits

        if not arg:
            print("\nDevice topology:")
            for q1, q2 in topology:
                print(f"  {q1} -- {q2}")
            print()
            return

        try:
            requested = [int(i) for i in arg.split()]

            print("\nRequested device topology:")
            for q in requested:
                if q < 0 or q >= total:
                    print(f"  {q} -- Doesn't exist")

            for q1, q2 in topology:
                if q1 in requested or q2 in requested:
                    print(f"  {q1} -- {q2}")
            print()

        except ValueError:
            print("Invalid argument for qtopology.")

    def do_qerrors(self, arg):
        flag = arg.strip()[0] if arg.strip() else 'b'

        if flag not in ['e', 'q', 'b']:
            print("Invalid flag. Use: q (qubit), e (edge), b (both, default).")
            return

        if flag in ('q', 'b'):
            print("\nQubit Error Map:\n")
            for q, err in sorted(self.kernel.get_error_map().items()):
                print(f"  {q} -> {err:.4f}")

        if flag in ('e', 'b'):
            print("\nEdge Error Map:\n")
            for edge, err in sorted(self.kernel.get_edge_error_map().items()):
                print(f"  {edge} -> {err:.4f}")

        print()

    def do_qconfig(self, arg):
        '''Display the active DevQ configuration and the source of each value.'''
        device   = self.kernel.device
        provider = type(device.provider).__name__

        print(f"\n  provider   =  {provider}")
        print(f"  device     =  {device.name}  ({device.num_qubits} qubits)")
        print()

        rows = [
            ("scheduler", SCHEDULER_LABELS.get(self._config.get("scheduler", ""), "")),
            ("allocator",  ALLOCATOR_LABELS.get(self._config.get("allocator",  ""), "")),
            ("shots",      ""),
        ]

        for key, label in rows:
            val    = self._config.get(key, "")
            source = self._provenance.get(key, "")
            if label:
                print(f"  {key:<12} =  {str(val):<14}  [{label}]  source: {source}")
            else:
                print(f"  {key:<12} =  {str(val):<14}  source: {source}")

        print()