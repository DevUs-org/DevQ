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

SEED RESOLUTION. A spec's `seed` is a DEFAULT, not a guarantee. When a
provider is registered as a class, the parser constructs it with the
spec's seed and there is nothing to conflict. When a ready-made
INSTANCE is registered, the instance's own seed wins and the parser
warns — a collaborator who takes someone else's spec and pins a
different seed in their own code is expressing intent, and should not
have to edit a shared file to do it. The log records seed_requested,
seed_effective and seed_source, so an artifact never claims a seed that
did not run.

  provider registered as   spec seed   effective        conflict?
  ----------------------   ---------   --------------   ---------
  class                    absent      unseeded         no
  class                    42          42               no
  instance, seed=None      42          42 (set_seed)    no
  instance, seed=99        42          99  ← code wins  WARNED
  instance, seed=99        absent      99               no
'''

import json
import os

from devq import DevQError


# Every key the parser understands, per level. Anything outside these
# sets is an error — see _reject_unknown.
_TOP_KEYS    = frozenset({"name", "seed", "config", "devices", "arrival", "jobs"})
_DEVICE_KEYS = frozenset({"id", "provider", "backend", "config"})
_JOB_KEYS    = frozenset({"circuit", "repeat", "max_qubit_error",
                          "max_edge_error", "exec_on", "no_exec_on"})
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


def load_spec(path):
    '''
    Read and validate a workload spec file. Returns the parsed dict.

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

    return validate_spec(spec, source=path)


def validate_spec(spec, source="<spec>"):
    '''Validate a spec dict in place, returning it. Raises SpecError.'''
    _reject_unknown(spec, _TOP_KEYS, source)

    _require(spec, "name", source, str)
    _require(spec, "devices", source, list)
    _require(spec, "jobs", source, list)

    if "seed" in spec and not isinstance(spec["seed"], int):
        raise SpecError(
            f"{source}: 'seed' must be an integer, got "
            f"{type(spec['seed']).__name__} ({spec['seed']!r})."
        )

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

        repeat = job.get("repeat", 1)
        if not isinstance(repeat, int) or repeat < 1:
            raise SpecError(
                f"{where}: 'repeat' must be a positive integer, got {repeat!r}."
            )

        for key in ("max_qubit_error", "max_edge_error"):
            if key in job and job[key] is not None:
                if not isinstance(job[key], (int, float)):
                    raise SpecError(
                        f"{where}: '{key}' must be a number, got "
                        f"{type(job[key]).__name__}."
                    )

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

    return spec


def resolve_seed(provider_entry, spec_seed, device_id):
    '''
    Decide the effective seed for one device's provider.

    Returns (provider_instance, seed_effective, seed_source, warning).
    warning is None or a string the caller should surface and record.

    See the table in this module's docstring. The short version: a
    registered CLASS is constructed with the spec's seed; a registered
    INSTANCE keeps its own seed if it has one, because a collaborator
    pinning a seed in code is expressing intent about a spec they may
    not own.
    '''
    # Registered as a class — construct it. No conflict is possible
    # because nothing existed to hold a competing seed.
    if isinstance(provider_entry, type):
        instance = (provider_entry(seed=spec_seed) if spec_seed is not None
                    else provider_entry())
        source = "spec" if spec_seed is not None else "unseeded"
        return instance, spec_seed, source, None

    instance = provider_entry
    own_seed = getattr(instance, "seed", None)

    # Instance carries a seed AND the spec asks for a different one:
    # code wins, loudly.
    if own_seed is not None and spec_seed is not None and own_seed != spec_seed:
        warning = (
            f"[Spec] Device '{device_id}': spec requests seed {spec_seed}, "
            f"but the registered provider instance was constructed with "
            f"seed={own_seed}, which takes precedence. Recording effective "
            f"seed {own_seed}. Register the provider class instead of an "
            f"instance if the spec's seed should apply."
        )
        return instance, own_seed, "provider instance (spec overridden)", warning

    if own_seed is not None:
        return instance, own_seed, "provider instance", None

    # Instance was constructed unseeded and the spec supplies one. Safe
    # to apply: set_seed's contract is that no device has been built
    # yet, which holds because the parser builds them next.
    if spec_seed is not None:
        instance.set_seed(spec_seed)
        applied = getattr(instance, "seed", None)
        warning = None
        if applied != spec_seed:
            warning = (
                f"[Spec] Device '{device_id}': provider "
                f"{type(instance).__name__} did not apply the spec's seed "
                f"({spec_seed}); its seed is {applied!r}. The provider "
                f"probably does not implement set_seed(). Results will not "
                f"be reproducible."
            )
        return instance, applied, "spec", warning

    return instance, None, "unseeded", None


def build_session(spec, registry_owner, source="<spec>"):
    '''
    Turn a validated spec into a built DevQ session.

    registry_owner is a DevQ instance with providers already registered
    — callers register in Python, per DevQ's extension model, so a spec
    can only name components that already exist. A spec naming an
    unregistered provider is an error, not an invitation to import
    something: a data file that can trigger arbitrary imports is a data
    file that can run arbitrary code.

    Returns (shell, meta) where meta records the seed resolution per
    device, for the log header.
    '''
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
        "spec": spec,
        "seed_requested": spec_seed,
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
    from circuits.qasm_loader import load_qasm

    name_to_index = {ctx.name: ctx.index for ctx in shell.kernel.contexts
                     if ctx.name}
    submitted = []

    for i, job in enumerate(spec["jobs"]):
        where = f"{source}: jobs[{i}]"
        try:
            circuit = load_qasm(job["circuit"])
        except FileNotFoundError:
            raise SpecError(f"{where}: circuit not found: {job['circuit']}") from None
        except Exception as exc:
            raise SpecError(
                f"{where}: could not load {job['circuit']}: "
                f"{type(exc).__name__}: {exc}"
            ) from None

        def indices(key):
            ids = job.get(key)
            if not ids:
                return None
            return [name_to_index[d] for d in ids]

        for _ in range(job.get("repeat", 1)):
            submitted.append(shell.kernel.submit_job(
                circuit,
                max_qubit_error = job.get("max_qubit_error"),
                max_edge_error  = job.get("max_edge_error"),
                exec_on         = indices("exec_on"),
                no_exec_on      = indices("no_exec_on"),
            ))

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