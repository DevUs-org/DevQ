# DevQ Cost Model & Routing Formulas

Formal statement of DevQ's scoring mathematics, kept out of the README so
that the README stays a usage document. This file is the canonical
reference for the formulas and the source for the corresponding sections
of any write-up.

Implemented in `kernel/memory/allocators/noise_graph_allocator.py`
(block selection) and `kernel/router/noise_router.py` (device
selection). Weight resolution and normalisation live in
`config/config_loader.py`. The values below are what `qconfig` reports
and what [`TEST_BLOCKS.md`](TEST_BLOCKS.md) asserts against.

**Scope.** Phase 5.1. The Phase 5.3 metrics layer — the quantities
computed from a completed run — is defined in [`METRICS.md`](METRICS.md),
in this notation.

---

## Notation

| Symbol | Meaning |
|---|---|
| $Q$ | set of physical qubits on a device |
| $E$ | set of undirected coupling edges $(u,v)$, $u < v$ |
| $\varepsilon_q$ | readout error rate of qubit $q$ (`device.qubit_error(q)`) |
| $\varepsilon_{uv}$ | two-qubit gate error rate on edge $(u,v)$ (`device.edge_error(u,v)`) |
| $B \subseteq Q$ | a candidate block: a connected set of qubits, of size $n$ |
| $n$ | qubits required by the circuit |
| $\alpha, \beta$ | `qubit_error_weight`, `edge_error_weight`, with $\alpha + \beta = 1$ |
| $w_q, w_n$ | `router_queue_weight`, `router_noise_weight`, with $w_q + w_n = 1$ |
| $D$ | set of candidate devices for a job (already filtered by feasibility and `--exec`/`--no-exec`) |

## Block cost $S$

The allocator scores each candidate block $B$ by summing its qubit
errors and the errors of the edges *internal* to it (edges with exactly
one endpoint in $B$ are not charged, since the circuit never uses them):

$$S(B) = \alpha \sum_{q \in B} \varepsilon_q + \beta \sum_{(u,v) \in E(B)} \varepsilon_{uv}$$

where $E(B) = \{(u,v) \in E : u \in B \text{ and } v \in B\}$ is the set
of edges internal to the block.

`NoiseGraphAllocator` returns $\arg\min_B S(B)$ over all connected
blocks of size $n$ reachable from an eligible starting qubit. Thresholds
are **hard constraints applied before scoring**, not penalty terms: with
`--max-qubit-error` $\tau_q$ and `--max-edge-error` $\tau_e$, the
eligible sets are $\{q : \varepsilon_q \le \tau_q\}$ and
$\{(u,v) : \varepsilon_{uv} \le \tau_e\}$ (inclusive bounds), and a job
with no feasible block is REJECTED rather than assigned a poor mapping.

Weights are normalised so that $\alpha + \beta = 1$. Only the ratio
affects $\arg\min_B S(B)$, so normalisation leaves allocator decisions
unchanged while putting $S$ on one comparable scale across devices —
which is what makes the router's use of $S$ meaningful. The defaults
$\alpha = 0.1$, $\beta = 0.9$ reflect two-qubit gate error being the
dominant NISQ noise source.

## Device score

For each candidate device $d \in D$ the router computes two raw terms.
Queue pressure counts both waiting and running work:

$$p_d = \text{queued}(d) + \text{running}(d)$$

Noise cost is a *best-case* estimate: the router dry-runs device $d$'s
own configured allocator against a fresh, fully-free pool clone, and
scores the mapping $B_d^\ast$ it returns using the **global-scope**
$\alpha, \beta$:

$$c_d = S(B_d^\ast)$$

where $B_d^\ast$ is the mapping returned by device $d$'s allocator run
against a free pool.

Using one global $(\alpha, \beta)$ here rather than each device's own
weights is deliberate — it is a single uniform ruler, so scores stay
comparable across devices that may be configured differently. Note that
$S$ is applied to whatever mapping the device's allocator actually
returns, so a Static-configured device is scored on the noise-oblivious
block Static would really pick. If allocation unexpectedly fails,
$c_d = \infty$.

Both terms are min-max normalised across the candidate set before
weighting, because queue depths are small integers while noise costs sit
around $0.01$–$0.1$ and raw mixing would let one term silently dominate.
For a raw vector $x$ over $D$:

$$\hat{x}_d = \frac{x_d - \min_{d' \in D} x_{d'}}{\max_{d' \in D} x_{d'} - \min_{d' \in D} x_{d'}}$$

with $\hat{x}_d = 0$ for all $d$ when the span is zero, and
$\hat{x}_d = 1$ for any $x_d = \infty$. The device score is then

$$\text{score}(d) = w_q \hat{p}_d + w_n \hat{c}_d$$

and the router selects $\arg\min_{d \in D} \text{score}(d)$, breaking
ties by lower device index so routing is deterministic.

**A consequence worth noting.** Min-max normalisation is relative to the
candidate set, so with two candidates the better device always
normalises to $0$ and the worse to $1$ on each term independently. Ties
at $w_q = w_n = 0.5$ are therefore common in two-device sessions
whenever one device wins on queue and the other on noise — both score
$0.5$, and the lower index wins. This is expected behaviour, not a
degenerate case, and it is why several test blocks pin `--exec` to make
routing assertions unambiguous.

---

## Worked values

Reference values on the pinned stack (qiskit-ibm-runtime 0.45.1), with
default weights $\alpha = 0.1$, $\beta = 0.9$. These reproduce the
mappings asserted in [`TEST_BLOCKS.md`](TEST_BLOCKS.md) Blocks 2 and 4.

**Bell circuit ($n = 2$).** Every connected pair is a candidate block.

| Device | Block $B$ | $\sum \varepsilon_q$ | $\sum \varepsilon_{uv}$ | $S(B)$ |
|---|---|---|---|---|
| d1 `fakenairobiv2` | $\{1, 2\}$ | $0.0199 + 0.0193$ | $0.0070$ | **$0.0102$** |
| d2 `fakelagosv2` | $\{1, 3\}$ | $0.1362 + 0.0167$ | $0.0107$ | **$0.0249$** |

Both are the $\arg\min$ over their device's candidate pairs, which is why
Block 2 routes a bell job to d1: $0.0102 < 0.0249$ on the router's shared
yardstick.

**GHZ circuit ($n = 3$) on d2, block $\{3, 4, 5\}$.** This case shows the
$E(B)$ rule doing real work. The block's internal edges are $(3,5)$ and
$(4,5)$; edges $(1,3)$ and $(5,6)$ each have exactly one endpoint in $B$
and are **not** charged.

$$S = 0.1 \times (0.0167 + 0.0292 + 0.2619) + 0.9 \times (0.0290 + 0.0083) = 0.0643$$

**Weight sensitivity.** Because only the ratio $\alpha : \beta$ matters,
re-weighting changes which block wins without any change to the
threshold or feasibility logic. On d1 with a bell circuit: edge-only
weighting ($\alpha=0$, $\beta=1$) selects $\{1, 3\}$, following Nairobi's
lowest-error edge $(1,3) = 0.0068$; qubit-only ($\alpha=1$, $\beta=0$)
and the $1{:}9$ ratio both still select $\{1, 2\}$, the latter because
$1{:}9$ normalises to exactly the $0.1 / 0.9$ default. This is the axis
Phase 5.5 sweeps.

### Answering the sweep from one recorded run (Phase 5.5a)

The sweep
does not re-execute anything. Both the allocator and the router record,
per decision, the $\alpha/\beta$-*free* summands of $S$ — $\sum_{q}
\varepsilon_q$ and $\sum_{(u,v) \in E(B)} \varepsilon_{uv}$ per candidate
— in the event log (the `allocate` event's per-block `scores` for the
allocator, the `route` event's per-device `scores` for the router; see
[`EVENT_LOG.md`](EVENT_LOG.md)). Because $S(\alpha') = \alpha' \sum_q
\varepsilon_q + (1-\alpha') \sum_{uv} \varepsilon_{uv}$ is linear in the
summands, any $\alpha'$'s decision is recomputed by re-weighting the
recorded sums and taking the $\arg\min$ afresh — no allocator re-run.
This is exactly the `Sweepable` contract: the summands are the raw terms,
$S$ is the per-candidate score, and the $\arg\min$ is the rank.

Two scope notes. The allocator's candidate set is **pool-dependent** — a
recorded decision is re-weightable among the blocks that were free at
that placement — so an allocator sweep answers "given the same free-pool
state, would a different $\alpha/\beta$ have chosen a different block".
And the shared-scope $\alpha/\beta$ (one key pair feeds both the router
yardstick and each device's allocator) means a sweep must be explicit
about **which consumer** it re-weights: the router's device choice, or a
device's block choice. They are the same weights over two different
decisions.

### Sweeping an n-term weight group over the simplex (Phase 5.5c)

The $\alpha/\beta$ sweep above is the two-term case of a general
construction. A scoring component's swept weight group has $n$ terms
(here $n=2$: qubit and edge). Because the score is a linear combination
compared by $\arg\min$, its ranking is **scale-invariant** — multiplying
all weights by a positive constant changes nothing — so only the
*direction* of the weight vector matters, and the faithful search space is
the normalised simplex (the weights that sum to 1), whether or not a
component stores its weights normalised. This holds for $n=2$ (the
$(\alpha, 1-\alpha)$ line) and for every larger $n$.

DevQ enumerates that simplex as the **Scheffé $\{n, m\}$ simplex-lattice**
(`[Scheffe-Mixtures]`, see [`REFERENCES.md`](REFERENCES.md)): every
normalised weight $n$-tuple whose entries are multiples of $1/m$. The
resolution $m$ is the `coarse_m` argument; the point count is
$\binom{m+n-1}{n-1}$. At $n=2$ this lattice *is* the historical
$(\alpha, 1-\alpha)$ grid, so the two-term sweep is unchanged.

The winner a weight point induces is **piecewise-constant**: it is constant
within cells of the simplex and jumps across straight tie-loci (where two
candidates' scores cross). The sweep therefore *enumerates* the lattice
rather than descending it — there is no useful gradient. To localise where
a winner flips, it walks the lattice's **edge graph** — pairs of points
differing by moving one $1/m$ unit between two coordinates — not
list-consecutive points. On an edge the segment is a 1-D interval a single
tie-locus crosses once, so bisection along it localises the flip exactly;
along an arbitrary interior chord it would not (multiple crossings, no
single flip), which is why detection is edge-based. At $n=2$ the edge graph
is exactly the consecutive chain, so this reduces to the historical
interval bisection.

**What the sweep faithfully covers.** Replaying decisions from one recorded
run is exact only up to the first decision that reads state a prior decision
mutated — a load-aware router or a pool-depleting allocator couples its
decisions through evolving state, so past the first flip the recorded terms
describe a trajectory that no longer occurs. DevQ treats every component
uniformly under this bound: the sweep is a **first-flip sensitivity** — it
answers "how far can these weights move before the decision first changes,
and where", exactly, and does not claim to reproduce the whole downstream
trajectory at arbitrary weights. Recovering behaviour past the first flip
requires real re-execution at each weight point, which is a separate,
heavier capability (a metric sweep), not this replay.

**Three sweepable axes (Phase 5.6).** The router and allocator sweep the
shared qubit/edge weight pair, whose keys are fixed and known. A scored
*scheduler* (e.g. the NAQJS baseline) is the third axis, and it differs in
two ways the sweep handles generically. First, its weight keys are
plugin-specific (NAQJS's `naqjs.width_weight`/`naqjs.shots_weight`/`naqjs.seq_weight`), so they are not
hardcoded — the scheduler axis leaves its weight group unset and derives the
swept keys from the reconstructed component's `live_params()`, the contract's
own declaration of the weights it scores with, so no plugin key names enter
core. Second, a batch scheduler emits one `schedule` event per dispatched job
in a cycle, all sharing one ranking snapshot; those collapse to a single
sweep decision whose winner is the ranking's argmin, so a cycle's ranking is
one decision the same way a router's single choice is. A research baseline is
not globally registered, so the sweep is passed the same class map the run
registered and rebuilds the component from it to replay — without it, the
sweep refuses honestly rather than resolving the plugin name to nothing.

**Plugin scheduler config keys (Phase 5.6).** A scheduler with knobs of its
own declares them in `CONFIG_SCHEMA` under dotted `<prefix>.<key>` names
(`naqjs.width_weight`, `naqjs.shots_weight`, `naqjs.seq_weight`, `naqjs.eta`,
`naqjs.default_shots`) — the registry rejects un-namespaced plugin keys, and
the dot keeps qconfig readable and the plugin boundary visible in published
artifacts. These keys cascade and validate exactly like core keys. `dq.build`
feeds them into the scheduler by reading the class's `CONFIG_SCHEMA`,
stripping the namespace prefix to recover the ctor parameter name
(`naqjs.eta` → `eta`), and passing the resolved values as kwargs — generic
across any scheduler plugin, so a new baseline needs no core edit. Declaring
a schema key does *not* oblige the ctor to accept it: a component may consume
a key at runtime instead of by injection, so `dq.build` passes only the
subset of schema keys the ctor actually names as parameters (checked with
`inspect.signature`). The three weight keys are what the sweep varies; `eta`
and `default_shots` are fixed scoring inputs and are deliberately kept OUT of
`live_params()` so the sweep does not try to vary them.

**Shots as a ranking feature.** NAQJS's shots term is resolved per job as: the
job's own `shots` if set; else the plugin-level `naqjs.default_shots` if the
researcher set one; else a neutral constant. The neutral constant makes every
unspecified-shots job tie on the shots axis after min-max — the correct
behaviour, since a job that does not distinguish itself on shots should not be
ordered by shots. This is what lets NAQJS rank a workload whose jobs omit
shots (e.g. QASMBench) instead of feeding `None` into the normalisation. The
resolver deliberately does NOT read the device-resolved `shots` config: that
value lives on `DeviceContext.config`, a layer the scheduler does not hold,
and the kernel already resolves job-vs-device shots at dispatch. Ranking is a
queue-relative ordering, so a per-plugin assumed value (or a tie) is the
faithful, layer-clean choice.