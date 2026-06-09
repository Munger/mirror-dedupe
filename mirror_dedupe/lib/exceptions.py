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

from __future__ import annotations


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
    ## @param message  Optional — if omitted, derived via
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
