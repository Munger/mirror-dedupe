## @file exceptions.py
##
## @brief Exception base classes for mirror-dedupe.
##
## All exceptions carry a ``context`` attribute set automatically to the
## ``self`` of the frame that raised them.  Catch sites can inspect it for
## structured logging without any extra boilerplate at the raise site.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT

import sys


def _caller_self() -> object:
    ## @brief Return the ``self`` of the frame that instantiated an exception.
    ##
    ## Walks one frame up from the exception ``__init__`` (frame 0 = this
    ## function, frame 1 = exception __init__, frame 2 = raise site).
    ## Returns ``None`` if the raise site is not inside an instance method.
    try:
        return sys._getframe(2).f_locals.get("self")
    except (AttributeError, ValueError):
        return None


class ExceptionMsg(RuntimeError):
    ## @brief Generic exception with structured code / message.
    ##
    ## Subclasses must implement ``_format_message(code)`` to derive
    ## the human-readable string from the numeric code.  The message
    ## can also be passed directly for non-code failures.
    ##
    ## ``str(error)`` returns *message*.
    ## ``error.context`` is the object whose method raised the exception.
    ##
    ## @param code     Numeric code (e.g. HTTP status, curl exit).
    ## @param message  Optional - if omitted, derived via
    ##                 ``_format_message(code)``.

    def __init__(self, code: int, message: str | None = None):
        self.code = code
        self.context = _caller_self()
        if message is not None:
            self.message = message
        else:
            self.message = self._format_message(code)
        super().__init__(self.message)

    def _format_message(self, code: int) -> str:
        raise NotImplementedError


class StagingLockTimeout(Exception):
    ## @brief Raised when a worker cannot acquire the per-hash staging lock
    ##        within the timeout period.
    ##
    ## The coordinator catches this and requeues the node at the back of
    ## the stack so it is retried once the competing download completes,
    ## breaking lockstep between repos downloading the same content.
    ##
    ## ``context`` is set automatically to the node that timed out.

    def __init__(self) -> None:
        self.context = _caller_self()
        super().__init__()


class RepoAbortError(Exception):
    ## @brief Raised to abort a single repo's sync immediately.
    ##
    ## Caught specifically in ``Repos._sync_one()`` so it does not
    ## propagate to the global error handler.  The stale sweep is
    ## skipped automatically because the exception unwinds past it.
    ##
    ## Raise after calling ``MDNode.abort()`` - which sets the abort
    ## event and kills the repo's curl processes - so the caller's
    ## own context exits immediately rather than waiting for the
    ## coordinator to detect the event.
    ##
    ## @param reason  Human-readable explanation logged at WARN level.

    def __init__(self, reason: str) -> None:
        self.context = _caller_self()
        self.reason = reason
        super().__init__(reason)
