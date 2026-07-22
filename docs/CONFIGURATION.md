# DevQ Configuration Reference

The four-level configuration cascade, the three key scopes, seeding and
reproducibility, and the components that ship with DevQ.

Kept out of the README so that the README stays an overview rather than a
specification. Configuration is resolved by `config/config_loader.py`;
`qconfig` in the shell reports every active value with its provenance.

Related: [`REGISTRY.md`](REGISTRY.md) for declaring your own config keys,
[`COST_MODEL.md`](COST_MODEL.md) for what the weight keys mean
mathematically, [`SHELL.md`](SHELL.md) for the `qconfig` command itself.

---

## Configuration

Configuration keys are split into three scopes.

**Device keys** (`scheduler`, `allocator`, `shots`) are resolved
independently for every attached device through a four-level cascade,
later levels overriding earlier ones:

1. **DevQ core defaults**
2. **That device's provider `preferred_config()`** (e.g. IBM prefers `shots: 2048`)
3. **Global user config file** — `DevQ(config_path=...)`, applies to all devices
4. **Per-device user config file** — `add_device(device, config_path)`, this device only

**Global keys** (`router`, `router_queue_weight`, `router_noise_weight`)
are resolved once for the whole system: core defaults ← global user file.
Providers deliberately **cannot** set global keys — a provider expressing
system routing policy would be a layer violation; such keys are warned
about and ignored. Like the common keys below, the router weight pair
accepts any non-negative numbers and is normalised to sum to 1 at
resolution time (scaling both weights never changes which device wins).

**Common keys** (`qubit_error_weight`, `edge_error_weight`) are the α / β
of the noise cost `S = α·Σ(qubit_error) + β·Σ(edge_error)` — one concept
with two consumers, so the pair is resolved in **both** scopes. The
global-scope copy feeds the NoiseRouter's scoring yardstick (one uniform
ruler across all candidate devices); each device's copy rides the full
four-level device cascade and feeds that device's allocator. Setting the
weights once in the global file therefore moves the yardstick and every
device's allocator together; a per-device file overrides only that
device's allocator copy. The keys accept any non-negative numbers and
each resolved pair is **normalised to sum to 1** — only the ratio
matters, so `1`/`9`, `0.1`/`0.9`, and `2`/`18` are all equivalent, and
normalising keeps S values on one comparable scale everywhere. Keys
cascade independently, *then* the pair is normalised (overriding only
one of the two mixes it with the other's inherited value). A both-zero
pair falls back to core defaults with a warning. `qconfig` shows the
effective normalised values in both the global section and each device
section. Allocators without a cost model (Static, Graph) ignore the
weights — the same precedent as Static ignoring edge thresholds.

**Plugin keys.** The three scopes above cover DevQ's own keys. A
registered component may declare its own, which must be namespaced
(`qos.batch_window`) and then behave exactly like core keys: they
cascade, validate, carry provenance, and appear in `qconfig`. A plugin
key is legal only once its owner is registered — before that it is an
unknown key like any other. Plugins may also declare their own
normalisation groups, N-ary rather than pairs. See
[`docs/REGISTRY.md`](docs/REGISTRY.md).

The exact scoring formulas — the block cost `S`, the router's device
score, and the normalisation rules — are stated formally, with worked
values, in [`docs/COST_MODEL.md`](docs/COST_MODEL.md).

One JSON file may freely mix both scopes; each loader reads only its own
keys:

```json
{
    "scheduler": "packing",
    "allocator": "noise_graph",
    "shots": 1024,
    "qubit_error_weight": 0.1,
    "edge_error_weight": 0.9,
    "router": "noise",
    "router_queue_weight": 0.5,
    "router_noise_weight": 0.5
}
```

`qconfig` shows the provenance of every active value: `DevQ Core`,
`<ProviderName>`, `User (global)`, or `User (dN)`.

Ready-made example config files — including the ones used by the sanity
test blocks (`router_only`, `round_robin`, per-device overrides, weight
variants) — live in `config/config_examples/`.

### Reproducibility & Seeding

Providers accept an optional `seed` at construction for reproducible runs.
It is a **constructor parameter, not a config key** — providers are built
before configuration is resolved and never consume the user config, so a
config key would be a layer violation.

```python
DevQSimulatedProvider(seed=42).get_device("random", 7)   # reproducible topology
IBMSimulatedProvider(seed=42)                            # reproducible counts
```

`seed=None` (the default) preserves fully unseeded behaviour, byte-identical
to a build without the parameter.

The example `example.py` wires this to a `--seed` flag, so a whole session can
be made reproducible without editing code:

```bash
python example.py              # unseeded (default)
python example.py --seed 42    # identical devices and counts every launch
```

Two launches with the same seed produce byte-identical transcripts —
d0's generated topology and error maps, and every job's counts.

**What each provider does with it.** `DevQSimulatedProvider` holds a
provider-local `random.Random(seed)` used for topology and error map
generation, so a generated device is identical across launches. The global
`random` state is never touched — seeding DevQ will not perturb randomness
elsewhere in your program. `IBMSimulatedProvider` derives a per-run seed
`seed + k`, where `k` is a provider-local submission counter incremented on
the shell thread, and passes it to both the transpiler and the Aer
simulator. Because all job dispatch happens on the shell thread, submission
order is deterministic: an identical session replays identical counts
job-for-job, while two runs of the *same* circuit within one session still
produce distinct counts rather than cloned ones.

Providers with no stochastic behaviour inherit `seed` from `BaseProvider`
and ignore it — the same precedent as `StaticAllocator` ignoring cost
weights.

**Scope.** Seeding makes DevQ's *own* randomness reproducible. It does not
pin your dependency versions: fake-backend calibration data is tied to the
`qiskit-ibm-runtime` release, so reproducing counts across machines also
requires matching the pinned stack in `requirements.txt`.

### Scheduler, Allocator & Router Reference

| Config key | Class | Tag | Scope | Behaviour |
|---|---|---|---|---|
| `fcfs` | `FCFSScheduler` | Alt | device | Strict submission order; head-of-line blocking on WAITING jobs (REJECTED jobs are removed, never block) |
| `sdf` | `ShortestDepthScheduler` | Alt | device | Shallowest circuit first; better throughput under mixed-depth workloads |
| `packing` | `PackingScheduler` | **default** | device | SDF + greedy bin-packing (TempPool, two-phase commit); concurrent circuits on disjoint qubits |
| `static` | `StaticAllocator` | Alt | device | First available block; no topology/noise awareness; ignores edge thresholds by design |
| `graph` | `GraphAllocator` | Alt | device | BFS over topology graph; guarantees connected subgraph |
| `noise_graph` | `NoiseGraphAllocator` | **default** | device | BFS + weighted cost S = α·Σ(qubit_err) + β·Σ(edge_err); α/β via the common-scope `qubit_error_weight`/`edge_error_weight` (defaults 0.1/0.9, normalised to sum 1) |
| `round_robin` | `RoundRobinRouter` | Alt | global | Cycles through feasible devices in index order; load/noise-oblivious baseline |
| `noise` | `NoiseRouter` | **default** | global | Routes to lowest `w_q·queue_pressure + w_n·best_case_cost` over feasible devices; weights via `router_queue_weight` / `router_noise_weight` (any non-negative numbers, normalised to sum 1, default 0.5 each) |

These are the components DevQ ships with, not a closed list: any
registered component is equally addressable by its config key. See
[`docs/REGISTRY.md`](docs/REGISTRY.md).

Because per-device FCFS queues sit below the router, FCFS ordering is
per-device: global submission order is approximately preserved via routing
order — the standard two-level-scheduling tradeoff.

---