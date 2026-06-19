## @file exceptions.py
##
## @brief Generic exception base class carrying a numeric *code* and
##        human-readable *message*.
##
## Concrete exception types (e.g. ``HTTPException``, ``CurlException``)
## are defined alongside the code that raises them and inherit from
## this class.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT



class ExceptionMsg(RuntimeError):
    ## @brief Generic exception with structured code / message.
    ##
    ## Subclasses must implement ``_format_message(code)`` to derive
    ## the human-readable string from the numeric code.  The message
    ## can also be passed directly for non-code failures.
    ##
    ## ``str(error)`` returns *message*.
    ##
    ## @param code     Numeric code (e.g. HTTP status, curl exit).
    ## @param message  Optional - if omitted, derived via
    ##                 ``_format_message(code)``.

    def __init__(self, code: int, message: str | None = None):
        self.code = code
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
    pass


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
        self.reason = reason
        super().__init__(reason)
