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
import collections
import os
import re
import time
import readline
import atexit
from circuits.qasm_loader import load_qasm
from shell.parser import parse_job_args

_DEVICE_RE = re.compile(r"^d(\d+)$")


# Cap the on-disk history. Unbounded, this file grows for the life of
# the project and every session pays to read it back — on macOS, where
# readline is backed by libedit, reading a large history file consumes
# memory catastrophically (a 6 GB file cost ~25 GB of RSS).
HISTORY_LIMIT = 1000

# Refuse to read a history file larger than this. set_history_length
# only bounds what gets WRITTEN, so a file that grew before the cap
# existed would still be read in full on the next launch — the cap
# would never get a chance to apply. Truncating on read makes the fix
# self-healing rather than dependent on a clean prior shutdown.
HISTORY_MAX_BYTES = 4 * 1024 * 1024


class QShell(cmd.Cmd):
    prompt = "devq> "

    def __init__(self, kernel, global_config=None, global_provenance=None,
                 labels=None, interactive=True):
        '''
        Args:
            labels:      display names for registered components, as
                         {kind: {name: label}}, resolved once at build
                         time. Passed as DATA rather than as a reference
                         to the ConfigLoader: the shell renders
                         configuration, it does not resolve it, and
                         holding the loader would let it re-read config
                         or reach the registry mid-session.
            interactive: whether this shell is driven by a human at a
                         terminal. False disables readline history —
                         command history is meaningless for a shell
                         driven through onecmd(), and loading it is
                         actively harmful: on macOS the readline module
                         is backed by libedit, whose read_history_file
                         can consume enormous amounts of memory on a
                         large history file. A harness that builds many
                         shells per process would pay that cost every
                         time, and every shell would also register an
                         atexit hook that rewrites the file.
        '''
        super().__init__()
        self.kernel             = kernel
        self._global_config     = global_config     or {}
        self._global_provenance = global_provenance or {}
        self._labels            = labels            or {}
        self._last_command = None
        self._history_file = None

        if interactive:
            self._history_file = os.path.expanduser("~/.devq_history")
            readline.parse_and_bind("tab: complete")
            self._load_history()
            atexit.register(self._save_history)

    def _load_history(self):
        '''
        Read command history, trimming the file first if it has grown
        past HISTORY_MAX_BYTES. Any failure here is non-fatal — history
        is a convenience, and a corrupt or unreadable file must never
        stop a session from starting.
        '''
        path = self._history_file
        try:
            if os.path.getsize(path) > HISTORY_MAX_BYTES:
                self._truncate_history(path)
        except OSError:
            return          # missing or unreadable — nothing to load

        try:
            readline.read_history_file(path)
        except (OSError, ValueError):
            pass

    @staticmethod
    def _truncate_history(path):
        '''
        Keep only the last HISTORY_LIMIT lines of an oversized history
        file, reading from the end so an enormous file is never pulled
        into memory whole.
        '''
        try:
            keep = collections.deque(maxlen=HISTORY_LIMIT)
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    keep.append(line)
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(keep)
        except OSError:
            pass

    def _save_history(self):
        if not self._history_file:
            return
        try:
            readline.set_history_length(HISTORY_LIMIT)
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

    def _lookup_name(self, token):
        '''Context whose user-supplied name matches token, or None.'''
        needle = token.strip().lower()
        for ctx in self._contexts():
            if ctx.name and ctx.name == needle:
                return ctx
        return None

    def _is_device_token(self, token):
        '''
        True if token refers to an attached device, by index or by name.

        Used to DISCRIMINATE device tokens from other positional
        arguments, so it must not raise: an out-of-range index like d9
        is still recognisably a device reference (and reported as a bad
        device later), while an arbitrary word is only a device if it
        actually matches an attached name.
        '''
        return bool(_DEVICE_RE.match(token)) or self._lookup_name(token) is not None

    def _parse_device_token(self, token):
        '''
        "d2" or a user-supplied name → context, or raises ValueError
        with a user-facing message.
        '''
        contexts = self._contexts()

        m = _DEVICE_RE.match(token)
        if m:
            index = int(m.group(1))
            if index >= len(contexts):
                raise ValueError(f"Device d{index} does not exist — "
                                 f"{len(contexts)} device(s) attached "
                                 f"(d0..d{len(contexts)-1}).")
            return contexts[index]

        ctx = self._lookup_name(token)
        if ctx is not None:
            return ctx

        named = [c.name for c in contexts if c.name]
        hint  = f" Named devices: {', '.join(named)}." if named else ""
        raise ValueError(f"'{token}' is not a device — expected dN or a "
                         f"device name (see qdevices).{hint}")

    def _select_contexts(self, arg):
        '''
        Optional dN in a command's args → ([context], rest_tokens);
        no device token → (all contexts, rest_tokens).
        '''
        tokens  = arg.split()
        device  = None
        rest    = []
        for t in tokens:
            if self._is_device_token(t):
                if device is not None:
                    raise ValueError("Specify at most one device.")
                device = self._parse_device_token(t)
            else:
                rest.append(t)
        contexts = [device] if device else list(self._contexts())
        return contexts, rest

    def _validate_spec_devices(self, specs):
        '''
        Resolve --exec/--no-exec device references to indices, IN PLACE.

        The parser leaves these as raw tokens ("d0", "nairobi") because
        it has no view of the federation; this is where they become the
        indices the kernel and QCB use. Raises ValueError on the first
        unresolvable reference — before any circuit is loaded, so the
        whole batch dies atomically and no partial submission occurs.
        '''
        for spec in specs:
            for attr, flag in (("exec_on", "--exec"),
                               ("no_exec_on", "--no-exec")):
                refs = getattr(spec, attr)
                if not refs:
                    continue

                indices = []
                for ref in refs:
                    try:
                        ctx = self._parse_device_token(ref)
                    except ValueError as e:
                        raise ValueError(f"{flag}: {e}")
                    if ctx.index not in indices:
                        indices.append(ctx.index)

                setattr(spec, attr, sorted(indices))

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
                      f"on {self._contexts()[qcb.device_index].label}.")
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
        '''List attached devices: index, name, backend, provider, size, load.'''
        contexts = self._contexts()
        show_names = any(ctx.name for ctx in contexts)
        width = max((len(ctx.name) for ctx in contexts if ctx.name),
                    default=0)

        print()
        for ctx in contexts:
            provider = type(ctx.device.provider).__name__
            alias    = f"{(ctx.name or '-'):<{width}}   " if show_names else ""
            print(f"  d{ctx.index}   {alias}{ctx.device.display_kind:<20} {provider:<24}"
                  f"{ctx.device.num_qubits:>3} qubits   "
                  f"queued: {ctx.queue_depth()}  running: {ctx.running_jobs}")
        print()

    def do_qps(self, arg):
        try:
            jobs = self.kernel.list_jobs()

            if not jobs:
                print("No jobs.")
                return

            contexts = self._contexts()
            labels   = [c.label for c in contexts]
            # Minimum 3 keeps unnamed sessions byte-identical to the
            # pre-naming column layout; names widen it as needed.
            width    = max([3] + [len(l) for l in labels])

            for job in jobs:
                dev = (labels[job.device_index]
                       if job.device_index is not None else "-")
                print(f"{job.job_id} | {dev:<{width}} | {job.state.value}")

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
            print(f"device: {ctx.label} ({ctx.device.display_kind})\n")
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
                print(f"  {ctx.label} ({ctx.device.display_kind}):")
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
                print(f"  {ctx.label} ({ctx.device.display_kind}) topology:")
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
                print(f"\n  {ctx.label} ({ctx.device.display_kind}):")

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
                label  = self._labels.get("router", {}).get(router, "")
                source = self._global_provenance.get("router", "")
                print(f"  router       =  {router:<14}  [{label}]  "
                      f"source: {source}")
                for key in ("router_queue_weight", "router_noise_weight"):
                    val    = self._global_config.get(key, "")
                    source = self._global_provenance.get(key, "")
                    print(f"  {key:<12} =  {str(val):<14}  source: {source}")
                for key in ("qubit_error_weight", "edge_error_weight"):
                    val    = self._global_config.get(key, "")
                    source = self._global_provenance.get(key, "")
                    print(f"  {key:<12} =  {str(val):<14}  source: {source}")
                print()

            for ctx in contexts:
                provider = type(ctx.device.provider).__name__
                print(f"  {ctx.label}:")
                print(f"    provider   =  {provider}")
                print(f"    device     =  {ctx.device.display_kind}  "
                      f"({ctx.device.num_qubits} qubits)")

                rows = [
                    ("scheduler", self._labels.get("scheduler", {}).get(
                        ctx.config.get("scheduler", ""), "")),
                    ("allocator", self._labels.get("allocator", {}).get(
                        ctx.config.get("allocator", ""), "")),
                    ("shots",     ""),
                    ("qubit_error_weight", ""),
                    ("edge_error_weight",  ""),
                ]

                # Keys contributed by registered plugins. Namespaced, so
                # they sort together under their owner's prefix and can
                # never be confused with a core key. Listed after the
                # core rows rather than interleaved, because a device
                # not using that plugin still resolves its defaults.
                rows += [(key, "") for key in sorted(ctx.config)
                         if "." in key]

                for key, label in rows:
                    val    = ctx.config.get(key, "")
                    source = ctx.provenance.get(key, "")
                    # Compact float display — normalised weights can be
                    # long repeating decimals (e.g. 0.35714285714285715).
                    val_str = f"{val:g}" if isinstance(val, float) else str(val)
                    if label:
                        print(f"    {key:<18} =  {val_str:<14}  [{label}]  "
                              f"source: {source}")
                    else:
                        print(f"    {key:<18} =  {val_str:<14}  "
                              f"source: {source}")
                print()

        except ValueError as e:
            print(f"[DevQ Error] {e}")