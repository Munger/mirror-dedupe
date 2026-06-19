## @file science.py
##
## @brief Scientific and engineering notation formatters for log-it.
##
## Provides SI prefix formatting, significant-figure rounding, and
## engineering notation (exponents restricted to multiples of 3).
##
## Import explicitly — not loaded by ``log_it.fmt`` automatically::
##
##     from log_it.fmt import science
##
##     Col("voltage",   width=8, fmt=science.si("V"))
##     Col("frequency", width=8, fmt=science.si("Hz"))
##     Col("value",     width=8, fmt=science.sigfig(3))
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT

## Not yet implemented.
