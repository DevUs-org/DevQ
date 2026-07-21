'''
Tags: Main

QShell Argument Parser

Parses qsubmit and qrun argument strings into JobSpec objects.

Syntax:
    Bare job:
        job.qasm                          no thresholds, any device

    Job with trailing flags:
        job.qasm --max-qubit-error=0.05   qubit threshold only
        job.qasm --max-edge-error=0.1     edge threshold only
        job.qasm --exec=d0,d2             allow-list: may ONLY run on d0/d2;
        job.qasm --exec=nairobi            names work wherever dN does;
                                          if infeasible on all of them the
                                          job is REJECTED — never re-routed
        job.qasm --no-exec=d1             deny-list: never routed to d1

    Bracket group (flags apply to all jobs in group):
        [job1.qasm job2.qasm --max-qubit-error=0.05 --no-exec=d1]
        [job1.qasm job2.qasm]             valid — no flags

    Mixed:
        [job1 job2 --max-qubit-error=0.05] job3 job4 --exec=d0 job5

Rules:
    - Threshold values must be floats in [0, 1].
    - Device lists are comma-separated tokens (no brackets — brackets
      are reserved for job grouping): --exec=d0,d1 or --exec=nairobi,d2.
      A token is either a dN index or a user-supplied device name.
    - --exec and --no-exec are mutually exclusive per job/group.
    - Device *existence* is validated at submit time by the shell
      (the parser cannot know how many devices are attached);
      the parser validates format only.
    - Malformed input (unclosed brackets, unknown flags, out-of-range
      values, flags with no preceding file, malformed device lists,
      both exec flags together) rejects the ENTIRE command —
      no job is created.
'''

from dataclasses import dataclass


@dataclass
class JobSpec:
    file_path:       str
    max_qubit_error: float | None = None  # None = no qubit-error filtering
    max_edge_error:  float | None = None  # None = no edge-error filtering
    # Device references as WRITTEN by the user — "d0" or a device name.
    # The parser cannot resolve these (it does not know what is
    # attached); the shell resolves them to indices at submit time.
    exec_on:         list[str] | None = None  # allow-list of device refs
    no_exec_on:      list[str] | None = None  # deny-list of device refs

    def __repr__(self):
        return (f"JobSpec(file={self.file_path}, "
                f"qe={self.max_qubit_error}, ee={self.max_edge_error}, "
                f"exec={self.exec_on}, no_exec={self.no_exec_on})")


_THRESHOLD_FLAGS = ('max-qubit-error', 'max-edge-error')
_DEVICE_FLAGS    = ('exec', 'no-exec')
_KNOWN_FLAGS     = _THRESHOLD_FLAGS + _DEVICE_FLAGS


# ── Public entry point ────────────────────────────────────────────────────────

def parse_job_args(arg: str) -> list[JobSpec]:
    '''
    Parse a qsubmit or qrun argument string into a list of JobSpec objects.

    Args:
        arg: raw argument string from the shell command handler

    Returns:
        list of JobSpec — one per job file found

    Raises:
        ValueError: on malformed syntax (unclosed brackets, bad flag
        values, conflicting exec flags)
    '''
    tokens = arg.split()
    specs  = []
    i      = 0

    while i < len(tokens):
        token = tokens[i]

        if token.startswith('['):
            # ── Bracket group ─────────────────────────────────────────────
            group_tokens = []

            # Token may be '[job.qasm' or '[' alone
            first = token[1:]  # strip opening bracket
            if first:
                group_tokens.append(first)
            i += 1

            closed = False
            while i < len(tokens):
                t = tokens[i]
                if t.endswith(']'):
                    inner = t[:-1]  # strip closing bracket
                    if inner:
                        group_tokens.append(inner)
                    closed = True
                    i += 1
                    break
                group_tokens.append(t)
                i += 1

            if not closed:
                raise ValueError(
                    "Unclosed '[' in arguments. "
                    "Every '[' must have a matching ']'."
                )

            # Separate files and flags within the group
            files, flags = _extract_files_and_flags(group_tokens)

            for f in files:
                specs.append(JobSpec(file_path=f, **flags))

        elif token.startswith('--'):
            raise ValueError(
                f"Unexpected flag '{token}' — flags must follow a job file "
                f"or appear inside a bracket group."
            )

        else:
            # ── Bare job — check for immediately following flags ──────────
            file_path   = token
            flag_tokens = []
            i += 1

            while i < len(tokens) and tokens[i].startswith('--'):
                flag_tokens.append(tokens[i])
                i += 1

            _, flags = _extract_files_and_flags([file_path] + flag_tokens)
            specs.append(JobSpec(file_path=file_path, **flags))

    return specs


# ── Private helpers ───────────────────────────────────────────────────────────

def _extract_files_and_flags(tokens: list) -> tuple:
    '''
    Split a flat token list (from inside brackets or a bare job sequence)
    into file paths and a flag dict for JobSpec construction.

    Returns:
        (files, flags) where flags has keys
        max_qubit_error / max_edge_error / exec_on / no_exec_on

    Raises:
        ValueError: on malformed flags or --exec + --no-exec together
    '''
    files = []
    flags = {
        "max_qubit_error": None,
        "max_edge_error":  None,
        "exec_on":         None,
        "no_exec_on":      None
    }

    for token in tokens:
        if token.startswith('--'):
            key, val = _parse_flag(token)
            if key == 'max-qubit-error':
                flags["max_qubit_error"] = val
            elif key == 'max-edge-error':
                flags["max_edge_error"] = val
            elif key == 'exec':
                flags["exec_on"] = val
            elif key == 'no-exec':
                flags["no_exec_on"] = val
        else:
            files.append(token)

    if flags["exec_on"] is not None and flags["no_exec_on"] is not None:
        raise ValueError(
            "--exec and --no-exec are mutually exclusive on the same "
            "job or group — an allow-list already implies exclusion of "
            "every other device."
        )

    if not files:
        raise ValueError(
            "A bracket group or job entry must contain at least one file path."
        )

    return files, flags


def _parse_flag(flag: str) -> tuple:
    '''
    Parse a single flag string into (key, value).

    Threshold flags return (key, float); device flags return
    (key, list_of_device_indices).

    Raises:
        ValueError: if the flag is unknown or the value malformed.
    '''
    if '=' not in flag:
        raise ValueError(
            f"Flag '{flag}' is missing a value. "
            f"Expected format: --max-qubit-error=0.05 or --exec=d0,d1"
        )

    key, _, raw_val = flag.lstrip('-').partition('=')

    if key not in _KNOWN_FLAGS:
        raise ValueError(
            f"Unknown flag '--{key}'. Supported flags: "
            f"--max-qubit-error, --max-edge-error, --exec, --no-exec"
        )

    if not raw_val:
        raise ValueError(
            f"Missing value for '--{key}'. "
            f"Expected format: --{key}="
            f"{'0.05' if key in _THRESHOLD_FLAGS else 'd0,d1'}"
        )

    if key in _DEVICE_FLAGS:
        return key, _parse_device_list(key, raw_val)

    try:
        val = float(raw_val)
    except ValueError:
        raise ValueError(
            f"Invalid value for '--{key}': '{raw_val}' is not a number. "
            f"Expected a float between 0 and 1."
        )

    if not 0.0 <= val <= 1.0:
        raise ValueError(
            f"Invalid value for '--{key}': {val} is out of range. "
            f"Expected a float between 0 and 1."
        )

    return key, val


def _parse_device_list(key: str, raw_val: str) -> list[str]:
    '''
    Parse a comma-separated device list ("d0,nairobi") into a list of
    unique device reference tokens, order preserved.

    Tokens are either dN indices or user-supplied device names. The
    parser validates FORM only — that tokens are non-empty and bracket
    free. Whether a token names an attached device (and what index it
    resolves to) is the shell's job at submit time, since the parser
    has no view of the device federation.
    '''
    if '[' in raw_val or ']' in raw_val:
        raise ValueError(
            f"Invalid device list for '--{key}': '{raw_val}'. Device lists "
            f"are comma-separated without brackets — e.g. --{key}=d0,d1 "
            f"(brackets are reserved for job grouping)."
        )

    refs = []
    for part in raw_val.split(','):
        part = part.strip()
        if not part:
            raise ValueError(
                f"Empty device reference in '--{key}={raw_val}'. "
                f"Device lists are comma-separated — e.g. --{key}=d0,d1."
            )
        if part.lower() not in [r.lower() for r in refs]:
            refs.append(part)

    return refs