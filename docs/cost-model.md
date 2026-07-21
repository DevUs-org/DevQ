# DevQ Cost Model & Routing Formulas

Formal statement of DevQ's scoring mathematics, kept out of the README so
that the README stays a usage document. This file is the canonical
reference for the formulas and the source for the corresponding sections
of any write-up.

Implemented in `kernel/memory/allocators/noise_graph_allocator.py`
(block selection) and `kernel/router/noise_router.py` (device
selection). Weight resolution and normalisation live in
`config/config_loader.py`. The values below are what `qconfig` reports
and what `test_blocks.txt` asserts against.

**Scope.** Phase 5.1. Phase 5.3 will extend this file with the metrics
layer (fidelity against closed-form ideals, throughput, queue latency,
utilisation, rejection rate, load balance) using the same notation.

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
mappings asserted in `test_blocks.txt` Blocks 2 and 4.

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