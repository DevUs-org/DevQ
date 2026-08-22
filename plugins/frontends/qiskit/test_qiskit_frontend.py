'''
Tags: Plugin

test_qiskit_frontend — standalone sanity harness for the opt-in Qiskit
frontend (plugins/frontends/qiskit/). Same run_tests-style shape as
test_mapomatic (print-per-check, non-zero exit on any failure) and
deliberately self-contained: it does not import run_tests, and run_tests
never imports it — a plugin owns its own tests, decoupled from the core
suite.

What it proves:
  - IMPORT DISCIPLINE (docs/EXTENDING.md): the plugin reaches into DevQ
    only through plugin_bases — no kernel/circuits/hardware/registry
    import — and every qiskit import is lazy, so the modules import and the
    frontend constructs with qiskit BLOCKED.
  - LOWERING CORRECTNESS: a QuantumCircuit lowers to the CircuitRep the
    rest of DevQ expects — right gate names, flattened qubit/clbit indices,
    measures/resets in source order, classical feedback as conditional ops.
  - CROSS-FRONTEND AGREEMENT: the Qiskit and built-in qasm2 frontends
    produce a byte-identical CircuitRep for the same Bell circuit — the
    property that makes the source language invisible to the rest of the
    stack.
  - DECLINED CONSTRUCTS: an unbound Parameter and a source exposing no
    QuantumCircuit are declined, not silently mislowered.

Run: python -m plugins.frontends.qiskit.test_qiskit_frontend
SKIPS the correctness cases (exit 0, printed notice) when qiskit is not
installed, so it never makes qiskit a hard dependency.
'''

import ast as _pyast
import os
import sys
import tempfile

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__),
                                     "..", "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_failures = []


def check(ok, description):
    print(("  PASS  " if ok else "  FAIL  ") + description)
    if not ok:
        _failures.append(description)
    return ok


def _have_qiskit():
    try:
        import qiskit  # noqa: F401
        return True
    except ImportError:
        return False


# ── import discipline (provable without qiskit) ─────────────────────────

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
_FORBIDDEN_CORE = ("kernel", "hardware", "circuits", "registry",
                   "benchmark", "engine", "frontend")


def test_no_core_imports():
    '''The plugin imports from plugin_bases and its own siblings only —
    never a core package directly (the seam rule from docs/EXTENDING.md).'''
    offenders = []
    for fn in ("qiskit_lowering.py", "qiskit_frontend.py", "__init__.py"):
        path = os.path.join(_PLUGIN_DIR, fn)
        with open(path) as handle:
            tree = _pyast.parse(handle.read(), filename=fn)
        for node in _pyast.walk(tree):
            if isinstance(node, (_pyast.Import, _pyast.ImportFrom)):
                mod = (node.module if isinstance(node, _pyast.ImportFrom)
                       else ",".join(a.name for a in node.names)) or ""
                if mod.split(".")[0] in _FORBIDDEN_CORE:
                    offenders.append(f"{fn}: {mod}")
    check(not offenders,
          f"no direct core-package import in the plugin "
          f"(offenders: {offenders})")


def test_circuitrep_from_plugin_bases():
    '''CircuitRep is imported from plugin_bases.common, not circuits.*.
    Checked structurally (AST), so prose mentioning "circuits" in a
    docstring does not false-trip the rule.'''
    path = os.path.join(_PLUGIN_DIR, "qiskit_lowering.py")
    with open(path) as handle:
        src = handle.read()
        tree = _pyast.parse(src, filename="qiskit_lowering.py")
    from_plugin_bases = False
    circuits_import = []
    for node in _pyast.walk(tree):
        if isinstance(node, _pyast.ImportFrom):
            if node.module == "plugin_bases.common" and \
                    any(a.name == "CircuitRep" for a in node.names):
                from_plugin_bases = True
            if (node.module or "").split(".")[0] == "circuits":
                circuits_import.append(node.module)
        elif isinstance(node, _pyast.Import):
            for a in node.names:
                if a.name.split(".")[0] == "circuits":
                    circuits_import.append(a.name)
    check(from_plugin_bases, "CircuitRep comes from plugin_bases.common")
    check(not circuits_import,
          f"no direct circuits.* import (found: {circuits_import})")


def test_lazy_qiskit_imports():
    '''No qiskit import at module scope — every one inside a function.'''
    offenders = []
    for fn in ("qiskit_lowering.py", "qiskit_frontend.py"):
        path = os.path.join(_PLUGIN_DIR, fn)
        with open(path) as handle:
            tree = _pyast.parse(handle.read(), filename=fn)
        for node in tree.body:   # module scope only
            if isinstance(node, (_pyast.Import, _pyast.ImportFrom)):
                mod = (node.module if isinstance(node, _pyast.ImportFrom)
                       else ",".join(a.name for a in node.names)) or ""
                if mod.split(".")[0] == "qiskit":
                    offenders.append(f"{fn}: {mod}")
    check(not offenders,
          f"no module-scope qiskit import (offenders: {offenders})")


def test_imports_with_qiskit_blocked():
    '''The plugin imports and constructs with qiskit BLOCKED — the
    lazy-import guarantee, enforced by poisoning the import machinery.'''
    import builtins
    real_import = builtins.__import__

    def guard(name, *a, **k):
        if name == "qiskit" or name.startswith("qiskit."):
            raise ImportError("qiskit BLOCKED for containment test")
        return real_import(name, *a, **k)

    saved = {m: sys.modules[m] for m in list(sys.modules)
             if m == "qiskit" or m.startswith("qiskit.")}
    for m in saved:
        del sys.modules[m]
    builtins.__import__ = guard
    try:
        for m in list(sys.modules):
            if m.startswith("plugins.frontends.qiskit"):
                del sys.modules[m]
        from plugins.frontends.qiskit.qiskit_frontend import QiskitFrontend
        QiskitFrontend()
        ok = True
    except ImportError:
        ok = False
    finally:
        builtins.__import__ = real_import
        sys.modules.update(saved)
    check(ok, "plugin imports and constructs with qiskit blocked")


# ── correctness (needs qiskit) ──────────────────────────────────────────

def _write(path, text):
    with open(path, "w") as handle:
        handle.write(text)


def test_bell(tmpdir):
    from plugins.frontends.qiskit.qiskit_frontend import QiskitFrontend
    src = os.path.join(tmpdir, "bell.py")
    _write(src, (
        "from qiskit import QuantumCircuit\n"
        "circuit = QuantumCircuit(2, 2)\n"
        "circuit.h(0)\ncircuit.cx(0, 1)\n"
        "circuit.measure(0, 0)\ncircuit.measure(1, 1)\n"
    ))
    cr = QiskitFrontend().parse(src)
    check(cr.num_qubits == 2 and cr.num_clbits == 2, "bell widths (2, 2)")
    check(cr.cregs == {"c": (0, 2)}, "bell creg recorded as (base 0, size 2)")
    ops = [(i["op"], i.get("gate")) for i in cr.instructions]
    check(ops == [("gate", "h"), ("gate", "cx"),
                  ("measure", None), ("measure", None)],
          "bell op sequence: h, cx, measure, measure")


def test_reset_order(tmpdir):
    from plugins.frontends.qiskit.qiskit_frontend import QiskitFrontend
    src = os.path.join(tmpdir, "reset.py")
    _write(src, (
        "from qiskit import QuantumCircuit\n"
        "def build():\n"
        "    qc = QuantumCircuit(1, 1)\n"
        "    qc.h(0)\n    qc.reset(0)\n    qc.measure(0, 0)\n"
        "    return qc\n"
    ))
    cr = QiskitFrontend().parse(src)
    ops = [i["op"] for i in cr.instructions]
    check(ops == ["gate", "reset", "measure"],
          "build() source: reset kept in source order")


def test_feedback(tmpdir):
    from plugins.frontends.qiskit.qiskit_frontend import QiskitFrontend
    src = os.path.join(tmpdir, "fb.py")
    _write(src, (
        "from qiskit import QuantumCircuit\n"
        "from qiskit.circuit import QuantumRegister, ClassicalRegister\n"
        "def build():\n"
        "    q = QuantumRegister(2,'q'); c = ClassicalRegister(2,'c')\n"
        "    qc = QuantumCircuit(q, c)\n"
        "    qc.h(0)\n    qc.measure(0, 0)\n"
        "    with qc.if_test((c, 1)):\n        qc.x(1)\n"
        "    return qc\n"
    ))
    cr = QiskitFrontend().parse(src)
    check(cr.is_dynamic, "feedback circuit is dynamic")
    conds = cr.conditionals
    check(len(conds) == 1, "one conditional op emitted")
    c0 = conds[0]
    check(c0["condition"]["clbits"] == [0, 1]
          and c0["condition"]["value"] == 1,
          "guard is clbits [0,1] LSB-first, value 1")
    check(c0["body"]["gate"] == "x" and c0["body"]["qubits"] == [1],
          "guarded body is x on qubit 1")


def test_unbound_parameter_declined(tmpdir):
    from plugins.frontends.qiskit.qiskit_frontend import QiskitFrontend
    src = os.path.join(tmpdir, "param.py")
    _write(src, (
        "from qiskit import QuantumCircuit\n"
        "from qiskit.circuit import Parameter\n"
        "theta = Parameter('theta')\n"
        "circuit = QuantumCircuit(1, 0)\n"
        "circuit.rz(theta, 0)\n"
    ))
    declined = False
    try:
        QiskitFrontend().parse(src)
    except ValueError:
        declined = True
    check(declined, "unbound Parameter is declined")


def test_no_circuit_declined(tmpdir):
    from plugins.frontends.qiskit.qiskit_frontend import QiskitFrontend
    src = os.path.join(tmpdir, "empty.py")
    _write(src, "x = 1\n")
    declined = False
    try:
        QiskitFrontend().parse(src)
    except ValueError:
        declined = True
    check(declined, "source exposing no QuantumCircuit is declined")


def test_agreement_with_qasm2(tmpdir):
    from plugins.frontends.qiskit.qiskit_frontend import QiskitFrontend
    from frontend.qasm2_frontend import QASM2Frontend
    py = os.path.join(tmpdir, "b.py")
    _write(py, (
        "from qiskit import QuantumCircuit\n"
        "circuit = QuantumCircuit(2, 2)\n"
        "circuit.h(0); circuit.cx(0, 1)\n"
        "circuit.measure(0, 0); circuit.measure(1, 1)\n"
    ))
    qasm = os.path.join(tmpdir, "b.qasm")
    _write(qasm, (
        'OPENQASM 2.0;\ninclude "qelib1.inc";\n'
        "qreg q[2];\ncreg c[2];\n"
        "h q[0];\ncx q[0],q[1];\n"
        "measure q[0] -> c[0];\nmeasure q[1] -> c[1];\n"
    ))
    a = QiskitFrontend().parse(py)
    b = QASM2Frontend().parse(qasm)
    same = (a.num_qubits == b.num_qubits and a.num_clbits == b.num_clbits
            and a.cregs == b.cregs and a.instructions == b.instructions)
    check(same, "qiskit and qasm2 produce identical CircuitRep for Bell")


def main():
    print("Qiskit frontend tests")
    test_no_core_imports()
    test_circuitrep_from_plugin_bases()
    test_lazy_qiskit_imports()
    test_imports_with_qiskit_blocked()

    if not _have_qiskit():
        print("\n  SKIP  correctness tests (qiskit not installed)")
    else:
        with tempfile.TemporaryDirectory() as tmpdir:
            test_bell(tmpdir)
            test_reset_order(tmpdir)
            test_feedback(tmpdir)
            test_unbound_parameter_declined(tmpdir)
            test_no_circuit_declined(tmpdir)
            test_agreement_with_qasm2(tmpdir)

    print()
    if _failures:
        print(f"{len(_failures)} failure(s).")
        sys.exit(1)
    print("All Qiskit frontend tests passed.")


if __name__ == "__main__":
    main()
