'''
Tags: Main

research — paper and benchmark tooling that USES DevQ but is not part of it.

This package holds the artifacts behind the paper's results rather than
any DevQ feature. Nothing in DevQ core imports it, and run_tests.py does
not exercise it: it sits outside the system under test on purpose, so a
benchmark sweep whose numbers depend on a pinned calibration snapshot
never masquerades as a correctness test.

  run_qasmbench_small.py   run the vendored QASMBench small-scale circuits
                           through DevQ's fidelity metric and report a
                           per-circuit table plus a session summary
  workloads/               the workload spec(s) those runs consume
  circuits/qasmbench/      the vendored QASMBench circuits, carrying their
                           upstream LICENSE and NOTICE (see PROVENANCE.md)

Run the tooling as a module from the repo root so DevQ's top-level
packages resolve, e.g. `python -m research.run_qasmbench_small`.

Files here are tagged Research to distinguish them from the Main entry
points into DevQ itself — this is tooling built on top of the system, not
part of it.
'''