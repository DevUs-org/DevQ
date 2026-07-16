'''
Tags: Alt

RoundRobinRouter — Load-oblivious, noise-oblivious baseline router.

Cycles through the attached devices in index order, routing each job
to the next feasible device after the last one used. Useful as a
debugging tool, a fairness baseline, and a qbench comparison point
against the NoiseRouter — exactly the role Static/FCFS play at the
allocator/scheduler layers.

The feasibility pipeline (device constraints + per-device feasible())
is inherited from BaseRouter, so a round-robin turn never lands a job
on a device where it could never run — the rotation simply skips
infeasible devices for that job.
'''

from kernel.router.base_router import BaseRouter


class RoundRobinRouter(BaseRouter):

    def __init__(self):
        self._last = -1   # index of the last device routed to

    def select(self, qcb, candidates):
        # Candidates arrive filtered to feasible devices, in index
        # order. Pick the first candidate strictly after the last
        # routed index, wrapping around.
        for ctx in candidates:
            if ctx.index > self._last:
                self._last = ctx.index
                return ctx

        # Wrap around
        ctx = candidates[0]
        self._last = ctx.index
        return ctx