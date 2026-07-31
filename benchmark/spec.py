'''
Tags: Main

Workload spec parsing — JSON in, a built DevQ session out.

A workload spec describes a benchmark run as DATA: which devices, which
jobs, which config, which seed. The runner reads one and produces a
session; the event log records the spec verbatim, so a log is
self-describing and a result can be traced back to the exact input that
produced it.

STRICTNESS. The config cascade WARNS on an unknown key and continues;
spec parsing HARD-ERRORS. The asymmetry is not a difference in
severity, it is a difference in what recovery is available: EVERY
CONFIG KEY HAS A DOCUMENTED DEFAULT TO FALL BACK TO, AND A SPEC KEY HAS
NONE. There is no sensible default for which circuit to run or which
device to run it on, so the only alternatives to refusing are guessing
or silently dropping — and a benchmark that quietly ran something other
than what was written is worse than one that would not start.

Absent-with-a-default is therefore fine and not an exception to this:
`repeat` defaults to 1 and `arrival.pattern` to "batch", and omitting
them is silent. It is UNKNOWN keys — ones carrying no meaning the
parser can act on — that are refused. Every error names the offending
key and lists what was expected.

SEED RESOLUTION. A spec's `seed` is simply the seed. Providers are
registered as CLASSES, so the parser constructs each one here and
passes the spec's seed to it; if the spec gives none, the provider is
constructed unseeded. Nothing pre-existing can hold a competing seed,
so there is no conflict to arbitrate.

  spec seed   effective   source
  ---------   ---------   --------
  absent      unseeded    unseeded
  42          42          spec

This used to be a five-row table with a documented winner, because a
provider could be registered as a ready-made instance carrying its own
seed. Class-only registration removed the ambiguity rather than
resolving it — see docs/REGISTRY.md. A caller who wants a seed the spec
does not name constructs the provider themselves and attaches its
device with add_device(), which is the same path a credentialed
provider takes.

The log records seed_requested, seed_effective and seed_source, so an
artifact never claims a seed that did not run. seed_requested is the
value AS WRITTEN — for a ${NAME} placeholder spec it is the literal
"${NAME}", matching the verbatim spec in the header — while
seed_effective is the resolved integer the run actually used. The two
coincide for a spec with no placeholder.
'''

import hashlib
import json
import os

from devq import DevQError


# Every key the parser understands, per level. Anything outside these
# sets is an error — see _reject_unknown.
_TOP_KEYS    = frozenset({"name", "seed", "config", "devices", "arrival", "jobs"})
_DEVICE_KEYS = frozenset({"id", "provider", "backend", "config"})
_JOB_KEYS    = frozenset({"circuit", "repeat", "max_qubit_error",
                          "max_edge_error", "max_1q_gate_error",
                          "exec_on", "no_exec_on",
                          "frontend", "shots"})
_ARRIVAL_KEYS = frozenset({"pattern"})

# Phase 5.2 supports batch arrival only. Poisson needs virtual time —
# wall-clock sleeps would make runs non-reproducible — so it is deferred
# without changing this schema.
_ARRIVAL_PATTERNS = frozenset({"batch"})


class SpecError(DevQError):
    '''Raised for any malformed workload spec. Always names the offending
    key and what was expected.'''
    pass


def _reject_unknown(obj, allowed, where):
    '''Hard-error on any key outside `allowed`, listing what was expected.'''
    unknown = set(obj) - allowed
    if unknown:
        raise SpecError(
            f"{where}: unknown key(s) {sorted(unknown)}. "
            f"Expected one of {sorted(allowed)}. Spec keys are validated "
            f"strictly — a typo here would silently change what runs."
        )


def _require(obj, key, where, types=None):
    if key not in obj:
        raise SpecError(f"{where}: missing required key '{key}'.")
    value = obj[key]
    if types is not None and not isinstance(value, types):
        names = (types.__name__ if isinstance(types, type)
                 else " or ".join(t.__name__ for t in types))
        raise SpecError(
            f"{where}: '{key}' must be {names}, got "
            f"{type(value).__name__} ({value!r})."
        )
    return value


def _coerce_int(value, key, where):
    '''
    Coerce a scalar to int, or raise SpecError naming the field.

    Placeholder resolution yields strings, so a numeric spec field may
    arrive as "42" rather than 42; this turns it into an int and refuses
    only what genuinely is not one. A bool is rejected outright: in
    Python bool is an int subclass, so True would coerce to 1 silently,
    which is never what a seed or repeat count meant.
    '''
    if isinstance(value, bool):
        raise SpecError(
            f"{where}: '{key}' must be an integer, got a boolean ({value!r})."
        )
    try:
        return int(value)
    except (TypeError, ValueError):
        raise SpecError(
            f"{where}: '{key}' must be an integer, got "
            f"{type(value).__name__} ({value!r}) which is not coercible. "
            f"A ${{...}} placeholder resolves to a string, so a numeric "
            f"field accepts a coercible string — but this value is not one."
        ) from None


def _coerce_float(value, key, where):
    '''Coerce a scalar to float, or raise SpecError. See _coerce_int for
    why booleans are refused rather than silently treated as 0.0/1.0.'''
    if isinstance(value, bool):
        raise SpecError(
            f"{where}: '{key}' must be a number, got a boolean ({value!r})."
        )
    try:
        return float(value)
    except (TypeError, ValueError):
        raise SpecError(
            f"{where}: '{key}' must be a number, got "
            f"{type(value).__name__} ({value!r}) which is not coercible."
        ) from None


def load_spec(path):
    '''
    Read and validate a workload spec file.

    Returns (resolved, verbatim): the resolved spec drives construction,
    the verbatim spec is what the log header records. They differ only
    when the file contains ${NAME} placeholders — resolved has the
    environment values substituted, verbatim keeps the literal ${NAME}.

    WHY BOTH. A resolved ${IONQ_API_KEY} must never reach disk, so the
    header records verbatim; but construction needs the real value, so
    device-building reads resolved. Returning both keeps the two honest
    and puts the choice at each use site rather than guessing. When a
    spec has no placeholders the two are equal, so a caller that ignores
    verbatim loses nothing.

    Validation is structural only — it does not resolve providers or
    touch the filesystem beyond reading this file. build_session() does
    that, so a spec can be checked without constructing anything.
    '''
    if not os.path.exists(path):
        raise SpecError(f"workload spec not found: {path}")

    try:
        with open(path) as handle:
            spec = json.load(handle)
    except json.JSONDecodeError as exc:
        raise SpecError(f"{path} is not valid JSON: {exc}") from None

    if not isinstance(spec, dict):
        raise SpecError(
            f"{path} must contain a JSON object, got {type(spec).__name__}."
        )

    # Resolve ${NAME} placeholders against the environment BEFORE
    # validation, so validate_spec checks the values that will actually
    # run rather than the placeholder text. resolve_placeholders returns
    # a NEW object, so `spec` remains the verbatim original — that copy
    # is what the header must record, and returning it here is what makes
    # the "never log a resolved secret" rule enforceable rather than
    # aspirational. Function-level import avoids a cycle: placeholders
    # imports SpecError from this module.
    from benchmark.placeholders import resolve_placeholders
    resolved = resolve_placeholders(spec, source=path)

    # Validate the resolved spec (coercion, structural checks). The
    # verbatim copy is returned unvalidated by design: it is a record of
    # what was written, not something that will run, and validating it
    # would reject a ${SEED} the resolved spec legitimately coerced.
    return validate_spec(resolved, source=path), spec


def validate_spec(spec, source="<spec>"):
    '''Validate a spec dict in place, returning it. Raises SpecError.'''
    _reject_unknown(spec, _TOP_KEYS, source)

    _require(spec, "name", source, str)
    _require(spec, "devices", source, list)
    _require(spec, "jobs", source, list)

    # Scalars are COERCED, not type-gated. A ${SEED} placeholder resolves
    # to a string (an environment holds only strings), so a strict
    # isinstance check would reject every placeholder-sourced value. We
    # instead attempt the coercion and raise only when it genuinely
    # cannot be an int — "42" and 42 both become 42; "banana" is refused.
    # This also accepts a literal "seed": "42", which the strict version
    # rejected; that loosening is deliberate (see the placeholder design).
    if "seed" in spec:
        spec["seed"] = _coerce_int(spec["seed"], "seed", source)

    if "config" in spec and not isinstance(spec["config"], str):
        raise SpecError(f"{source}: 'config' must be a path string.")

    if not spec["devices"]:
        raise SpecError(f"{source}: 'devices' is empty — a run needs at "
                        f"least one device.")
    if not spec["jobs"]:
        raise SpecError(f"{source}: 'jobs' is empty — nothing to run.")

    # ── arrival ───────────────────────────────────────────────────────────
    arrival = spec.get("arrival", {"pattern": "batch"})
    if not isinstance(arrival, dict):
        raise SpecError(f"{source}: 'arrival' must be an object.")
    _reject_unknown(arrival, _ARRIVAL_KEYS, f"{source}: arrival")
    pattern = arrival.get("pattern", "batch")
    if pattern not in _ARRIVAL_PATTERNS:
        raise SpecError(
            f"{source}: arrival pattern '{pattern}' is not supported. "
            f"Expected one of {sorted(_ARRIVAL_PATTERNS)}. Poisson arrival "
            f"is deferred — it needs virtual time, and wall-clock sleeps "
            f"would break reproducibility."
        )
    spec["arrival"] = {"pattern": pattern}

    # ── devices ───────────────────────────────────────────────────────────
    seen_ids = set()
    for i, device in enumerate(spec["devices"]):
        where = f"{source}: devices[{i}]"
        if not isinstance(device, dict):
            raise SpecError(f"{where}: must be an object.")
        _reject_unknown(device, _DEVICE_KEYS, where)

        device_id = _require(device, "id", where, str)
        # The spec's id IS the device name. add_device validates it
        # further (reserved words, dN-shaped names); duplicates are
        # caught here so the error names the spec position.
        if device_id in seen_ids:
            raise SpecError(
                f"{where}: duplicate device id '{device_id}'. Device ids "
                f"are names and must be unique within a run."
            )
        seen_ids.add(device_id)

        _require(device, "provider", where, str)
        _require(device, "backend", where, dict)

        if "config" in device and not isinstance(device["config"], str):
            raise SpecError(f"{where}: 'config' must be a path string.")

    # ── jobs ──────────────────────────────────────────────────────────────
    for i, job in enumerate(spec["jobs"]):
        where = f"{source}: jobs[{i}]"
        if not isinstance(job, dict):
            raise SpecError(f"{where}: must be an object.")
        _reject_unknown(job, _JOB_KEYS, where)

        _require(job, "circuit", where, str)

        if "repeat" in job:
            repeat = _coerce_int(job["repeat"], "repeat", where)
            if repeat < 1:
                raise SpecError(
                    f"{where}: 'repeat' must be a positive integer, got "
                    f"{repeat!r}."
                )
            job["repeat"] = repeat

        # A per-job shot count overrides the device-resolved `shots` for
        # this job only (the per-job tier above the device cascade — see
        # kernel _execute). Absent = defer to the device config. A plain
        # positive-int literal, not a ${} placeholder: it carries no
        # secret, so it stays outside the resolved/verbatim split.
        if "shots" in job and job["shots"] is not None:
            # A fractional float is a user error, not a roundable value —
            # reject it before _coerce_int, which would truncate int(10.5)
            # to 10 silently. (This guards the shots callsite specifically;
            # _coerce_int keeps its shared repeat/seed behaviour.) A whole
            # float like 1024.0 is tolerated the way a "1024" string is.
            raw = job["shots"]
            if isinstance(raw, float) and not raw.is_integer():
                raise SpecError(
                    f"{where}: 'shots' must be a whole number, got "
                    f"{raw!r}."
                )
            shots = _coerce_int(raw, "shots", where)
            if shots < 1:
                raise SpecError(
                    f"{where}: 'shots' must be a positive integer, got "
                    f"{shots!r}."
                )
            job["shots"] = shots

        for key in ("max_qubit_error", "max_edge_error",
                    "max_1q_gate_error"):
            if key in job and job[key] is not None:
                job[key] = _coerce_float(job[key], key, where)

        if "exec_on" in job and "no_exec_on" in job:
            raise SpecError(
                f"{where}: 'exec_on' and 'no_exec_on' are mutually "
                f"exclusive — an allow-list already excludes every other "
                f"device."
            )

        for key in ("exec_on", "no_exec_on"):
            if key in job:
                if not isinstance(job[key], list):
                    raise SpecError(f"{where}: '{key}' must be a list of "
                                    f"device ids.")
                unknown = [d for d in job[key] if d not in seen_ids]
                if unknown:
                    raise SpecError(
                        f"{where}: '{key}' names device(s) {unknown} that "
                        f"this spec does not define. Defined: "
                        f"{sorted(seen_ids)}."
                    )

        if "frontend" in job:
            # The spec-level analogue of --frontend: an explicit frontend
            # name for jobs whose extension is ambiguous. Validity as a
            # REGISTERED name is checked at submit time by the resolver,
            # which is where the frontend map lives; here we validate only
            # that it is a non-empty string, mirroring how device
            # references are form-checked here and resolved later.
            if not isinstance(job["frontend"], str) or not job["frontend"].strip():
                raise SpecError(
                    f"{where}: 'frontend' must be a non-empty string naming "
                    f"a registered frontend."
                )

    return spec


def resolve_seed(provider_entry, spec_seed, device_id):
    '''
    Construct one device's provider with the spec's seed.

    Returns (provider_instance, seed_effective, seed_source, warning).
    The warning slot is kept — and always None — because callers record
    it in the log header, and there is no longer anything that can
    conflict.

    THIS USED TO BE A NEGOTIATION. Providers could be registered as
    ready-made instances, so an instance might carry a seed of its own
    while the spec asked for a different one, and the parser had to pick
    a winner and warn about it. Providers are now CLASS-ONLY (see
    docs/REGISTRY.md), so nothing exists to hold a competing seed at the
    moment a spec is read: DevQ constructs the provider here, with this
    seed, or unseeded if the spec gave none. A caller who wants a
    different seed constructs the provider themselves and attaches the
    device with add_device().
    '''
    instance = (provider_entry(seed=spec_seed) if spec_seed is not None
                else provider_entry())
    source = "spec" if spec_seed is not None else "unseeded"
    return instance, spec_seed, source, None


def build_session(spec, registry_owner, source="<spec>", verbatim=None):
    '''
    Turn a validated spec into a built DevQ session.

    registry_owner is a DevQ instance with providers already registered
    — callers register in Python, per DevQ's extension model, so a spec
    can only name components that already exist. A spec naming an
    unregistered provider is an error, not an invitation to import
    something: a data file that can trigger arbitrary imports is a data
    file that can run arbitrary code.

    `spec` is the RESOLVED spec — placeholders substituted — because
    device construction needs real values. `verbatim` is what the header
    records: the spec with ${NAME} placeholders still literal, so a
    resolved secret never reaches the log. It defaults to `spec`, which
    is correct for any spec built in code or loaded without placeholders,
    where the two are identical. A caller with a placeholder spec passes
    both: build from resolved, record verbatim.

    Returns (shell, meta) where meta records the seed resolution per
    device, for the log header.
    '''
    if verbatim is None:
        verbatim = spec
    spec_seed = spec.get("seed")
    warnings  = []
    devices   = []

    for entry in spec["devices"]:
        device_id = entry["id"]
        name      = entry["provider"]
        where     = f"{source}: device '{device_id}'"

        try:
            provider_entry = registry_owner._registry.get("provider", name)
        except Exception:
            provider_entry = None

        if provider_entry is None:
            available = sorted(registry_owner._registry.names("provider"))
            raise SpecError(
                f"{where}: provider '{name}' is not registered. "
                f"Registered providers: {available}. Register it in Python "
                f"before running the spec — specs reference registered "
                f"names and never import by path."
            )

        instance, seed_eff, seed_src, warning = resolve_seed(
            provider_entry, spec_seed, device_id
        )
        if warning:
            warnings.append(warning)

        try:
            device = instance.get_device_from_spec(entry["backend"])
        except SpecError:
            raise
        except Exception as exc:
            raise SpecError(
                f"{where}: provider '{name}' could not build a device from "
                f"backend spec {entry['backend']!r}: "
                f"{type(exc).__name__}: {exc}"
            ) from None

        devices.append({
            "id": device_id, "device": device, "config": entry.get("config"),
            "provider": name, "seed_effective": seed_eff,
            "seed_source": seed_src,
        })

    dq = registry_owner
    for d in devices:
        dq.add_device(d["device"], config_path=d["config"], name=d["id"])

    # The global config path is a DevQ() constructor argument, but the
    # caller had to construct DevQ before the spec was read in order to
    # register providers. Setting it here is the only ordering that
    # works, and the spec's value wins because the caller had no way to
    # supply it earlier.
    if spec.get("config") is not None:
        dq._global_config_path = spec["config"]

    shell = dq.build(interactive=False)

    meta = {
        "spec": verbatim,
        # WHAT WAS REQUESTED, taken from the verbatim spec: a placeholder
        # spec shows "${DEVQ_SEED}" here, not 42. This keeps the field
        # true to its name and consistent with the verbatim `spec` beside
        # it — "requested" is what the author wrote. The per-device
        # seed_effective below is the resolved int the run actually used;
        # requested-vs-effective is the honest provenance pair. For a
        # spec with no placeholder the two coincide, since the literal is
        # already the value.
        "seed_requested": verbatim.get("seed"),
        "devices": [{"id": d["id"], "provider": d["provider"],
                     "kind": d["device"].kind,
                     "index": d["device"].index,
                     "seed_effective": d["seed_effective"],
                     "seed_source": d["seed_source"]} for d in devices],
        "warnings": warnings,
    }
    return shell, meta


def submit_jobs(shell, spec, source="<spec>"):
    '''
    Submit every job the spec describes. repeat:N creates N DISTINCT
    jobs (one QCB per call), not one job run N times — they queue,
    route and are scheduled independently, which is the point of a
    benchmark workload.

    Returns the list of submitted QCBs.
    '''
    from frontends.resolver import resolve_frontend, FrontendResolutionError
    from benchmark.reference import circuit_hash
    from circuits.circuit_rep import CircuitRep

    name_to_index = {ctx.name: ctx.index for ctx in shell.kernel.contexts
                     if ctx.name}
    frontends = getattr(shell, "_frontends", {})
    submitted = []

    for i, job in enumerate(spec["jobs"]):
        where = f"{source}: jobs[{i}]"

        # Two kinds of failure, deliberately handled differently:
        #
        #  - A SPEC-AUTHORING error (no frontend for the extension, or the
        #    file does not exist) is the spec's fault, not the circuit's.
        #    It aborts loudly, because silently recording it would hide a
        #    typo in the workload the user wrote.
        #
        #  - A PARSE error (the file exists and a frontend claimed it, but
        #    its contents are not valid/ supported source) is a property
        #    of the CIRCUIT. It does NOT abort: the circuit becomes a
        #    REJECTED job carrying the parse error as its reason, so it
        #    appears in the results as a rejected row alongside the ones
        #    that ran, and one bad circuit never takes down a 40-circuit
        #    sweep. This is the same umbrella as a well-formed-but-
        #    unsupported circuit (classical control, mid-circuit
        #    measurement): every "DevQ will not run this" is one outcome —
        #    a REJECTED job with a reason — whether the block is detected
        #    at parse time or later. The difference is only the reason
        #    text: "malformed/unsupported source: ..." here versus the
        #    execution-model reason a well-formed circuit carries.
        try:
            frontend = resolve_frontend(
                job["circuit"], frontends, explicit=job.get("frontend")
            )
        except FrontendResolutionError as exc:
            raise SpecError(f"{where}: {exc}") from None

        parse_failed = False
        try:
            circuit = frontend.parse(job["circuit"])
        except FileNotFoundError:
            raise SpecError(f"{where}: circuit not found: {job['circuit']}") from None
        except Exception as exc:
            # A parse failure: build a placeholder circuit marked
            # unrunnable so the kernel rejects the job through the normal
            # path. num_qubits=0 is fine — a rejected job never routes or
            # allocates, so width is never read; the reason is what matters.
            circuit = CircuitRep(0, 0)
            circuit.unrunnable_reason = (
                f"could not parse circuit: {type(exc).__name__}: {exc}")
            parse_failed = True

        def indices(key):
            ids = job.get(key)
            if not ids:
                return None
            return [name_to_index[d] for d in ids]

        # Content hash of this circuit, stamped on every job that runs it
        # so the fidelity metric can join a job's measured counts to its
        # circuit's recorded ideal. Computed once per spec entry (the
        # CircuitRep is parsed once and shared across repeats), not per
        # job. The kernel stores it opaquely; the benchmark layer owns the
        # hashing, keeping the kernel free of any dependency on this layer.
        #
        # An UNPARSEABLE placeholder is a special case: every parse failure
        # yields the same empty CircuitRep(0,0), so content-hashing would
        # collapse all unparseable circuits onto ONE hash — they would
        # collide in the log and the results table would show only the
        # first and dedup the rest. Since a rejected job has no counts and
        # no ideal, its hash is never a real join key; it only needs to be
        # UNIQUE per circuit so each shows its own name. Derive it from the
        # source path instead.
        if parse_failed:
            chash = hashlib.sha256(
                f"unparseable:{job['circuit']}".encode("utf-8")).hexdigest()
        else:
            chash = circuit_hash(circuit)

        for _ in range(job.get("repeat", 1)):
            qcb = shell.kernel.submit_job(
                circuit,
                max_qubit_error = job.get("max_qubit_error"),
                max_edge_error  = job.get("max_edge_error"),
                max_1q_gate_error = job.get("max_1q_gate_error"),
                exec_on         = indices("exec_on"),
                no_exec_on      = indices("no_exec_on"),
                shots           = job.get("shots"),
            )
            qcb.circuit_hash = chash
            qcb.circuit_label = job["circuit"]
            submitted.append(qcb)

    return submitted


def drain(shell, poll_interval=0.01, timeout=300):
    '''
    Run the session to completion and return the number of cycles taken.

    ⚠ DO NOT busy-loop on step(). Stepping while futures are merely
    in flight does no work and emits a cycle_end each time: an early
    version of this loop produced 37,923 empty cycles for a five-job
    workload, burying twenty real events. When there is nothing
    queued, sleep and let the executor make progress instead.

    The kernel is stepped only while work can actually be done —
    something queued, or a resolved future waiting to be collected.
    '''
    import time

    deadline = time.monotonic() + timeout
    cycles   = 0

    while shell.kernel.has_queued() or shell.kernel.has_pending():
        if time.monotonic() > deadline:
            raise SpecError(
                f"workload did not complete within {timeout}s — "
                f"{len(shell.kernel._pending)} job(s) still pending. "
                f"A provider or the executor may be wedged."
            )

        before_queued  = shell.kernel.has_queued()
        before_pending = len(shell.kernel._pending)

        shell.kernel.step()
        cycles += 1

        # A cycle that changed nothing means every remaining job is
        # waiting on a future or on qubits held by one. Stepping again
        # immediately cannot help and only emits empty cycles, so wait
        # for the executor instead.
        made_progress = (shell.kernel.has_queued() != before_queued
                         or len(shell.kernel._pending) != before_pending)
        if not made_progress:
            time.sleep(poll_interval)

    return cycles