'''
Tags: Main

QShell Argument Parser

Parses qsubmit and qrun argument strings into JobSpec objects.

Syntax:
    Bare job:
        job.qasm                          uses device defaults

    Job with trailing flags:
        job.qasm --max-qubit-error=0.05   overrides qubit threshold only
        job.qasm --max-edge-error=0.1     overrides edge threshold only
        job.qasm --max-qubit-error=0.05 --max-edge-error=0.1

    Bracket group (flags apply to all jobs in group):
        [job1.qasm job2.qasm --max-qubit-error=0.05]
        [job1.qasm job2.qasm]             valid — no flags, uses device defaults

    Mixed:
        [job1 job2 --max-qubit-error=0.05] job3 job4 --max-edge-error=0.1 job5

    Priority chain:
        job-level → device-level → None (no filtering)
'''

from dataclasses import dataclass


@dataclass
class JobSpec:
    file_path:       str
    max_qubit_error: float | None = None  # None = use device default
    max_edge_error:  float | None = None  # None = use device default

    def __repr__(self):
        return (f"JobSpec(file={self.file_path}, "
                f"qe={self.max_qubit_error}, ee={self.max_edge_error})")


# ── Public entry point ────────────────────────────────────────────────────────

def parse_job_args(arg: str) -> list[JobSpec]:
    '''
    Parse a qsubmit or qrun argument string into a list of JobSpec objects.

    Args:
        arg: raw argument string from the shell command handler

    Returns:
        list of JobSpec — one per job file found

    Raises:
        ValueError: on malformed syntax (unclosed brackets, bad flag values)
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
            files, qe, ee = _extract_files_and_flags(group_tokens)

            for f in files:
                specs.append(JobSpec(
                    file_path       = f,
                    max_qubit_error = qe,
                    max_edge_error  = ee
                ))

        elif token.startswith('--'):
            raise ValueError(
                f"Unexpected flag '{token}' — flags must follow a job file "
                f"or appear inside a bracket group."
            )

        else:
            # ── Bare job — check for immediately following flags ──────────
            file_path = token
            i += 1
            qe, ee = None, None

            while i < len(tokens) and tokens[i].startswith('--'):
                flag = tokens[i]
                i += 1
                key, val = _parse_flag(flag)
                if key == 'max-qubit-error':
                    qe = val
                elif key == 'max-edge-error':
                    ee = val

            specs.append(JobSpec(
                file_path       = file_path,
                max_qubit_error = qe,
                max_edge_error  = ee
            ))

    return specs


# ── Private helpers ───────────────────────────────────────────────────────────

def _extract_files_and_flags(tokens: list) -> tuple:
    '''
    Split a flat token list (from inside brackets or a bare job sequence)
    into file paths and optional threshold flags.

    Returns:
        (files, max_qubit_error, max_edge_error)
    '''
    files = []
    qe    = None
    ee    = None

    for token in tokens:
        if token.startswith('--'):
            key, val = _parse_flag(token)
            if key == 'max-qubit-error':
                qe = val
            elif key == 'max-edge-error':
                ee = val
        else:
            files.append(token)

    if not files:
        raise ValueError(
            "A bracket group or job entry must contain at least one file path."
        )

    return files, qe, ee


def _parse_flag(flag: str) -> tuple:
    '''
    Parse a single flag string into (key, value).

    Args:
        flag: e.g. "--max-qubit-error=0.05"

    Returns:
        (key, float_value) e.g. ("max-qubit-error", 0.05)

    Raises:
        ValueError: if the flag is unknown, or the value is not a valid
        float in [0, 1]
    '''
    if '=' not in flag:
        raise ValueError(
            f"Flag '{flag}' is missing a value. "
            f"Expected format: --max-qubit-error=0.05"
        )

    key, _, raw_val = flag.lstrip('-').partition('=')

    if key not in ('max-qubit-error', 'max-edge-error'):
        raise ValueError(
            f"Unknown flag '--{key}'. "
            f"Supported flags: --max-qubit-error, --max-edge-error"
        )

    if not raw_val:
        raise ValueError(
            f"Missing value for '--{key}'. "
            f"Expected format: --{key}=0.05"
        )

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