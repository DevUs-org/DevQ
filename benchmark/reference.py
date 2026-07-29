'''
Tags: Main

Reference orchestration — the shipped, vendor-neutral machinery that
turns "what circuits ran" into "here is each circuit's ideal", so the
fidelity metric has a noiseless yardstick to compare a noisy run against.

WHAT THIS IS AND IS NOT. This module owns the vendor-NEUTRAL half of the
reference run: identifying circuits, deciding which provider computes
ideals, computing each distinct circuit's ideal once, and shaping the
result into recordable data. It owns NONE of the quantum simulation —
that is a provider capability (BaseProvider.reference_ideal), overridden
by a provider that can faithfully simulate a circuit noiselessly
(IBMSimulatedProvider, via a density-matrix Aer path). So this module has
no qiskit import and no IBM import: it depends on the provider INTERFACE,
never on any concrete provider, and DevQ core ships it without dragging a
simulator in. A run with no reference-capable provider simply produces no
ideals, and fidelity is then reported as None — the population rule, an
honest undefined rather than a fake number.

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

def compute_ideals(circuits, provider):
    '''
    Compute the ideal for each DISTINCT circuit once, keyed by hash.

    `circuits` is any iterable of CircuitReps (typically one per submitted
    job, with heavy repetition — a repeat:20 workload runs one circuit 20
    times). Deduplicating by hash means the noiseless simulation runs once
    per distinct circuit, not once per job.

    A provider that returns None for a given circuit (cannot simulate it —
    a gate it does not know, or an unavailable Aer path) contributes no
    ideal for that hash: the entry is simply absent, and fidelity for jobs
    on that circuit is later reported as None. An absent ideal is not a
    zero distribution.

    Args:
        circuits : iterable of CircuitRep
        provider : the reference-capable provider chosen for the run, or
                   None (in which case no ideals are produced at all).

    Returns:
        dict {circuit_hash: {"ideal": {bitstring: prob}, "label": str|None}}
        — the label is filled by the caller that knows each circuit's
        source path; this function leaves it None, since a CircuitRep does
        not carry its own source. Only hashes with a non-None ideal appear.
    '''
    ideals = {}
    if provider is None:
        return ideals

    for circuit in circuits:
        key = circuit_hash(circuit)
        if key in ideals:
            continue
        ideal = provider.reference_ideal(circuit)
        if ideal is None:
            continue
        ideals[key] = {"ideal": ideal, "label": None}
    return ideals