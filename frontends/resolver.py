'''
Tags: Main

Frontend resolution — pick the frontend that reads one job's source.

A frontend is DISPATCHED per job, not selected by config (see
frontends/base_frontend.py). This module holds the single piece of
logic that answers "which frontend reads this source?", so the shell
and the benchmark spec runner resolve identically instead of each
growing its own copy.

RESOLUTION ORDER, for one job's source and an optional explicit
override:

  1. explicit override given  -> that frontend, or an error naming the
                                 registered ones if it is unknown.
  2. else, extension claimed by EXACTLY ONE frontend -> that one.
  3. else, extension claimed by TWO OR MORE          -> reject, asking
                                 for --frontend to disambiguate. This is
                                 the qasm2/qasm3 case: both claim .qasm,
                                 and DevQ must not guess which dialect a
                                 file is. The ambiguity is resolved HERE,
                                 per job, NOT at registration — both
                                 claiming .qasm is legal and expected.
  4. else, extension claimed by NONE                 -> reject, naming
                                 the extensions that are handled.

WHY PER-JOB, NOT AT REGISTRATION. Registering both qasm2 and qasm3 is
correct: a session should be able to read either. The conflict is not
between the two frontends existing, it is in a single file whose
extension does not say which dialect it is. That is a property of the
job, so it is answered where the job is — the same place --exec and
--max-qubit-error are answered.
'''


class FrontendResolutionError(Exception):
    '''
    Raised when a job's source cannot be matched to exactly one
    frontend. Callers wrap this with the job's provenance (the shell
    prints it; the spec runner raises SpecError) — it carries only the
    reason, not who asked.
    '''
    pass


def extension_of(source):
    '''
    The dispatch extension of a source path: everything from the last
    dot, lowercased. "circuits/Bell.QASM" -> ".qasm". A path with no dot
    yields "" — which no frontend claims, so it falls to the no-handler
    branch with a clear message rather than a silent miss.
    '''
    name = source.rsplit("/", 1)[-1]
    dot  = name.rfind(".")
    if dot == -1:
        return ""
    return name[dot:].lower()


def build_extension_index(frontends):
    '''
    Invert a {name: frontend} map into {extension: [names]}, preserving
    registration order within each extension so error messages and the
    single-claimant path are deterministic.

    Args:
        frontends: {name: BaseFrontend instance} — every registered
                   frontend, already constructed. Order is the map's
                   iteration order, i.e. registration order.

    Returns:
        {extension: [name, ...]} with lowercase dotted extensions.
    '''
    index = {}
    for name, frontend in frontends.items():
        for ext in getattr(frontend, "EXTENSIONS", ()):
            index.setdefault(ext.lower(), []).append(name)
    return index


def resolve_frontend(source, frontends, explicit=None):
    '''
    Return the frontend instance that should read `source`.

    Args:
        source:    path to the job's source file.
        frontends: {name: BaseFrontend instance} — every registered
                   frontend.
        explicit:  optional frontend name from a --frontend flag (shell)
                   or a "frontend" spec key. Overrides extension
                   dispatch entirely.

    Returns:
        BaseFrontend instance.

    Raises:
        FrontendResolutionError: on an unknown explicit name, an
        ambiguous extension with no override, or an unhandled extension.
    '''
    if explicit is not None:
        try:
            return frontends[explicit]
        except KeyError:
            known = ", ".join(frontends) or "none"
            raise FrontendResolutionError(
                f"unknown frontend '{explicit}'. Registered frontends: "
                f"{known}."
            ) from None

    ext   = extension_of(source)
    index = build_extension_index(frontends)
    names = index.get(ext, [])

    if len(names) == 1:
        return frontends[names[0]]

    if len(names) > 1:
        raise FrontendResolutionError(
            f"'{ext}' is handled by more than one frontend "
            f"({', '.join(names)}) — DevQ cannot tell which dialect this "
            f"file is. Disambiguate with --frontend=<name> (shell) or a "
            f"\"frontend\" key on the job (spec)."
        )

    handled = ", ".join(sorted(index)) or "none"
    raise FrontendResolutionError(
        f"no registered frontend handles '{ext or '(no extension)'}'. "
        f"Handled extensions: {handled}."
    )