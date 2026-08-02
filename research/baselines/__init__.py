'''
Tags: Main

research.baselines — published scheduling/allocation/routing policies from
the literature, ported to DevQ as plugins for benchmarking against the
built-in defaults. Each baseline builds entirely through the documented
plugin API (register_*, the Base* contracts, namespaced config keys) with
no edits to DevQ core; a baseline that cannot be expressed that way is a
finding about the plugin API, recorded, not worked around.

Like every package __init__ this is tagged Main; the baseline modules
themselves are tagged Research (they use DevQ but are not part of it, and
their results depend on the pinned calibration snapshot).
'''