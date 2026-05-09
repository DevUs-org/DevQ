'''
Tags: Main

ExecutionResult — Structured result returned after circuit execution.
ExecutionFuture — Lightweight future wrapper around an ExecutionResult.

ExecutionFuture is intentionally simple for now — synchronous simulation
means results are always immediately available. Phase 4 (distributed
backends) will replace the internals with real async execution while
keeping the same .done() / .result() interface so the Kernel never
needs to change.
'''


class ExecutionResult:
    def __init__(self, counts, success, error=None):
        '''
        Args:
            counts  : dict — measurement outcome counts e.g. {"00": 512, "11": 512}
            success : bool — whether execution completed successfully
            error   : str | None — error message on failure, None on success
        '''
        self.counts  = counts
        self.success = success
        self.error   = error

    def __repr__(self):
        if self.success:
            return f"ExecutionResult(success=True, counts={self.counts})"
        return f"ExecutionResult(success=False, error={self.error})"


class ExecutionFuture:
    '''
    Wraps a synchronous ExecutionResult to look like a future.

    Keeps the interface identical to what Phase 4 async execution
    will need — the Kernel only ever calls .done() and .result(),
    so swapping in a real async future requires no kernel changes.
    '''

    def __init__(self, result: ExecutionResult):
        self._result = result
        self._done   = True     # synchronous — always immediately done

    def done(self) -> bool:
        return self._done

    def result(self) -> ExecutionResult:
        return self._result

    def __repr__(self):
        state = "done" if self._done else "pending"
        return f"ExecutionFuture(state={state}, result={self._result})"