'''
Tags: Main

QShell — Interactive frontend to the DevQ kernel, built on cmd.

Commands deliberately mirror classical OS tools: qps ≈ ps, qmem ≈ free,
qerrors ≈ iostat, qdevices ≈ lscpu for the cluster. The shell is a pure
frontend — every decision is made by the kernel; QShell only parses
input (via shell.parser), invokes, and displays. Supports command
history, !!, and tab completion.

Device-scoped commands (qconfig, qmem, qtopology, qerrors) take an
optional dN argument: with it, output covers that device only; without
it, output is sectioned per attached device. The format is uniform —
a single-device session simply shows one d0 section.
'''

import cmd
import os
import re
import time
import readline
import atexit
from circuits.qasm_loader import load_qasm
from config.config_loader import (SCHEDULER_LABELS, ALLOCATOR_LABELS,
                                  ROUTER_LABELS)
from shell.parser import parse_job_args

_DEVICE_RE = re.compile(r"^d(\d+)$")


class QShell(cmd.Cmd):
    prompt = "devq> "

    def __init__(self, kernel, global_config=None, global_provenance=None):
        super().__init__()
        self.kernel             = kernel
        self._global_config     = global_config     or {}
        self._global_provenance = global_provenance or {}
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

    # ── Device-argument helpers ───────────────────────────────────────────────

    def _contexts(self):
        return self.kernel.list_devices()

    def _parse_device_token(self, token):
        '''
        "d2" → context or raises ValueError with a user-facing message.
        '''
        m = _DEVICE_RE.match(token)
        if not m:
            raise ValueError(f"'{token}' is not a device — expected dN "
                             f"(see qdevices).")
        index    = int(m.group(1))
        contexts = self._contexts()
        if index >= len(contexts):
            raise ValueError(f"Device d{index} does not exist — "
                             f"{len(contexts)} device(s) attached "
                             f"(d0..d{len(contexts)-1}).")
        return contexts[index]

    def _select_contexts(self, arg):
        '''
        Optional dN in a command's args → ([context], rest_tokens);
        no device token → (all contexts, rest_tokens).
        '''
        tokens  = arg.split()
        device  = None
        rest    = []
        for t in tokens:
            if _DEVICE_RE.match(t):
                if device is not None:
                    raise ValueError("Specify at most one device.")
                device = self._parse_device_token(t)
            else:
                rest.append(t)
        contexts = [device] if device else list(self._contexts())
        return contexts, rest

    def _validate_spec_devices(self, specs):
        '''
        Validate --exec/--no-exec indices against attached devices.
        Raises ValueError on the first unknown index — before any
        circuit is loaded, so the whole batch dies atomically.
        '''
        n = len(self._contexts())
        for spec in specs:
            for indices, flag in ((spec.exec_on, "--exec"),
                                  (spec.no_exec_on, "--no-exec")):
                if not indices:
                    continue
                for i in indices:
                    if i >= n:
                        raise ValueError(
                            f"{flag} references d{i}, but only {n} device(s) "
                            f"are attached (d0..d{n-1}). See qdevices.")

    # ── Job commands ──────────────────────────────────────────────────────────

    def do_qrun(self, arg):
        try:
            if not arg:
                print("Usage: qrun <qasm_file> [--max-qubit-error=X] "
                      "[--max-edge-error=Y] [--exec=d0,d1 | --no-exec=d2]")
                return

            specs = parse_job_args(arg)

            if len(specs) > 1:
                print("[DevQ Error] qrun accepts exactly one job. "
                      "Use qsubmit + qrunpack for multiple jobs.")
                return

            self._validate_spec_devices(specs)

            spec    = specs[0]
            circuit = load_qasm(spec.file_path)
            qcb     = self.kernel.submit_job(
                circuit,
                max_qubit_error=spec.max_qubit_error,
                max_edge_error=spec.max_edge_error,
                exec_on=spec.exec_on,
                no_exec_on=spec.no_exec_on
            )
            print(f"Job {qcb.job_id} submitted to queue.")

            self.kernel.run_job(qcb)

            if qcb.state.value == "FINISHED":
                print(f"[+] Job {qcb.job_id} FINISHED.")
            elif qcb.state.value == "FAILED":
                print(f"[-] Job {qcb.job_id} failed. See above for details.")
            elif qcb.state.value == "WAITING":
                print(f"[~] Job {qcb.job_id} is WAITING for resources "
                      f"on d{qcb.device_index}.")
            elif qcb.state.value == "REJECTED":
                print(f"[x] Job {qcb.job_id} REJECTED: {qcb.reject_reason}")

        except Exception as e:
            print(f"[DevQ Error] {e}")

    def do_qsubmit(self, arg):
        try:
            if not arg:
                print("Usage: qsubmit <qasm_file> [...] "
                      "[--max-qubit-error=X] [--max-edge-error=Y] "
                      "[--exec=d0,d1 | --no-exec=d2] "
                      "| [group syntax: [a.qasm b.qasm --flag=X]]")
                return

            specs = parse_job_args(arg)

            # Validate device references, then load all circuits, before
            # submitting any — a bad device or file path rejects the
            # whole batch, consistent with parser atomicity.
            self._validate_spec_devices(specs)
            circuits = [load_qasm(spec.file_path) for spec in specs]

            for spec, circuit in zip(specs, circuits):
                qcb = self.kernel.submit_job(
                    circuit,
                    max_qubit_error=spec.max_qubit_error,
                    max_edge_error=spec.max_edge_error,
                    exec_on=spec.exec_on,
                    no_exec_on=spec.no_exec_on
                )
                print(f"Job {qcb.job_id} submitted to queue.")

        except Exception as e:
            print(f"[DevQ Error] {e}")

    def do_qrunpack(self, arg):
        try:
            total = 0
            reported = set()

            # Terminate only when nothing is queued anywhere AND no
            # dispatched future is still in flight — with async
            # execution the queues can drain while results are pending.
            while True:
                jobs = self.kernel.step()

                for job in jobs:
                    if job.job_id in reported:
                        continue
                    if job.state.value == "REJECTED":
                        print(f"[x] Job {job.job_id} REJECTED: "
                              f"{job.reject_reason}")
                        reported.add(job.job_id)
                        total += 1
                    elif job.state.value in ("RUNNING", "FINISHED", "FAILED"):
                        # dispatched this cycle; result prints on resolution
                        reported.add(job.job_id)
                        total += 1

                if not jobs and not self.kernel.has_queued() \
                        and not self.kernel.has_pending():
                    break

                if self.kernel.has_pending():
                    time.sleep(0.05)

            # Post-drain summary — final states for everything this
            # command processed (results themselves print on resolution).
            for job_id in sorted(reported):
                job = self.kernel.get_job(job_id)
                if job.state.value == "FINISHED":
                    print(f"[+] Job {job.job_id} FINISHED. "
                          f"Counts: {job.result.counts}")
                elif job.state.value == "FAILED":
                    print(f"[-] Job {job.job_id} FAILED. "
                          f"Error: {job.result.error}")

            if total == 0:
                print("No jobs in queue.")

        except Exception as e:
            print(f"[DevQ Error] {e}")

    # ── Inspection commands ───────────────────────────────────────────────────

    def do_qdevices(self, arg):
        '''List attached devices: index, name, provider, size, load.'''
        print()
        for ctx in self._contexts():
            provider = type(ctx.device.provider).__name__
            print(f"  d{ctx.index}   {ctx.device.name:<20} {provider:<24}"
                  f"{ctx.device.num_qubits:>3} qubits   "
                  f"queued: {ctx.queue_depth()}  running: {ctx.running_jobs}")
        print()

    def do_qps(self, arg):
        try:
            jobs = self.kernel.list_jobs()

            if not jobs:
                print("No jobs.")
                return

            for job in jobs:
                dev = f"d{job.device_index}" if job.device_index is not None else "-"
                print(f"{job.job_id} | {dev:<3} | {job.state.value}")

        except Exception as e:
            print(f"[DevQ Error] {e}")

    def do_qmap(self, arg):
        try:
            job_id = int(arg.strip())
        except ValueError:
            print("Invalid job id.")
            return

        job = self.kernel.get_job(job_id)

        if job is None:
            print(f"Job {job_id} does not exist.")
            return

        print(f"\nJob {job_id} mapping\n")
        if job.device_index is not None:
            ctx = self._contexts()[job.device_index]
            print(f"device: d{ctx.index} ({ctx.device.name})\n")
        else:
            print("device: - (not routed)\n")

        print("virtual → physical\n")
        for v, p in job.v2p_map.items():
            print(f"  {v} → {p}")
        print()

    def do_qmem(self, arg):
        try:
            contexts, rest = self._select_contexts(arg)
            if rest:
                print("Usage: qmem [dN]")
                return

            print()
            for ctx in contexts:
                free_set = ctx.memory_manager.pool.free_qubits
                print(f"  d{ctx.index} ({ctx.device.name}):")
                for qubit in range(ctx.device.num_qubits):
                    status = "[]" if qubit in free_set else "[X]"
                    print(f"    {qubit} {status}")
                print()

        except ValueError as e:
            print(f"[DevQ Error] {e}")

    def do_qtopology(self, arg):
        try:
            contexts, rest = self._select_contexts(arg)

            requested = None
            if rest:
                if len(contexts) > 1:
                    print("Qubit filtering requires a device: "
                          "qtopology dN [q ...]")
                    return
                try:
                    requested = [int(i) for i in rest]
                except ValueError:
                    print("Invalid argument for qtopology.")
                    return

            print()
            for ctx in contexts:
                print(f"  d{ctx.index} ({ctx.device.name}) topology:")
                topology = ctx.device.coupling_map
                total    = ctx.device.num_qubits

                if requested is None:
                    for q1, q2 in topology:
                        print(f"    {q1} -- {q2}")
                else:
                    for q in requested:
                        if q < 0 or q >= total:
                            print(f"    {q} -- Doesn't exist")
                    for q1, q2 in topology:
                        if q1 in requested or q2 in requested:
                            print(f"    {q1} -- {q2}")
                print()

        except ValueError as e:
            print(f"[DevQ Error] {e}")

    def do_qerrors(self, arg):
        try:
            contexts, rest = self._select_contexts(arg)

            flag = rest[0][0] if rest else 'b'
            if flag not in ['e', 'q', 'b']:
                print("Invalid flag. Use: q (qubit), e (edge), b (both, "
                      "default) — optionally with a device: qerrors e d1")
                return

            for ctx in contexts:
                print(f"\n  d{ctx.index} ({ctx.device.name}):")

                if flag in ('q', 'b'):
                    print("\n  Qubit Error Map:\n")
                    for q, err in sorted(ctx.device.error_map.items()):
                        print(f"    {q} -> {err:.4f}")

                if flag in ('e', 'b'):
                    print("\n  Edge Error Map:\n")
                    for edge, err in sorted(ctx.device.edge_error_map.items()):
                        print(f"    {edge} -> {err:.4f}")

            print()

        except ValueError as e:
            print(f"[DevQ Error] {e}")

    def do_qconfig(self, arg):
        '''Display active configuration — global router policy plus the
        per-device cascade result — and the source of each value.'''
        try:
            contexts, rest = self._select_contexts(arg)
            if rest:
                print("Usage: qconfig [dN]")
                return

            show_global = not arg.strip()

            print()
            if show_global:
                router = self._global_config.get("router", "")
                label  = ROUTER_LABELS.get(router, "")
                source = self._global_provenance.get("router", "")
                print(f"  router       =  {router:<14}  [{label}]  "
                      f"source: {source}")
                for key in ("router_queue_weight", "router_noise_weight"):
                    val    = self._global_config.get(key, "")
                    source = self._global_provenance.get(key, "")
                    print(f"  {key:<12} =  {str(val):<14}  source: {source}")
                print()

            for ctx in contexts:
                provider = type(ctx.device.provider).__name__
                print(f"  d{ctx.index}:")
                print(f"    provider   =  {provider}")
                print(f"    device     =  {ctx.device.name}  "
                      f"({ctx.device.num_qubits} qubits)")

                rows = [
                    ("scheduler", SCHEDULER_LABELS.get(
                        ctx.config.get("scheduler", ""), "")),
                    ("allocator", ALLOCATOR_LABELS.get(
                        ctx.config.get("allocator", ""), "")),
                    ("shots",     ""),
                ]

                for key, label in rows:
                    val    = ctx.config.get(key, "")
                    source = ctx.provenance.get(key, "")
                    if label:
                        print(f"    {key:<12} =  {str(val):<14}  [{label}]  "
                              f"source: {source}")
                    else:
                        print(f"    {key:<12} =  {str(val):<14}  "
                              f"source: {source}")
                print()

        except ValueError as e:
            print(f"[DevQ Error] {e}")