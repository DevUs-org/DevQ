'''
Tags: Main

ExecutionResult — Structured result returned after circuit execution.
ExecutionFuture — Synchronous future wrapper (reference implementation).
AsyncExecutionFuture — Real asynchronous future (Phase 4).

Both futures expose the identical done()/result() interface the Kernel
polls — which future a provider returns is invisible above the
provider layer. ExecutionFuture remains the simplest possible
reference for provider authors; AsyncExecutionFuture wraps a
concurrent.futures.Future so execution genuinely overlaps with
scheduling, routing, and shell interaction.
'''

import atexit
import concurrent.futures


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
    Wraps an already-computed ExecutionResult to look like a future.
    The reference implementation for providers whose execution is
    synchronous — done() is immediately True.
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


class AsyncExecutionFuture:
    '''
    Wraps a concurrent.futures.Future whose callable returns an
    ExecutionResult. Same done()/result() surface as ExecutionFuture.

    A callable that raises is converted into a failed ExecutionResult
    rather than propagating — the kernel's contract is that result()
    always yields an ExecutionResult.
    '''

    def __init__(self, future: concurrent.futures.Future):
        self._future = future

    def done(self) -> bool:
        return self._future.done()

    def result(self) -> ExecutionResult:
        try:
            return self._future.result()
        except Exception as e:
            return ExecutionResult(counts=None, success=False, error=str(e))

    def __repr__(self):
        state = "done" if self.done() else "pending"
        return f"AsyncExecutionFuture(state={state})"


# Shared pool for providers that execute via AsyncExecutionFuture.
# Module-level singleton — provider authors call submit_async(fn, *args).
_EXECUTOR = None


def submit_async(fn, *args, **kwargs) -> AsyncExecutionFuture:
    '''
    Run fn(*args, **kwargs) -> ExecutionResult on the shared executor,
    returning an AsyncExecutionFuture the kernel can poll.
    '''
    global _EXECUTOR
    if _EXECUTOR is None:
        _EXECUTOR = concurrent.futures.ThreadPoolExecutor(
            max_workers=8, thread_name_prefix="devq-exec"
        )
        atexit.register(shutdown_executor)
    return AsyncExecutionFuture(_EXECUTOR.submit(fn, *args, **kwargs))


def shutdown_executor(wait=True):
    '''
    Shut the shared executor down and drop it, so a later submit_async()
    builds a fresh one.

    ThreadPoolExecutor workers are NON-daemon: without this, they stay
    alive after the last job resolves and the interpreter blocks on them
    at exit. One interactive session barely notices, but a process that
    builds many sessions — the test runner, or any batch driver —
    accumulates idle workers and appears to hang after its final output.

    Registered with atexit on first use, so normal programs need not
    call it. Call it directly to reclaim threads mid-process, e.g.
    between independent sessions in a long-running harness.
    '''
    global _EXECUTOR
    if _EXECUTOR is not None:
        _EXECUTOR.shutdown(wait=wait)
        _EXECUTOR = None