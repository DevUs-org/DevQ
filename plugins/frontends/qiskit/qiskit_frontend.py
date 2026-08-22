'''
Tags: Plugin

QiskitFrontend — DevQ frontend for Qiskit circuits given as a .py source.

An opt-in frontend (registered by hand, not shipped in the built-in set).
It reads a Python source file that BUILDS a Qiskit QuantumCircuit and
lowers that circuit into CircuitRep via the one shared walk in
qiskit_lowering.lower_circuit. Register it when you want it:

    from plugins.frontends.qiskit.qiskit_frontend import QiskitFrontend
    devq.register_frontend("qiskit", QiskitFrontend())

so `import devq` never pulls qiskit in for a user who does not use it.

WHY A .py SOURCE. A frontend's contract is parse(source_path) -> CircuitRep,
so the extension-dispatched path needs a FILE. A Qiskit circuit's natural
file form is the Python that constructs it. So this frontend claims ".py":
it executes the source in an isolated module namespace and takes the
QuantumCircuit the source produced. The source names its circuit by a
module-level variable (default `circuit`, or `qc`), or by a `build()` that
returns one — see _extract.

FUTURE NON-FILE SOURCES. The planned REST / live-object path hands a
QuantumCircuit in directly, no file. That path does NOT re-parse — it calls
lower_from_circuit() below, the SAME walk parse() ends in. parse() is a
thin adapter over that shared tail, so the two source shapes cannot
diverge.

IMPORT DISCIPLINE (docs/EXTENDING.md). The contract base comes from
plugin_bases.base_frontend; the lowering is a genuine sibling that ships
alongside this file (its own implementation, not a reach across the seam).
No core path is imported. qiskit is imported lazily, inside the methods
that touch it, so importing this module needs no qiskit.
'''

import importlib.util
import os

from plugin_bases.base_frontend import BaseFrontend
from plugins.frontends.qiskit.qiskit_lowering import (
    lower_circuit,
    QiskitLoweringError,
)


# Module-level variable names this frontend looks for, in order, when a
# .py source does not expose a build() function.
_CIRCUIT_NAMES = ("circuit", "qc")


class QiskitFrontend(BaseFrontend):
    LABEL = "Qiskit circuit (.py)"

    # There is exactly one .py frontend, so extension dispatch resolves it
    # unambiguously; a .py that builds something other than a QuantumCircuit
    # is a load error, not an ambiguity.
    EXTENSIONS = (".py",)

    def parse(self, source):
        '''
        Lower a .py source that builds a Qiskit QuantumCircuit into a
        CircuitRep.

        The source is executed in an isolated module namespace, the circuit
        it produced is extracted (see _extract), and the shared walk lowers
        it.

        Raises:
            FileNotFoundError: if the source path does not exist (callers
                map it to their own message, as for the qasm2 frontend).
            ValueError: on a source that does not import, exposes no
                QuantumCircuit, or holds an operation the lowering cannot
                represent — always naming the source.
        '''
        if not os.path.exists(source):
            raise FileNotFoundError(source)

        try:
            module = self._load_module(source)
        except Exception as e:
            raise ValueError(
                f"{source}: could not import the Qiskit source "
                f"({type(e).__name__}: {e})"
            ) from None

        qc = self._extract(module, source)

        try:
            return lower_circuit(qc)
        except QiskitLoweringError as e:
            raise ValueError(f"{source}: {e}") from None

    # ── the shared lowering tail, reusable by a future object/REST path ──

    @staticmethod
    def lower_from_circuit(qc):
        '''
        Lower an already-in-hand Qiskit QuantumCircuit into a CircuitRep.

        The non-file entry point into the SAME lowering parse() ends in —
        a future REST / live-object source calls this with no path and no
        import machinery, so the two source shapes share one lowering.

        Raises:
            QiskitLoweringError: propagated as-is (the caller with
            provenance wraps it).
        '''
        return lower_circuit(qc)

    # ── file machinery ──────────────────────────────────────────────────

    @staticmethod
    def _load_module(source):
        '''Import a .py file as an isolated, throwaway module and return it
        — a namespace whose only purpose is to run the source and expose
        its top-level names.'''
        spec = importlib.util.spec_from_file_location(
            "devq_qiskit_source", source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    @staticmethod
    def _extract(module, source):
        '''
        Pull the QuantumCircuit out of an executed source module.

        Resolution order: a `build()` callable (call it, use its return
        value), else a module-level `circuit`, then `qc`. The result must
        be a QuantumCircuit; anything else is a load error naming what was
        found. QuantumCircuit is imported lazily HERE so importing this
        frontend needs no qiskit.

        Raises:
            ValueError: naming the source, when no QuantumCircuit is found
            or the found object is the wrong type.
        '''
        from qiskit import QuantumCircuit

        candidate = None
        builder = getattr(module, "build", None)
        if callable(builder):
            candidate = builder()
        else:
            for name in _CIRCUIT_NAMES:
                obj = getattr(module, name, None)
                if obj is not None:
                    candidate = obj
                    break

        if candidate is None:
            raise ValueError(
                f"{source}: no Qiskit circuit found. Expose one as a "
                f"module-level `circuit` (or `qc`), or define a `build()` "
                f"that returns a QuantumCircuit."
            )

        if not isinstance(candidate, QuantumCircuit):
            raise ValueError(
                f"{source}: expected a QuantumCircuit but found a "
                f"{type(candidate).__name__}."
            )

        return candidate
