'''
Tags: Main

common — the single plugin-facing slice of DevQ core.

This module is the ONE place where the plugin layer reaches into core.
Every core type an extension (or a base) may legitimately need is imported
here and re-exported, so that:

  - extensions import their contract from their base (BaseProvider,
    BaseRouter, …) and everything else from here — never from core;
  - the bases themselves import what they need from here, so no base file
    touches a core path either;
  - a maintainer exposing a new core type to extension authors adds one
    line HERE and nowhere else, without deciding which base it "belongs"
    to.

Anything a component might want from core goes through this file. If a
name a component needs is missing, add it here rather than importing
across the seam — that keeps core paths confined to this one module.

DEPENDENCY DIRECTION. This module imports FROM core and is imported BY the
bases and extensions; core must never import this module at load time. The
few core modules that reference the plugin layer (registry, engine,
benchmark) do so with deferred, function-local imports, so the package
graph is acyclic at import time. Keep it that way: do not promote those
core-side imports to module scope, and do not import a base from here.

Sweepable is deliberately NOT re-exported here: it lives in
plugin_bases.sweepable, is inherited by the scoring bases, and is never
imported by a component directly.
'''

# ── Provider-facing ───────────────────────────────────────────────────────────
from hardware.device import QuantumDevice
from circuits.execution_result import (
    ExecutionResult,
    ExecutionFuture,
    submit_async,
)

# ── Frontend-facing ───────────────────────────────────────────────────────────
from circuits.circuit_rep import CircuitRep

# ── Scheduler-facing ──────────────────────────────────────────────────────────
from kernel.process.lifecycle import JobStates

# ── Router-facing ─────────────────────────────────────────────────────────────
from kernel.memory.qubit_pool import QubitPool

# ── Allocator-facing ──────────────────────────────────────────────────────────
from kernel.memory.allocator.filtering import (
    eligible_qubits,
    edge_allowed,
    has_connected_block,
)

# ── Config (cross-cutting: routers and schedulers declare tunables) ───────────
from registry.keyspec import KeySpec, non_negative, unit_interval


__all__ = [
    "QuantumDevice",
    "ExecutionResult",
    "ExecutionFuture",
    "submit_async",
    "CircuitRep",
    "JobStates",
    "QubitPool",
    "eligible_qubits",
    "edge_allowed",
    "has_connected_block",
    "KeySpec",
    "non_negative",
    "unit_interval",
]
