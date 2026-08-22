'''
Tags: Main

Reference orchestration — the shipped, vendor-neutral machinery that
turns "what circuits ran" into "here is each circuit's ideal", so the
fidelity metric has a noiseless yardstick to compare a noisy run against.

WHAT THIS IS AND IS NOT. This module owns the vendor-NEUTRAL half of the
reference run: identifying circuits, choosing where each circuit's ideal
comes from, computing each distinct circuit's ideal once, and shaping the
result into recordable data. Ideals come from a three-tier precedence: an
ATTACHED reference-capable provider wins outright for a run; failing that,
DevQ's own CORE native statevector engine (Qiskit-free) computes the exact
ideal for pure circuits within a qubit cap; failing that, a registered
provider class overriding reference_ideal is instantiated unattached and
used. The module still imports no concrete provider and no Qiskit — tier 1
depends on the provider INTERFACE, tier 2 on the core engine, and tier 3
resolves a provider through the registry only when reached, so DevQ core
ships without dragging a simulator in unless a study registers one. A circuit
no tier can handle simply produces no ideal, and fidelity is then reported as
None — the population rule, an honest undefined rather than a fake number.

The three-tier model is what frees a run from having to attach a
reference-capable device (and then exclude it from routing on every job) just
to obtain ideals: with no provider attached, the core engine supplies them.
Per-circuit fallback between the engine and the registry tier is safe because
a noiseless ideal is mathematically unique and every tier returns normalised
probabilities, so a cross-device fidelity comparison for a given circuit is
never split across two disagreeing sources.

WHY A CONTENT HASH KEYS THE IDEAL. The ideal is a property of the
CIRCUIT, not of the job or the device: two jobs running the same circuit
on different devices must share one ideal, or a cross-device fidelity
comparison is meaningless. Circuits are therefore keyed by a hash of
their canonical instruction stream, so structurally identical circuits
collapse to one ideal computed once, and the same source path reused for
different content across specs never collides two circuits onto one
ideal. The source path rides ALONGSIDE the hash as a human-readable
label, but is deliberately NOT part of the hash — an inline circuit has
no path, and a path is not the circuit's identity, its contents are.
'''

import hashlib
import json


# ── circuit identity ────────────────────────────────────────────────────

def circuit_hash(circuit):
    '''
    A stable content hash of a CircuitRep — the join key between a job,
    its measured counts, and its ideal.

    Hashes a CANONICAL JSON serialisation of the circuit's identity: its
    qubit and classical widths and its ordered instruction stream (op
    tags, gate names, qubit lists, params, measures, resets — everything
    that determines what the circuit DOES). `sort_keys=True` fixes dict
    key order so the same circuit always serialises identically, and the
    instruction list's own order is meaningful and preserved. Two circuits
    with identical streams hash identically (share an ideal); any
    difference in gates, ordering, params or width changes the hash.

    Params are floats (rx(pi/2) carries its angle), and float repr is
    deterministic within a Python version, so the serialisation is stable
    across runs on the same interpreter — which is all reproducibility
    within a study requires. The hash is not a cross-version guarantee and
    is not meant as one; it is a within-run/within-study dedup and join
    key, not a published constant.

    Returns a hex SHA-256 digest string.
    '''
    payload = {
        "num_qubits": circuit.num_qubits,
        "num_clbits": circuit.num_clbits,
        "instructions": circuit.instructions,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ── choosing who computes ideals ─────────────────────────────────────────

def select_reference_provider(providers):
    '''
    Pick the ONE provider that computes ideals for a whole run, or None if
    none can.

    The ideal is vendor-neutral, so one provider computes ALL of a run's
    ideals — not each device's own provider computing its own, which would
    let two backends produce subtly different ideals for the same circuit
    (different simulators, different rounding) and silently break the
    cross-device comparison. One provider, one ideal per circuit, keyed by
    hash.

    Capability is probed structurally: a provider is reference-capable if
    it overrides reference_ideal (its class's method is not BaseProvider's
    default). We do not call it here — probing by identity avoids running a
    simulation just to ask "can you?". The first capable provider in the
    given order is chosen; a run that wants a specific one orders the
    providers so it comes first (in practice one reference-capable
    provider is registered, and that is the choice).

    Args:
        providers : iterable of provider instances (the run's registered
                    providers)

    Returns:
        a provider instance, or None if none is reference-capable.
    '''
    from providers.base_provider import BaseProvider

    for provider in providers:
        # Reference-capable iff the concrete class overrides the default.
        method = type(provider).reference_ideal
        if method is not BaseProvider.reference_ideal:
            return provider
    return None


# ── computing a run's ideals ─────────────────────────────────────────────

# The native statevector engine allocates a 2**n complex state vector, so
# memory is the ceiling: n=20 is 16 MB, n=24 is 256 MB. Above this the engine
# declines and the registry-search tier (a density-matrix provider, or
# whatever a study registered) takes over — a density matrix is 2**n x 2**n
# and far costlier (16 TB at n=20), so the cheap exact statevector is the
# right primary for pure circuits. The cap is a soft memory guard, not a
# correctness one: a noiseless ideal is unique, identical wherever computed.
_ENGINE_MAX_QUBITS = 20


def _engine_ideal(circuit):
    '''
    Tier 2: the exact ideal from DevQ's native statevector engine, or None if
    the engine cannot handle this circuit (above the qubit cap, or a reset on
    an entangled qubit it declines). Core and Qiskit-free — this is what lets a
    run compute ideals with no reference-capable provider attached, AND what a
    tier-1 provider falls through to when it declines a circuit (e.g. a dynamic
    circuit, whose exact mixed-state ideal the engine computes by branch
    enumeration but Aer's exact path cannot).

    None is not an error; it is the engine saying "not mine", so the caller
    falls through to the registry tier.
    '''
    if circuit.num_qubits > _ENGINE_MAX_QUBITS:
        return None
    # Local import: keep the engine dependency at the tier that uses it, and
    # charge nothing to a caller (a tier-1 run) that never reaches here.
    from engine.statevector import simulate, UnsupportedByEngine
    from engine.gates import UnknownGateError
    try:
        return simulate(circuit)
    except (UnsupportedByEngine, UnknownGateError):
        return None


def _registry_reference_ideal(circuit, registry):
    '''
    Tier 3: find a registered provider CLASS overriding reference_ideal,
    instantiate it unattached, and use it for this circuit. Internalises the
    "dry-run" hand-roll — standing up a reference-capable provider off to the
    side purely to compute an ideal — so a caller never has to.

    Reached only when the engine returned None (a circuit above the qubit cap,
    or a reset on an entangled qubit a statevector cannot represent — the
    engine now handles feedback and mid-circuit measurement itself via branch
    enumeration), so the heavyweight provider (a density-matrix Aer path) is
    constructed lazily and only when actually needed. Returns the ideal, or
    None if no registered provider is reference-capable or the chosen one
    cannot simulate this circuit either.
    '''
    if registry is None:
        return None
    from providers.base_provider import BaseProvider
    try:
        names = registry.names("provider")
    except Exception:
        return None
    for name in names:
        cls = registry.get("provider", name)
        if getattr(cls, "reference_ideal", None) is BaseProvider.reference_ideal:
            continue  # not reference-capable
        try:
            provider = cls()                            # unattached, seedless
            ideal = provider.reference_ideal(circuit)   # ideals are deterministic
        except Exception:
            continue
        if ideal is not None:
            return ideal
    return None


def compute_ideals(circuits, provider, registry=None):
    '''
    Compute the ideal for each DISTINCT circuit once, keyed by hash, via a
    three-tier precedence — attached provider, native engine, registry.

    For each distinct circuit:

      1. ATTACHED reference-capable provider — if `provider` is not None (a
         run that attached one, e.g. IBM/Aer), it computes every ideal and the
         other tiers are not consulted. A study that deliberately picks a
         reference backend gets exactly it.
      2. else CORE native statevector — the engine computes the exact ideal
         for pure circuits within its qubit cap. This frees a run from needing
         an attached reference-capable device: no provider, ideals still
         appear.
      3. else REGISTRY search — when the engine returns None (above the cap,
         or a reset / mid-circuit construct it declines), a registered
         provider class overriding reference_ideal is instantiated unattached
         and used. Internalises the dry-run hand-roll.

    Per-circuit fallback across tiers 2 and 3 is SAFE and does not break the
    "one ideal per circuit" guarantee: a noiseless ideal is mathematically
    unique, so any correct engine yields the identical distribution, and every
    tier returns normalised probabilities (never shot-quantised counts), so
    there is no sampling artifact for two tiers to disagree on. Tier 1 wins
    outright only because a run that attaches a specific reference provider has
    asked for it by name.

    `circuits` is any iterable of CircuitReps (typically one per submitted
    job, with heavy repetition); deduplicating by hash computes each distinct
    circuit's ideal once, not once per job.

    Args:
        circuits : iterable of CircuitRep
        provider : the attached reference-capable provider (tier 1), or None.
        registry : the run's component registry, for the tier-3 search. None
                   disables tier 3 (older callers pass nothing and keep tiers
                   1 and 2).

    Returns:
        dict {circuit_hash: {"ideal": {bitstring: prob}, "label": str|None}}.
        The label is filled by the caller that knows each circuit's source
        path. Only hashes with a non-None ideal from some tier appear.
    '''
    ideals = {}
    for circuit in circuits:
        key = circuit_hash(circuit)
        if key in ideals:
            continue
        # A circuit the frontend marked unrunnable will be REJECTED by the
        # kernel and never produce measured counts, so it has no fidelity to
        # compute. Skip it: no ideal. (Dynamic and mid-circuit circuits are NO
        # LONGER marked unrunnable — they are per-device capabilities that run
        # on a capable provider — so they fall through to the tiers below and
        # DO get an ideal from the engine's branch enumeration.)
        if getattr(circuit, "unrunnable_reason", None) is not None:
            continue

        # Tier 1 is a PREFERENCE, not an exclusion: an attached
        # reference-capable provider is tried first, but if it DECLINES a
        # circuit (returns None — e.g. Aer's exact path cannot give a
        # mixed-state ideal for a dynamic circuit), fall through to the native
        # engine and then the registry, rather than giving up. This is what
        # keeps a provider-attached run (e.g. the IBM-sim study) from reporting
        # None where the engine can compute the exact ideal — the asymmetry
        # that made a provider-attached run WORSE than an unattached one.
        ideal = None
        if provider is not None:
            ideal = provider.reference_ideal(circuit)
        if ideal is None:
            ideal = _engine_ideal(circuit)
        if ideal is None:
            ideal = _registry_reference_ideal(circuit, registry)

        if ideal is None:
            continue
        ideals[key] = {"ideal": ideal, "label": None}
    return ideals