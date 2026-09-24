"""Spawn-isolated connection tests, without credentials or exception text in IPC.

Use probe_provider(settings_root) in the API instead of store.provider().test().
Only a directory path and an anonymous shared integer go to the child. The child
opens the private settings record itself; it never sends a provider, key, body,
exception, or message back. The parent reconstructs errors from local classes.

The deadline includes child startup, settings reads, DNS and HTTP. Cleanup adds
at most two CLEANUP_TIMEOUT joins (terminate, then kill if necessary). OS process
creation/termination and scheduler stalls cannot be given a Python wall-clock
guarantee; no unbounded join or background thread is used to conceal that limit.
This helper supervises only the connection test, not task execution processes.
"""

from __future__ import annotations

import logging
import math
import multiprocessing
import os
import sys
import time
from pathlib import Path

from devflow.provider import (
    DEFAULT_TIMEOUT,
    MAX_TIMEOUT,
    ProviderAuthenticationError,
    ProviderError,
    ProviderInputError,
    ProviderInvalidJSONError,
    ProviderRateLimitError,
    ProviderRefusalError,
    ProviderResponseError,
    ProviderSettingsError,
    ProviderSettingsStore,
    ProviderTimeoutError,
    ProviderTransportError,
    ProviderTruncatedResponseError,
)

CLEANUP_TIMEOUT = 1.0


class ProviderProbeError(ProviderError):
    code = "provider_probe_error"
    public_message = "provider connection test worker failed; try the connection test again"


class ProviderProbeCleanupError(ProviderError):
    code = "provider_probe_cleanup_error"
    public_message = "provider connection test worker could not be stopped; restart the backend"


class ProviderProbeContextError(ProviderError):
    code = "provider_probe_context_error"
    public_message = "provider connection test must run from the API process, not a daemon task worker"


# Local status encoding only, not a serialized object/schema or a public API.
# Preserve known categories by exact type, never by reading error.code/message:
# an unexpected subclass or mutated exception can carry arbitrary remote data.
_ERROR_TYPES = (
    ProviderError,
    ProviderSettingsError,
    ProviderInputError,
    ProviderResponseError,
    ProviderTransportError,
    ProviderAuthenticationError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderRefusalError,
    ProviderTruncatedResponseError,
    ProviderInvalidJSONError,
)
_PENDING = 0
_SUCCESS = 1
_FAILED = -1


def _test_status(settings_root: str) -> int:
    """Run inside the muted child; return only an integer chosen locally."""
    try:
        result = ProviderSettingsStore(Path(settings_root)).provider().test()
        if type(result) is not dict or set(result) != {"ok"} or result["ok"] is not True:
            return _FAILED
        return _SUCCESS
    except BaseException as error:  # noqa: BLE001 - child boundary must suppress even SystemExit text
        # Process boundary: even SystemExit must not print its potentially
        # sensitive argument via multiprocessing's exception handling.
        for status, error_type in enumerate(_ERROR_TYPES, start=2):
            if type(error) is error_type:
                return status
        return _FAILED


def _probe_worker(settings_root: str, status) -> None:
    """Module-level spawn target; no closure, key, or provider instance arguments."""
    try:
        # Mute Python streams, native stderr/stdout and logging before reading
        # secrets. A failure to establish this boundary aborts the probe.
        logging.disable(sys.maxsize)
        with open(os.devnull, "w", encoding="utf-8") as sink:
            os.dup2(sink.fileno(), 1)
            os.dup2(sink.fileno(), 2)
            sys.stdout = sys.stderr = sink
            status.value = _test_status(settings_root)
    except BaseException:  # noqa: BLE001 - do not let child bootstrap print sensitive exception text
        status.value = _FAILED


def _reap(process) -> None:
    """Stop and reap on success, timeout, caller interruption, or startup failure."""
    try:
        if process.pid is not None:
            if process.is_alive():
                try:
                    process.terminate()
                except OSError:
                    pass  # Still attempt kill below; never leak an OS message.
                process.join(CLEANUP_TIMEOUT)
            if process.is_alive():
                try:
                    process.kill()
                except OSError:
                    pass
                process.join(CLEANUP_TIMEOUT)
            if process.is_alive():
                raise ProviderProbeCleanupError()
            process.join(0)
        process.close()
    except Exception:  # noqa: BLE001 - cleanup errors must not expose process/OS arguments
        raise ProviderProbeCleanupError() from None


def probe_provider(settings_root: Path, *, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Return {"ok": True} or raise a fresh, stable DevFlowError subclass.

    ``settings_root`` is the separate private settings volume directory, not a
    provider or settings dictionary. ``timeout`` must be finite, > 0 and <= 120
    seconds (default 30). No retries. Always uses Windows-compatible spawn.
    A successful result requires both a valid status and normal child exit
    before the deadline; writing a result cannot bypass timeout/cleanup.

    Call from the normal non-daemon API process. Executable entry points must
    use the usual multiprocessing __main__ guard; this module spawns nothing
    at import time. On cleanup failure, do not claim the child has been reaped.
    """
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not (
        math.isfinite(timeout) and 0 < timeout <= MAX_TIMEOUT
    ):
        raise ProviderInputError()
    # multiprocessing forbids a daemon parent from starting any child, even a
    # non-daemon one. Do not flip its private config or run the probe inline:
    # both would bypass the supervisor's lifetime/hard-timeout boundary.
    if multiprocessing.current_process().daemon:
        raise ProviderProbeContextError()
    process = None
    try:
        # Resolve only the lexical absolute path in the parent; all settings
        # file access (potentially blocking volume I/O) stays inside the child.
        root = str(Path(settings_root).absolute())
        deadline = time.monotonic() + timeout
        context = multiprocessing.get_context("spawn")
        # A single fixed-size shared integer avoids Queue feeder threads and
        # partial Pipe frames whose recv() could block after poll() succeeds.
        status = context.Value("i", _PENDING, lock=False)
        process = context.Process(target=_probe_worker, args=(root, status), daemon=True)
        process.start()
        process.join(max(0.0, deadline - time.monotonic()))
        if process.is_alive() or time.monotonic() >= deadline:
            raise ProviderTimeoutError()
        if process.exitcode != 0:
            raise ProviderProbeError()
        value = status.value
        if value == _SUCCESS:
            return {"ok": True}
        if 2 <= value < 2 + len(_ERROR_TYPES):
            raise _ERROR_TYPES[value - 2]()
        raise ProviderProbeError()
    except ProviderError:
        raise
    except Exception:  # noqa: BLE001 - never propagate spawn/IPC/path exception text
        raise ProviderProbeError() from None
    finally:
        if process is not None:
            _reap(process)
