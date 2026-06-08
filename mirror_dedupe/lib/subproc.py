## @file subproc.py
##
## @brief Subprocess tracking for Ctrl-C support.
##
## Provides a global registry of active subprocesses and signal-safe
## kill helpers so that a SIGINT can terminate all children immediately
## without waiting for Python's normal cleanup.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT

from __future__ import annotations

import subprocess
import threading
from typing import List, Optional, Tuple

_active_procs: set[subprocess.Popen] = set()
_active_lock = threading.Lock()


def register(proc: subprocess.Popen) -> None:
    ## @brief Track a subprocess for Ctrl-C kill.
    ## @param proc  ``subprocess.Popen`` instance to track.
    ## @return None
    with _active_lock:
        _active_procs.add(proc)


def unregister(proc: subprocess.Popen) -> None:
    ## @brief Stop tracking a subprocess (already finished).
    ## @param proc  ``subprocess.Popen`` instance to unregister.
    ## @return None
    with _active_lock:
        _active_procs.discard(proc)


def kill_active_subprocesses() -> None:
    ## @brief Kill all tracked subprocesses (called on Ctrl-C).
    ##
    ## Iterates a snapshot of the active set so concurrent modifications
    ## during kill don't race.  Silently ignores ``OSError`` for processes
    ## that already exited.
    ##
    ## @return None
    with _active_lock:
        _kill_all_no_lock()


def _kill_all_no_lock() -> None:
    ## @brief Iterate and kill all tracked processes (no lock held).
    ##
    ## Separated so the signal handler can call it with a non-blocking
    ## lock attempt — acquiring a ``threading.Lock`` from a signal handler
    ## risks deadlock if another thread holds it at the moment of the
    ## interrupt.
    ##
    ## @return None
    for p in list(_active_procs):
        try:
            p.kill()
        except OSError:
            pass
    _active_procs.clear()


def kill_active_subprocesses_signal_safe() -> None:
    ## @brief Signal-handler-safe variant of ``kill_active_subprocesses``.
    ##
    ## Attempts a non-blocking lock acquisition and bails if the lock is
    ## held (the other thread will finish quickly and the process is about
    ## to exit anyway).  Call this from a SIGINT handler before
    ## ``os._exit()``.
    ##
    ## @return None
    if _active_lock.acquire(blocking=False):
        try:
            _kill_all_no_lock()
        finally:
            _active_lock.release()


def run_subprocess(args: List[str]) -> Tuple[int, Optional[bytes], bytes]:
    ## @brief Run a subprocess with stdout/stderr capture and Ctrl-C safety.
    ##
    ## Registers the process so that a SIGINT kills it immediately,
    ## then unregisters once ``communicate()`` returns.  If the Python
    ## process is interrupted mid-communicate the ``except`` block
    ## kills the child before re-raising.
    ##
    ## @param args  Argument list for ``subprocess.Popen``.
    ## @return Tuple of ``(returncode, stdout_bytes, stderr_bytes)``.
    stdout_dest: int | None = subprocess.PIPE
    proc = subprocess.Popen(args, stdout=stdout_dest, stderr=subprocess.PIPE)
    register(proc)
    try:
        out, err = proc.communicate()
    except:  # noqa: E722
        try:
            proc.kill()
        except OSError:
            pass
        proc.wait()
        raise
    finally:
        unregister(proc)
    return proc.returncode, out, err
