## @file currency.py
##
## @brief Currency formatters for log-it.
##
## Uses Python's ``locale`` module (stdlib) by default.  If Babel is
## installed it is preferred for richer locale and symbol support.
##
## Import explicitly — not loaded by ``log_it.fmt`` automatically::
##
##     from log_it.fmt import currency
##
##     Col("price", width=10, align="right", fmt=currency.gbp)
##     Col("cost",  width=10, align="right", fmt=currency.of("EUR", locale="de_DE"))
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT

## Not yet implemented.
