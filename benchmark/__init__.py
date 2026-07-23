'''
Tags: Main

DevQ benchmark harness — running workloads from data rather than code.

A workload spec (JSON) describes a benchmark run: which devices, which
jobs, which config, which seed. This package turns one into a built,
drained session whose event log records the spec verbatim, so a result
can always be traced back to the exact input that produced it.

  spec.py   parse and validate a spec, build a session from it, submit
            its jobs, drain to completion

Specs reference REGISTERED component names and never import by path —
callers register plugins in Python first. A data file that can trigger
arbitrary imports is a data file that can run arbitrary code.
'''