'''
Tags: Plugin

test_qasm3_frontend — standalone sanity harness for the opt-in OpenQASM 3
frontend (plugins/frontends/qasm3/). Same run_tests-style shape as
test_mapomatic (print-per-check, non-zero exit on any failure) and
self-contained: it does not import run_tests, and run_tests never imports
it.

What it proves:
  - IMPORT DISCIPLINE (docs/EXTENDING.md): reaches into DevQ only through
    plugin_bases (contract from base_frontend, CircuitRep from common), no
    core-package import, and — unlike the Qiskit frontend — NO qiskit
    import at all, since it walks the reference AST rather than a
    QuantumCircuit. openqasm3 is imported lazily, so the module imports and
    the frontend constructs with openqasm3 BLOCKED.
  - LOWERING CORRECTNESS: declarations, standard gates, angle expressions
    (pi/2, -pi/4), single and whole-register measure, reset in source
    order, classical feedback `if (c==N) {...}` as conditional ops.
  - DECLINED CONSTRUCTS: a custom gate and an else branch are declined, not
    silently mislowered.
  - CROSS-FRONTEND AGREEMENT: agrees with the built-in qasm2 frontend on an
    equivalent Bell circuit.

Run: python -m plugins.frontends.qasm3.test_qasm3_frontend
SKIPS the correctness cases (exit 0, printed notice) when the openqasm3
parser is not installed.
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


def _have_parser():
    try:
        from openqasm3.parser import parse  # noqa: F401
        return True
    except ImportError:
        return False


_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
_FORBIDDEN_CORE = ("kernel", "hardware", "circuits", "registry",
                   "benchmark", "engine", "frontend")


# ── import discipline ───────────────────────────────────────────────────

def test_no_core_or_qiskit_imports():
    '''No core-package import and NO qiskit import anywhere — this frontend
    is qiskit-free by design. CircuitRep comes from plugin_bases.common.
    All checked structurally (AST), so docstring prose does not false-trip.'''
    core_offenders = []
    qiskit_offenders = []
    circuits_import = []
    from_plugin_bases = False
    for fn in ("qasm3_frontend.py", "__init__.py"):
        path = os.path.join(_PLUGIN_DIR, fn)
        with open(path) as handle:
            tree = _pyast.parse(handle.read(), filename=fn)
        for node in _pyast.walk(tree):
            if isinstance(node, _pyast.ImportFrom):
                mod = node.module or ""
                root = mod.split(".")[0]
                if root in _FORBIDDEN_CORE:
                    core_offenders.append(f"{fn}: {mod}")
                if root == "qiskit":
                    qiskit_offenders.append(f"{fn}: {mod}")
                if root == "circuits":
                    circuits_import.append(mod)
                if mod == "plugin_bases.common" and \
                        any(a.name == "CircuitRep" for a in node.names):
                    from_plugin_bases = True
            elif isinstance(node, _pyast.Import):
                for a in node.names:
                    root = a.name.split(".")[0]
                    if root in _FORBIDDEN_CORE:
                        core_offenders.append(f"{fn}: {a.name}")
                    if root == "qiskit":
                        qiskit_offenders.append(f"{fn}: {a.name}")
    check(not core_offenders,
          f"no direct core-package import (offenders: {core_offenders})")
    check(not qiskit_offenders,
          f"no qiskit import anywhere — frontend is qiskit-free "
          f"(offenders: {qiskit_offenders})")
    check(not circuits_import,
          f"no direct circuits.* import (found: {circuits_import})")
    check(from_plugin_bases, "CircuitRep comes from plugin_bases.common")


def test_lazy_openqasm3_import():
    '''No openqasm3 import at module scope — every one inside a function.'''
    path = os.path.join(_PLUGIN_DIR, "qasm3_frontend.py")
    with open(path) as handle:
        tree = _pyast.parse(handle.read(), filename="qasm3_frontend.py")
    offenders = []
    for node in tree.body:   # module scope only
        if isinstance(node, (_pyast.Import, _pyast.ImportFrom)):
            mod = (node.module if isinstance(node, _pyast.ImportFrom)
                   else ",".join(a.name for a in node.names)) or ""
            if mod.split(".")[0] == "openqasm3":
                offenders.append(mod)
    check(not offenders,
          f"no module-scope openqasm3 import (offenders: {offenders})")


def test_imports_with_parser_blocked():
    '''The frontend imports and constructs with openqasm3 BLOCKED.'''
    import builtins
    real_import = builtins.__import__

    def guard(name, *a, **k):
        if name == "openqasm3" or name.startswith("openqasm3."):
            raise ImportError("openqasm3 BLOCKED for containment test")
        return real_import(name, *a, **k)

    saved = {m: sys.modules[m] for m in list(sys.modules)
             if m == "openqasm3" or m.startswith("openqasm3.")}
    for m in saved:
        del sys.modules[m]
    builtins.__import__ = guard
    try:
        for m in list(sys.modules):
            if m.startswith("plugins.frontends.qasm3"):
                del sys.modules[m]
        from plugins.frontends.qasm3.qasm3_frontend import QASM3Frontend
        QASM3Frontend()
        ok = True
    except ImportError:
        ok = False
    finally:
        builtins.__import__ = real_import
        sys.modules.update(saved)
    check(ok, "frontend imports and constructs with openqasm3 blocked")


# ── correctness ─────────────────────────────────────────────────────────

def _write(path, text):
    with open(path, "w") as handle:
        handle.write(text)


_BELL = (
    "OPENQASM 3.0;\n"
    'include "stdgates.inc";\n'
    "qubit[2] q;\nbit[2] c;\n"
    "h q[0];\ncx q[0], q[1];\n"
    "c[0] = measure q[0];\nc[1] = measure q[1];\n"
)


def test_bell(tmpdir):
    from plugins.frontends.qasm3.qasm3_frontend import QASM3Frontend
    src = os.path.join(tmpdir, "bell.qasm3")
    _write(src, _BELL)
    cr = QASM3Frontend().parse(src)
    check(cr.num_qubits == 2 and cr.num_clbits == 2, "bell widths (2, 2)")
    check(cr.cregs == {"c": (0, 2)}, "bell creg (base 0, size 2)")
    ops = [(i["op"], i.get("gate")) for i in cr.instructions]
    check(ops == [("gate", "h"), ("gate", "cx"),
                  ("measure", None), ("measure", None)],
          "bell op sequence")


def test_angles_and_whole_register_measure(tmpdir):
    import math
    from plugins.frontends.qasm3.qasm3_frontend import QASM3Frontend
    src = os.path.join(tmpdir, "ang.qasm3")
    _write(src, (
        "OPENQASM 3.0;\n"
        'include "stdgates.inc";\n'
        "qubit[2] q;\nbit[2] c;\n"
        "rz(pi/2) q[0];\n"
        "rx(-pi/4) q[1];\n"
        "reset q[0];\n"
        "c = measure q;\n"
    ))
    cr = QASM3Frontend().parse(src)
    rz = cr.instructions[0]
    rx = cr.instructions[1]
    check(abs(rz["params"][0] - math.pi / 2) < 1e-12, "rz(pi/2) evaluated")
    check(abs(rx["params"][0] - (-math.pi / 4)) < 1e-12,
          "rx(-pi/4) evaluated (unary minus + division)")
    check(cr.instructions[2]["op"] == "reset", "reset kept in source order")
    measures = [i for i in cr.instructions if i["op"] == "measure"]
    check([(m["qubit"], m["clbit"]) for m in measures] == [(0, 0), (1, 1)],
          "whole-register `c = measure q` expands to (0->0, 1->1)")


def test_feedback(tmpdir):
    from plugins.frontends.qasm3.qasm3_frontend import QASM3Frontend
    src = os.path.join(tmpdir, "fb.qasm3")
    _write(src, (
        "OPENQASM 3.0;\n"
        'include "stdgates.inc";\n'
        "qubit[2] q;\nbit[2] c;\n"
        "h q[0];\nc[0] = measure q[0];\n"
        "if (c == 1) { x q[1]; }\n"
    ))
    cr = QASM3Frontend().parse(src)
    check(cr.is_dynamic, "feedback circuit is dynamic")
    conds = cr.conditionals
    check(len(conds) == 1, "one conditional op")
    c0 = conds[0]
    check(c0["condition"]["clbits"] == [0, 1]
          and c0["condition"]["value"] == 1,
          "guard clbits [0,1] LSB-first, value 1")
    check(c0["body"]["gate"] == "x" and c0["body"]["qubits"] == [1],
          "guarded body x on qubit 1")


def test_custom_gate_declined(tmpdir):
    from plugins.frontends.qasm3.qasm3_frontend import QASM3Frontend
    src = os.path.join(tmpdir, "cust.qasm3")
    _write(src, (
        "OPENQASM 3.0;\n"
        "gate mygate a { x a; }\n"
        "qubit[1] q;\n"
        "mygate q[0];\n"
    ))
    declined = False
    try:
        QASM3Frontend().parse(src)
    except ValueError:
        declined = True
    check(declined, "custom gate definition is declined")


def test_else_branch_declined(tmpdir):
    from plugins.frontends.qasm3.qasm3_frontend import QASM3Frontend
    src = os.path.join(tmpdir, "else.qasm3")
    _write(src, (
        "OPENQASM 3.0;\n"
        'include "stdgates.inc";\n'
        "qubit[2] q;\nbit[2] c;\n"
        "c[0] = measure q[0];\n"
        "if (c == 1) { x q[1]; } else { z q[1]; }\n"
    ))
    declined = False
    try:
        QASM3Frontend().parse(src)
    except ValueError:
        declined = True
    check(declined, "if/else is declined (no CircuitRep representation)")


def test_agreement_with_qasm2(tmpdir):
    from plugins.frontends.qasm3.qasm3_frontend import QASM3Frontend
    from frontend.qasm2_frontend import QASM2Frontend
    q3 = os.path.join(tmpdir, "b.qasm3")
    _write(q3, _BELL)
    q2 = os.path.join(tmpdir, "b.qasm")
    _write(q2, (
        'OPENQASM 2.0;\ninclude "qelib1.inc";\n'
        "qreg q[2];\ncreg c[2];\n"
        "h q[0];\ncx q[0],q[1];\n"
        "measure q[0] -> c[0];\nmeasure q[1] -> c[1];\n"
    ))
    a = QASM3Frontend().parse(q3)
    b = QASM2Frontend().parse(q2)
    same = (a.num_qubits == b.num_qubits and a.num_clbits == b.num_clbits
            and a.cregs == b.cregs and a.instructions == b.instructions)
    check(same, "qasm3 and qasm2 produce identical CircuitRep for Bell")


def main():
    print("OpenQASM 3 frontend tests")
    test_no_core_or_qiskit_imports()
    test_lazy_openqasm3_import()
    test_imports_with_parser_blocked()

    if not _have_parser():
        print("\n  SKIP  correctness tests (openqasm3[parser] not installed)")
    else:
        with tempfile.TemporaryDirectory() as tmpdir:
            test_bell(tmpdir)
            test_angles_and_whole_register_measure(tmpdir)
            test_feedback(tmpdir)
            test_custom_gate_declined(tmpdir)
            test_else_branch_declined(tmpdir)
            test_agreement_with_qasm2(tmpdir)

    print()
    if _failures:
        print(f"{len(_failures)} failure(s).")
        sys.exit(1)
    print("All OpenQASM 3 frontend tests passed.")


if __name__ == "__main__":
    main()
