## @file stats.py
##
## @brief Sync statistics lifecycle - write, read, format, and reset.
##
## Centralises all NDJSON stats management so callers never touch
## ``mirror-dedupe/<name>/stats.ndjson`` directly.  Provides the
## formatting helpers used by the end-of-sync summary table and
## the ``--stats`` / ``--stats-reset`` CLI commands.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT

import json
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional


def _ensure_dir(mirror_root: str, name: str) -> Path:
    ## @brief Return the path to ``stats.ndjson``, creating the parent
    ##        directory if needed.
    ##
    ## @param mirror_root  Absolute path to the mirror root.
    ## @param name         Repo name (subdirectory under ``mirror-dedupe/``).
    ## @return ``Path`` to ``stats.ndjson``.
    stats_dir = Path(mirror_root) / "mirror-dedupe" / name
    stats_dir.mkdir(parents=True, exist_ok=True)
    return stats_dir / "stats.ndjson"


def write_ndjson(
    session_ts: str,
    repo: Any,
    mirror_root: Optional[str] = None,
    peak_rss_mb: int = 0,
) -> None:
    ## @brief Append a stats NDJSON record for *repo* to its per-repo file.
    ##
    ## Writes to ``<mirror_root>/mirror-dedupe/<name>/stats.ndjson``.
    ## Computes delta_files and delta_bytes against the previous
    ## record for trend analysis.
    ##
    ## @param session_ts   ISO-8601 timestamp of the sync session start.
    ## @param repo         A ``Repo`` instance (duck-typed: needs a
    ##                     ``.stats()`` method and a ``.get("name")``).
    ## @param mirror_root  Optional mirror_root override.  Falls back to
    ##                     ``Config.mirror_root`` when ``None``.
    ## @param peak_rss_mb  Peak RSS in MB for this sync session (default 0).
    ## @return None
    name = repo.get("name", "")
    if not name:
        return

    if not mirror_root:
        from .config import Config
        cfg = Config.load()
        mirror_root = cfg.mirror_root

    assert mirror_root is not None
    stats_file = _ensure_dir(mirror_root, name)

    import time

    s = repo.stats()
    record = {
        "session_ts": session_ts,
        "ts": int(time.time()),
        "elapsed": round(s["elapsed"], 2),
        "file_count": s["file_count"],
        "total_bytes": s["total_bytes"],
        "deduped_files": s.get("deduped_files", 0),
        "deduped_bytes": s["deduped_bytes"],
        "bytes_transferred": s["bytes_transferred"],
        "errors": s["errors"],
        "gpg_failures": s.get("gpg_failures", 0),
        "no_response": s.get("no_response", 0),
        "pool_hits": s["pool_hits"],
        "pool_misses": s["pool_misses"],
        "removed": s["removed"],
        "peak_rss_mb": peak_rss_mb,
    }

    # Deltas from previous record
    try:
        # Initialise line so that if stats_file exists but is empty the
        # for-loop never executes and line remains a defined local rather
        # than causing UnboundLocalError (not caught by the OSError /
        # JSONDecodeError handler below).
        line = ""
        with open(stats_file) as f:
            for line in f:
                pass
        prev = json.loads(line) if line else {}
        curr_file_count = record["file_count"]
        prev_file_count = prev.get("file_count", 0)
        curr_bytes = record["total_bytes"]
        prev_bytes = prev.get("total_bytes", 0)
        record["delta_files"] = curr_file_count - prev_file_count
        record["delta_bytes"] = curr_bytes - prev_bytes
    except (OSError, json.JSONDecodeError):
        record["delta_files"] = 0
        record["delta_bytes"] = 0

    with open(stats_file, "a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_ndjson(
    mirror_root: str, name: str
) -> Generator[Dict, None, None]:
    ## @brief Yield parsed NDJSON records for a repo, newest first.
    ##
    ## Reads ``<mirror_root>/mirror-dedupe/<name>/stats.ndjson`` and
    ## yields each line as a parsed dict, starting from the most
    ## recent record.
    ##
    ## @param mirror_root  Absolute path to the mirror root.
    ## @param name         Repo name.
    ## @return Generator yielding dicts with raw NDJSON field names
    ##         (``file_count``, ``total_bytes``, etc.).
    stats_file = Path(mirror_root) / "mirror-dedupe" / name / "stats.ndjson"
    if not stats_file.exists():
        return
    lines = stats_file.read_text().splitlines()
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def clear_ndjson(mirror_root: str, name: str) -> Optional[Path]:
    ## @brief Truncate (clear) the NDJSON stats file for a repo.
    ##
    ## @param mirror_root  Absolute path to the mirror root.
    ## @param name         Repo name.
    ## @return The ``Path`` that was cleared, or ``None`` if the file
    ##         did not exist.
    stats_file = Path(mirror_root) / "mirror-dedupe" / name / "stats.ndjson"
    if not stats_file.exists():
        return None
    stats_file.write_text("")
    return stats_file


def format_row(s: Dict, name: str) -> Dict:
    ## @brief Convert a raw stats dict into a row dict for ``print_summary_table``.
    ##
    ## Returns raw (unformatted) values keyed by column name.
    ## Formatting is handled by the column definitions in ``print_summary_table``.
    ##
    ## @param s     Stats dict (from ``Repo.stats()`` or an NDJSON record).
    ## @param name  Repo name.
    ## @return Dict with raw values for each column.
    return {
        "ts":            s.get("ts"),
        "name":          name,
        "files":         s.get("file_count", 0),
        "total":         s.get("total_bytes", 0),
        "deduped_files": s.get("deduped_files", 0),
        "deduped":       s.get("deduped_bytes", 0),
        "tx":            s.get("bytes_transferred", 0),
        "hits":          s.get("pool_hits", 0),
        "misses":        s.get("pool_misses", 0),
        "errors":        s.get("errors", 0),
        "nr":            s.get("no_response", 0),
        "gpg":           s.get("gpg_failures", 0),
        "time":          s.get("elapsed", 0),
        "removed":       s.get("removed", 0),
    }


def print_summary_table(
    rows: List[Dict],
    *,
    session_start: str = "",
    session_end: str = "",
    session_elapsed: float = 0,
    show_name: bool = True,
    show_total: bool = True,
    title: str = "",
) -> None:
    ## @brief Print a formatted cross-repo summary table to stdout.
    ##
    ## @param rows             List of ``format_row()`` dicts.
    ## @param session_start    Optional start timestamp string.
    ## @param session_end      Optional end timestamp string.
    ## @param session_elapsed  Optional elapsed duration in seconds.
    ## @param show_name        Include the Repository column (default True).
    ## @param show_total       Include a footer totals row (default True).
    ## @param title            Optional table title.
    ## @return None
    import sys
    from datetime import datetime
    from .lib import fmt_duration
    from .lib.log_it import Col, Table, TerminalSink
    from .lib.log_it import fmt as logfmt

    if not rows:
        return

    show_dt = any(r.get("ts") for r in rows)
    ft = show_total

    def _dt(v) -> str:
        try:
            return datetime.fromtimestamp(v).strftime("%x %X") if v else ""
        except (OSError, OverflowError, ValueError):
            return ""

    def _int(v) -> str:
        return f"{int(v):,}" if v else ""

    cols: list[Col] = []
    if show_dt:
        cols.append(Col("ts",      header="Date/Time",    fmt=_dt))
    if show_name:
        cols.append(Col("name",    header="Repository",
                        footer=(lambda v: "Total") if ft else None))
    _sum_int = (lambda v: _int(sum(v))) if ft else None
    cols += [
        Col("files",         header="Files",        align="right",
            fmt=_int,            footer=_sum_int),
        Col("total",         header="Total",        align="right",
            fmt=logfmt.filesize, footer=sum if ft else None),
        Col("deduped_files", header="Shared",       align="right",
            fmt=_int,            footer=_sum_int),
        Col("deduped",       header="Shared bytes", align="right",
            fmt=logfmt.filesize, footer=sum if ft else None),
        Col("tx",            header="Transferred",  align="right",
            fmt=logfmt.filesize, footer=sum if ft else None),
        Col("hits",    header="Hit",           align="right",
            fmt=_int,            footer=_sum_int),
        Col("misses",  header="Miss",          align="right",
            fmt=_int,            footer=_sum_int),
        Col("errors",  header="Errors",        align="right",
            footer=(lambda v: str(sum(v))) if ft else None),
        Col("nr",      header="N/R",           align="right",
            fmt=_int,            footer=_sum_int),
        Col("gpg",     header="GPG",
            fmt=lambda v: "FAIL" if v else "pass",
            footer=(lambda v: "FAIL" if any(v) else "pass") if ft else None),
        Col("time",    header="Time",          align="right",
            fmt=lambda v: fmt_duration(v) if v else "",
            footer=(lambda _: fmt_duration(session_elapsed) if session_elapsed else "") if ft else None),
        Col("removed", header="Removed",       align="right",
            fmt=_int,            footer=_sum_int),
    ]

    t = Table(*cols, title=title)
    for r in rows:
        t.add(**{k: r.get(k) for k in (
            "ts", "name", "files", "total", "deduped_files", "deduped", "tx",
            "hits", "misses", "errors", "nr", "gpg", "time", "removed",
        )})

    print("")
    if session_start:
        print(f"  Start:   {session_start}")
    if session_end:
        print(f"  End:     {session_end}")
    if session_elapsed:
        print(f"  Elapsed: {fmt_duration(session_elapsed)}")
    if session_start or session_end or session_elapsed:
        print("")

    t.emit(sinks=[TerminalSink(sys.stdout)])
