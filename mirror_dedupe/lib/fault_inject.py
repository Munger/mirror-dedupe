## @file fault_inject.py
##
## @brief Runtime fault injection for debugging and circuit-breaker testing.
##
## Drop a JSON file at ``/tmp/die`` to trigger synthetic failures at any
## call site that calls ``check()``.  An empty file raises the default
## exception (``HostNotRespondingError``) after a random 10-30 s delay,
## mimicking an upstream stall.
##
## JSON schema (all fields optional):
##
##   {
##     "exception":   "HostNotRespondingError",   # see EXCEPTIONS below
##     "code":        503,                         # HTTP / curl exit code
##     "message":     "custom message",            # for ExceptionMsg fallback
##     "delay":       [10, 30],                    # seconds: fixed or [min, max]
##     "thread":      "test.*",                    # regex matched against thread name
##     "probability": 0.5                          # 0.0-1.0; default 1.0 (always)
##   }
##
## Supported exception names:
##   HostNotRespondingError  - upstream stall / circuit-breaker trigger (default)
##   HTTPException           - HTTP 4xx/5xx; requires "code"
##   CurlException           - curl subprocess failure; requires "code"
##   HashMismatch            - corrupt download
##   ExceptionMsg            - generic; uses "message"
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT


_SENTINEL = "/tmp/die"


def check() -> None:
    ## @brief Check for ``/tmp/die`` and raise a synthetic exception if present.
    ##
    ## No-op when the sentinel file does not exist.  Reads and parses the
    ## file contents as JSON on every call; an empty or invalid file falls
    ## back to default behaviour (HostNotRespondingError, 10-30 s delay).
    ##
    ## Thread and probability filters are evaluated before the delay so
    ## filtered-out calls return immediately without sleeping.
    ##
    ## @raises HostNotRespondingError  (default)
    ## @raises HTTPException           when ``exception`` is ``"HTTPException"``
    ## @raises CurlException           when ``exception`` is ``"CurlException"``
    ## @raises ExceptionMsg            for ``"HashMismatch"`` or ``"ExceptionMsg"``

    import os
    if not os.path.exists(_SENTINEL):
        return

    import json
    import random
    import re
    import threading
    import time

    try:
        raw = open(_SENTINEL).read().strip()
        cfg: dict = json.loads(raw) if raw else {}
    except (OSError, json.JSONDecodeError):
        cfg = {}

    # Thread filter - skip if current thread name doesn't match
    thread_pattern = cfg.get("thread")
    if thread_pattern and not re.search(thread_pattern, threading.current_thread().name):
        return

    # Probability filter
    if random.random() > cfg.get("probability", 1.0):
        return

    # Delay: fixed seconds or [min, max] range
    delay = cfg.get("delay", [10, 30])
    if isinstance(delay, list) and len(delay) == 2:
        time.sleep(random.uniform(float(delay[0]), float(delay[1])))
    elif isinstance(delay, (int, float)) and delay > 0:
        time.sleep(float(delay))

    # Raise the configured exception
    exc = cfg.get("exception", "HostNotRespondingError")
    code = cfg.get("code", 0)
    message = cfg.get("message", exc)

    if exc == "HostNotRespondingError":
        from .http_download import HostNotRespondingError
        raise HostNotRespondingError()
    elif exc == "HTTPException":
        from .http_download import HTTPException
        raise HTTPException(code)
    elif exc == "CurlException":
        from .http_download import CurlException
        raise CurlException(code)
    elif exc == "HashMismatch":
        from .exceptions import ExceptionMsg
        raise ExceptionMsg(0, "Hash mismatch")
    else:
        from .exceptions import ExceptionMsg
        raise ExceptionMsg(code, message)
