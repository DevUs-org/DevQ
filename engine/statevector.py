'''
Tags: Main

engine.statevector — the native statevector core.

Applies a CircuitRep's gates to a state vector and reads off the exact,
noiseless measured-bit distribution — the fidelity yardstick — with no
Qiskit and no provider. simulate() returns {bitstring: probability} at the
Option-B classical width, exactly the shape a reference-capable provider's
reference_ideal() returns, because the engine implements the SAME core
output contract (declared on BaseProvider) rather than borrowing a
provider's implementation of it.

STATE. The state is a 2**n complex vector over n = circuit.num_qubits, in
the little-endian basis DevQ uses throughout: basis-index bit q is qubit q
(qubit 0 is the least-significant bit). This matches Qiskit and the width
rule's `(index >> qubit) & 1` marginalisation, so the engine's bitstrings
align character-for-character with a provider's. The initial state is
|0...0>.

APPLICATION. Gates are applied in SOURCE ORDER over circuit.instructions —
a reset before versus after a two-qubit gate means different things, so the
order is honoured, not reordered. Each gate is embedded into the full state
by bit-indexing on the acted qubits rather than by materialising a 2**n
matrix: a one-qubit gate touches amplitude pairs differing only in its
qubit's bit; a controlled gate touches only pairs whose control bit is set;
swap/ecr/ccx/cswap follow the permutation or 4x4 their kind defines. The
per-gate unitaries and their kinds come from engine.gates, whose matrices
were locked against Qiskit before this file consumed them.

MEASUREMENT is terminal ON THE FAST PATH. For a plain circuit, DevQ's
execution model measures at the end, and simulate() applies gates and resets
to one statevector, then marginalises. A circuit that operates on a qubit
AFTER measuring it (mid-circuit measurement) or uses classical feedback
(conditional gates) is handled by a SEPARATE exact path — branch enumeration
(_simulate_dynamic), routed to when circuit.is_dynamic or
has_mid_circuit_measurement. A mid-circuit measurement produces a classical
MIXTURE a single statevector cannot hold; branch enumeration represents it
exactly as a set of weighted pure branches, splitting at each measurement and
reading the ideal from each branch's recorded classical register — exact and
deterministic, no sampling. (These circuits were once rejected upstream, so
the fast path never faced a post-measurement state; that upstream rejection
became a per-device capability, so the branch path now handles them here
rather than the fast path silently mis-simulating them.) A reset BEFORE
measurement is legitimate on the fast path too: it returns that qubit to |0>,
collapsing and renormalising the surviving mass. A reset on an ENTANGLED
qubit is declined on both paths (the resulting mixed state has no statevector
form).

OUTPUT CONTRACT (BaseProvider). Width is BaseProvider._counts_width — the
declared classical register, or the qubit count when no creg is declared.
Explicit measures place each qubit's outcome at its own clbit position;
a circuit with no measures falls back to measuring every qubit onto the
matching classical bit. Unmeasured classical bits read 0. Bitstrings follow
the Qiskit rendering: clbit j sits at string position width-1-j, so the
engine's keys align with any provider's for a fidelity comparison.
'''

import numpy as np

from providers.base_provider import BaseProvider
from engine.gates import gate_spec, UnknownGateError


# Below this magnitude a probability is floating-point dust, not signal.
# A statevector's |amplitude|**2 is cleaner than a density matrix's, but a
# circuit with irrational amplitudes (rotations, QASMBench) still lands tiny
# residues around true zeros; a raw `p > 0` keeps them, bloating the
# distribution with ~1e-17 keys. Clamp at the source, nine orders of
# magnitude above the residue and far below any real probability. This
# mirrors the same clamp the output contract's other implementers apply, so
# the engine's keys match theirs rather than carrying extra dust ones.
_DUST = 1e-9


def _resolve_measure_map(circuit, width):
    '''
    The (qubit, clbit) pairs to read out, with the measure-all fallback
    applied — the engine's own resolution of the core output contract's
    measurement rule.

    Explicit measures are used verbatim. A circuit with none falls back to
    measuring each qubit onto its own classical bit (q -> c[q]), bounded by
    the declared width. This is the same rule the contract states; the
    engine implements it here rather than importing a provider's copy, so
    the engine depends on no provider.
    '''
    explicit = [(i["qubit"], i["clbit"])
                for i in circuit.instructions if i["op"] == "measure"]
    if explicit:
        return explicit
    return [(q, q) for q in range(min(circuit.num_qubits, width))]


def _apply_1q(state, u, qubit, n):
    '''
    Apply a 2x2 unitary `u` to `qubit` of an n-qubit state, in place-style
    (returns the new state). Amplitudes are paired by the acted qubit's bit:
    for every basis index with that bit 0, its partner is the index with the
    bit set, and the pair is mixed by `u`. Reshaping the flat 2**n vector to
    put the acted axis on its own dimension does this for all pairs at once,
    without building a 2**n matrix.
    '''
    # View the state as [..., 2, ...] with the acted qubit as one axis.
    # Little-endian: qubit q is bit q, i.e. stride 2**q. Reshape to
    # (high, 2, low) where low = 2**q, then apply u over the middle axis.
    low = 1 << qubit
    high = 1 << (n - qubit - 1)
    s = state.reshape(high, 2, low)
    # new[:, 0, :] = u00*old0 + u01*old1 ; new[:, 1, :] = u10*old0 + u11*old1
    a0 = s[:, 0, :]
    a1 = s[:, 1, :]
    out = np.empty_like(s)
    out[:, 0, :] = u[0, 0] * a0 + u[0, 1] * a1
    out[:, 1, :] = u[1, 0] * a0 + u[1, 1] * a1
    return out.reshape(-1)


def _apply_controlled(state, u, control, target, n):
    '''
    Apply a controlled-`u` (2x2 on `target` when `control` is set) to an
    n-qubit state. Only amplitude pairs whose control bit is 1 are mixed;
    all others pass through unchanged.
    '''
    dim = 1 << n
    out = state.copy()
    cbit = 1 << control
    tbit = 1 << target
    for idx in range(dim):
        # Act once per pair: handle the target-0 member, touch its partner.
        if (idx & cbit) and not (idx & tbit):
            j = idx | tbit            # partner with target bit set
            a0 = state[idx]
            a1 = state[j]
            out[idx] = u[0, 0] * a0 + u[0, 1] * a1
            out[j]   = u[1, 0] * a0 + u[1, 1] * a1
    return out


def _apply_ecr(state, q0, q1, n):
    '''
    Apply the 4x4 ECR gate on (q0, q1). ECR is not a controlled gate, so the
    full 4x4 acts on the two-qubit subspace: for each setting of the other
    n-2 qubits, the four amplitudes indexed by (q1, q0) are mixed by ECR in
    the little-endian sub-basis (sub-index = 2*bit(q1) + bit(q0)), matching
    how engine.gates.ECR was verified against Qiskit.
    '''
    from engine.gates import ECR
    dim = 1 << n
    out = state.copy()
    b0 = 1 << q0
    b1 = 1 << q1
    seen = np.zeros(dim, dtype=bool)
    for idx in range(dim):
        if seen[idx]:
            continue
        # The four indices of this 2-qubit subspace, clearing q0/q1 then
        # setting them to each (v1, v0) combination.
        base = idx & ~b0 & ~b1
        members = [base,                 # (q1,q0) = (0,0) -> sub 0
                   base | b0,            # (0,1) -> sub 1
                   base | b1,            # (1,0) -> sub 2
                   base | b1 | b0]       # (1,1) -> sub 3
        amps = np.array([state[m] for m in members], dtype=complex)
        new = ECR @ amps
        for m, val in zip(members, new):
            out[m] = val
            seen[m] = True
    return out


def _apply_swap(state, q0, q1, n):
    '''Swap qubits q0 and q1 by permuting amplitudes.'''
    dim = 1 << n
    out = state.copy()
    b0 = 1 << q0
    b1 = 1 << q1
    for idx in range(dim):
        v0 = (idx >> q0) & 1
        v1 = (idx >> q1) & 1
        if v0 != v1:
            j = (idx & ~b0 & ~b1) | (v1 << q0) | (v0 << q1)
            out[j] = state[idx]
    return out


def _apply_ccx(state, c1, c2, t, n):
    '''Toffoli: flip target t when both controls c1, c2 are set.'''
    dim = 1 << n
    out = state.copy()
    for idx in range(dim):
        if (idx >> c1) & 1 and (idx >> c2) & 1:
            out[idx ^ (1 << t)] = state[idx]
    return out


def _apply_cswap(state, c, a, b, n):
    '''Fredkin: swap qubits a, b when control c is set.'''
    dim = 1 << n
    out = state.copy()
    ba = 1 << a
    bb = 1 << b
    for idx in range(dim):
        if (idx >> c) & 1:
            va = (idx >> a) & 1
            vb = (idx >> b) & 1
            if va != vb:
                j = (idx & ~ba & ~bb) | (vb << a) | (va << b)
                out[j] = state[idx]
        # control 0: amplitude stays at idx (already copied)
    return out


class UnsupportedByEngine(Exception):
    '''
    Raised when a circuit is well-formed and in-vocabulary but reaches a
    construct the native statevector engine cannot faithfully simulate —
    currently, a `reset` on an ENTANGLED qubit.

    WHY AN ENTANGLED RESET DECLINES. A pure statevector cannot represent the
    MIXED state that a reset produces when the reset qubit is entangled:
    resetting q0 of a Bell pair leaves q1 in a genuinely mixed 50/50 state,
    whose correct ideal is {"00": 0.5, "10": 0.5}. A statevector would
    collapse that to {"00": 1.0} — a plausible, wrong ideal, exactly the
    silent failure the whole engine exists to avoid.

    A reset on an UNENTANGLED qubit is different: its reduced state is pure,
    so the post-reset whole-register state is still a pure product |0> ⊗
    (rest), which a statevector holds exactly. The engine therefore HANDLES
    the separable reset (the common case — a leading reset, or a reset on a
    qubit not yet entangled) and declines only the entangled one. This
    matches the engine's contract: simulate what can be simulated exactly,
    and hand off what cannot. The caller treats this decline the same as an
    unknown gate — the engine has no ideal for this circuit, so the reference
    path falls back to a registered reference-capable provider, or records
    None. It is a capability boundary, not an error.
    '''


def _qubit_is_separable(state, qubit, n):
    '''
    Is `qubit` unentangled with the rest of the register in `state`?

    A reset returns the qubit to |0>. A statevector represents that EXACTLY
    only when the qubit is separable — its reduced state is pure, so the
    post-reset whole-register state is still a pure product |0> ⊗ (rest).
    When the qubit is entangled, resetting leaves the REST in a genuinely
    mixed state no statevector can hold, and the engine must decline.

    Separability is tested on the single-qubit reduced density matrix
    rho = Tr_rest(|psi><psi|): the qubit is separable iff rho is pure, i.e.
    Tr(rho^2) == 1. rho is 2x2, formed by summing amplitude products over
    the other qubits' basis settings.
    '''
    low = 1 << qubit
    high = 1 << (n - qubit - 1)
    s = state.reshape(high, 2, low)
    a0 = s[:, 0, :].reshape(-1)
    a1 = s[:, 1, :].reshape(-1)
    r00 = np.vdot(a0, a0).real            # <0|rho|0>
    r11 = np.vdot(a1, a1).real            # <1|rho|1>
    r01 = np.vdot(a1, a0)                 # <0|rho|1>  (vdot conjugates its first arg)
    # Tr(rho^2) = r00^2 + r11^2 + 2|r01|^2 for a Hermitian 2x2.
    purity = r00 * r00 + r11 * r11 + 2.0 * (abs(r01) ** 2)
    return abs(purity - 1.0) < 1e-9


def _apply_reset(state, qubit, n):
    '''
    Reset `qubit` to |0>, EXACTLY — for the separable case only.

    The caller (simulate) checks separability first and reaches here only
    when the qubit is unentangled, so the state factorises as
    (a|0> + b|1>)_q ⊗ |rest>. After reset the qubit is |0> and the rest is
    unchanged: |0>_q ⊗ |rest>. Because the qubit is separable, the |rest>
    vector appears (up to the scalar a or b) in BOTH branches, so we recover
    it by summing the branches' probability into the |0> slot: the new |0>
    amplitude for each rest-index is sqrt(|a0|^2 + |a1|^2) carrying the rest
    phase. Taking the magnitude this way moves the |1> population to |0>
    rather than discarding it — the reason a naive "zero the |1> branch"
    fails when the qubit is certainly |1>.
    '''
    low = 1 << qubit
    high = 1 << (n - qubit - 1)
    s = state.reshape(high, 2, low)
    a0 = s[:, 0, :]
    a1 = s[:, 1, :]
    # Combined magnitude per rest-index; phase taken from whichever branch
    # carries it (for a separable qubit both branches share the rest phase up
    # to the qubit scalar, so the dominant branch's phase is the rest phase).
    mag = np.sqrt(np.abs(a0) ** 2 + np.abs(a1) ** 2)
    # Rest phase: use a0 where it is non-trivial, else a1.
    ref = np.where(np.abs(a0) >= np.abs(a1), a0, a1)
    phase = np.ones_like(ref)
    nz = np.abs(ref) > 1e-15
    phase[nz] = ref[nz] / np.abs(ref[nz])
    out = np.zeros_like(s)
    out[:, 0, :] = mag * phase
    result = out.reshape(-1)
    norm = np.linalg.norm(result)
    if norm > 0:
        result = result / norm
    return result


def _apply_gate_inst(state, inst, n):
    '''
    Apply one `gate` instruction to a statevector and return the new state.
    Shared by the plain fast path and the dynamic branch-enumeration path so
    the gate vocabulary is defined once and cannot diverge between them.
    '''
    spec = gate_spec(inst["gate"].lower())
    qubits = inst["qubits"]
    params = inst.get("params", [])
    kind = spec.kind
    if kind == "u1":
        return _apply_1q(state, spec.unitary(params), qubits[0], n)
    if kind == "ctrl":
        return _apply_controlled(
            state, spec.unitary(params), qubits[0], qubits[1], n)
    if kind == "ecr":
        return _apply_ecr(state, qubits[0], qubits[1], n)
    if kind == "swap":
        return _apply_swap(state, qubits[0], qubits[1], n)
    if kind == "ccx":
        return _apply_ccx(state, qubits[0], qubits[1], qubits[2], n)
    if kind == "cswap":
        return _apply_cswap(state, qubits[0], qubits[1], qubits[2], n)
    # An engine.gates spec with a kind this core does not handle is an
    # internal inconsistency, not a user error.
    raise UnknownGateError(
        f"gate '{inst['gate']}' has unhandled kind '{kind}'")


# Branches whose probability weight falls below this are pruned: they
# contribute nothing measurable to the ideal and pruning bounds the branch
# count. The threshold is far below any bitstring probability that rounds to a
# non-zero value in a reported ideal, so pruning cannot change the result.
_BRANCH_PRUNE = 1e-12


def _simulate_dynamic(circuit, n, width):
    '''
    EXACT ideal for a circuit with classical feedback and/or mid-circuit
    measurement, by branch enumeration (collapse-and-continue).

    A mid-circuit measurement turns a pure state into a classical MIXTURE over
    its outcomes — which a single statevector cannot hold. Instead of sampling
    that mixture (seed-dependent, inexact), this represents it EXACTLY as a set
    of pure BRANCHES: each branch is (statevector, probability weight,
    classical-register contents). At a measurement, every branch SPLITS into
    its outcome-0 and outcome-1 sub-branches, each renormalised onto that
    outcome and weighted by its Born probability, recording the bit it wrote.
    At a conditional, the guarded gate is applied only to branches whose
    recorded classical bits satisfy the condition. The ideal is the
    probability-weighted sum over branches of each branch's classical-register
    value.

    This is exact and DETERMINISTIC — no seed, byte-reproducible — because the
    mixture is enumerated, not sampled. The cost is 2^(branching measurements)
    branches, bounded in practice because only a measurement whose result is
    later read or whose qubit is reused forces a split, low-weight branches are
    pruned, and identical branches could merge (not needed for the small
    dynamic circuits this serves). READOUT is from each branch's recorded
    clbits, NOT from marginalising a final statevector: a mid-circuit measure's
    outcome is fixed when it happens, and a later reset/reuse would erase it
    from the quantum state, so the classical record is the only faithful
    source.

    A reset on an entangled qubit is still declined (UnsupportedByEngine),
    exactly as in the plain path: within a single branch a reset must be
    separable to stay pure. (After a measurement collapse the measured qubit is
    separable, so a measure-then-reset on the same qubit — the common
    mid-circuit idiom — is fine; only a reset on a qubit still entangled with
    others in that branch is declined.)
    '''
    init = np.zeros(1 << n, dtype=complex)
    init[0] = 1.0
    # Each branch: [state, weight, {clbit: bit}].
    branches = [[init, 1.0, {}]]

    for inst in circuit.instructions:
        op = inst["op"]
        if op == "gate":
            for b in branches:
                b[0] = _apply_gate_inst(b[0], inst, n)
        elif op == "reset":
            q = inst["qubit"]
            for b in branches:
                if not _qubit_is_separable(b[0], q, n):
                    raise UnsupportedByEngine(
                        f"reset on qubit {q} follows entanglement within a "
                        f"measurement branch; the resulting mixed state cannot "
                        f"be represented, so the reference path falls back")
                b[0] = _apply_reset(b[0], q, n)
        elif op == "measure":
            q = inst["qubit"]
            cb = inst["clbit"]
            idx = np.arange(1 << n)
            new = []
            for state, weight, clbits in branches:
                for outcome in (0, 1):
                    proj = np.where(((idx >> q) & 1) == outcome, state, 0)
                    p = float(np.vdot(proj, proj).real)
                    if weight * p <= _BRANCH_PRUNE:
                        continue
                    nc = dict(clbits)
                    nc[cb] = outcome
                    new.append([proj / np.sqrt(p), weight * p, nc])
            branches = new
        elif op == "conditional":
            cbs = inst["condition"]["clbits"]
            val = inst["condition"]["value"]
            body = inst["body"]
            for b in branches:
                actual = sum((b[2].get(cb, 0) << i) for i, cb in enumerate(cbs))
                if actual == val:
                    b[0] = _apply_gate_inst(b[0], body, n)

    # Readout: each branch contributes its recorded classical register (width
    # bits, clbit 0 = least-significant, matching the plain path's bitstring
    # convention), weighted by the branch probability. Unwritten clbits read 0.
    dist = {}
    for _state, weight, clbits in branches:
        key = "".join(str(clbits.get(i, 0)) for i in range(width - 1, -1, -1))
        dist[key] = dist.get(key, 0.0) + weight
    return {k: v for k, v in dist.items() if v > 1e-9}


def simulate(circuit):
    '''
    The exact, noiseless measured-bit distribution for a circuit:
    {bitstring: probability} at the Option-B classical width.

    Walks circuit.instructions in source order, applying each gate to the
    state vector and each reset in place, then marginalises the final
    |amplitude|**2 onto the classical register per the output contract.

    Args:
        circuit : CircuitRep

    Returns:
        dict {bitstring: probability}, keys of length _counts_width(circuit).

    Raises:
        UnknownGateError : a gate name outside engine.gates' vocabulary. The
            caller (the reference path) catches this and treats it as "no
            ideal from the engine" — falling back to a registered
            reference-capable provider, or recording None — never a forged
            distribution.
    '''
    n = circuit.num_qubits
    width = BaseProvider._counts_width(circuit)

    # Classical feedback (conditional ops) and mid-circuit measurement (a
    # qubit measured then reused) turn the state into a classical MIXTURE that
    # a single statevector cannot hold. The engine handles these EXACTLY by
    # branch enumeration (collapse-and-continue) rather than declining: see
    # _simulate_dynamic. This keeps the engine's ideal exact and
    # byte-reproducible for these circuits — no sampling, no seed — so tier 2
    # of the reference path supplies them Qiskit-free. (Before this the engine
    # relied on these being rejected upstream and would otherwise have SILENTLY
    # dropped conditionals and mid-circuit measures, returning a false ideal.)
    if circuit.is_dynamic or circuit.has_mid_circuit_measurement:
        return _simulate_dynamic(circuit, n, width)

    # A width-0 / qubit-0 circuit has a single trivial amplitude.
    if n == 0:
        return {"": 1.0} if width == 0 else {"0" * width: 1.0}

    state = np.zeros(1 << n, dtype=complex)
    state[0] = 1.0  # |0...0>

    for inst in circuit.instructions:
        op = inst["op"]
        if op == "gate":
            spec = gate_spec(inst["gate"].lower())
            qubits = inst["qubits"]
            params = inst.get("params", [])
            kind = spec.kind
            if kind == "u1":
                state = _apply_1q(state, spec.unitary(params), qubits[0], n)
            elif kind == "ctrl":
                state = _apply_controlled(
                    state, spec.unitary(params), qubits[0], qubits[1], n)
            elif kind == "ecr":
                state = _apply_ecr(state, qubits[0], qubits[1], n)
            elif kind == "swap":
                state = _apply_swap(state, qubits[0], qubits[1], n)
            elif kind == "ccx":
                state = _apply_ccx(state, qubits[0], qubits[1], qubits[2], n)
            elif kind == "cswap":
                state = _apply_cswap(state, qubits[0], qubits[1], qubits[2], n)
            else:
                # An engine.gates spec with a kind this core does not handle
                # is an internal inconsistency, not a user error.
                raise UnknownGateError(
                    f"gate '{inst['gate']}' has unhandled kind '{kind}'")
        elif op == "reset":
            # A reset is exact only when its qubit is separable — then the
            # rest factorises and returning the qubit to |0> yields a pure
            # product state. When the qubit is entangled, the reset leaves the
            # rest MIXED, which a statevector cannot hold, so decline and let
            # the caller fall back. This is the engine's contract: simulate
            # what it can exactly, hand off what it cannot.
            q = inst["qubit"]
            if not _qubit_is_separable(state, q, n):
                raise UnsupportedByEngine(
                    f"reset on qubit {q} follows entanglement; the resulting "
                    f"mixed state cannot be represented by a statevector, so "
                    f"the reference path falls back to a reference-capable "
                    f"provider")
            state = _apply_reset(state, q, n)
        elif op == "measure":
            # Terminal measurement: recorded, not applied here. The
            # marginalisation below reads it out. (Mid-circuit measurement is
            # rejected upstream, so a measure never precedes a later gate.)
            continue

    probs = np.abs(state) ** 2
    measure_map = _resolve_measure_map(circuit, width)
    return _marginalise(probs, measure_map, width)


def run(circuit, shots, seed=None):
    '''
    Sample `shots` measurement outcomes from the circuit's exact
    distribution: {bitstring: integer count}, the counts a noiseless run of
    this circuit would produce.

    This is simulate() plus finite sampling — the exact probabilities are
    computed once, then drawn from `shots` times. Where simulate() gives the
    ideal (the fidelity yardstick), run() gives realisable counts without an
    Aer-backed device, for callers who want a gate-honest noiseless execution
    rather than the uniform mock devq.simulated returns.

    Args:
        circuit : CircuitRep
        shots   : positive int, the number of samples.
        seed    : int for a reproducible draw, or None for an unseeded one.
                  With a fixed seed the same circuit and shots reproduce the
                  same counts exactly; the seed is explicit here rather than
                  provider-derived because run() is a standalone function, not
                  a provider with a submission counter.

    Returns:
        dict {bitstring: int}, integer counts summing to `shots`, keyed at the
        Option-B classical width. Outcomes with zero draws are omitted.

    Raises:
        UnknownGateError / UnsupportedByEngine : propagated from simulate()
            for a circuit the engine cannot faithfully handle. run() does not
            swallow these — the caller decides the fallback, exactly as for
            simulate().
        ValueError : shots is not a positive integer.
    '''
    if not isinstance(shots, int) or isinstance(shots, bool) or shots < 1:
        raise ValueError(f"shots must be a positive integer, got {shots!r}")

    dist = simulate(circuit)  # may raise; propagated by contract

    # Draw `shots` outcomes from the exact distribution in one multinomial.
    # Order the support deterministically so a fixed seed is reproducible
    # regardless of dict iteration order.
    keys = sorted(dist)
    p = np.array([dist[k] for k in keys], dtype=float)
    # simulate()'s probabilities sum to 1 within floating-point tolerance;
    # renormalise defensively so multinomial gets a clean distribution.
    total = p.sum()
    if total > 0:
        p = p / total
    rng = np.random.default_rng(seed)
    draws = rng.multinomial(shots, p)
    return {k: int(c) for k, c in zip(keys, draws) if c > 0}


def _marginalise(probs, measure_map, width):
    '''
    Fold per-basis-state probabilities onto the classical register, per the
    core output contract. `probs` is indexed by the integer whose bit q
    (little-endian) is qubit q's value. For each basis index with
    above-dust probability, read each measured qubit's bit and place it at
    its clbit position (clbit j at string position width-1-j, the Qiskit
    rendering), then accumulate. Unmeasured classical bits stay 0.

    Pure arithmetic, so a test can hand it a synthetic probability vector
    and a map and assert exact numbers — the marginalisation is where a
    wrong index mapping would hide.
    '''
    out = {}
    for index, p in enumerate(probs):
        if p < _DUST:
            continue
        bits = ["0"] * width
        for qubit, clbit in measure_map:
            bits[width - 1 - clbit] = str((index >> qubit) & 1)
        key = "".join(bits)
        out[key] = out.get(key, 0.0) + float(p)
    return out