## @file stats.py
##
## @brief Sync statistics lifecycle — write, read, format, and reset.
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
from typing import Dict, Generator, List, Optional


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
    repo: object,
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

    stats_file = _ensure_dir(mirror_root, name)

    from .lib.datetimeutils import fmt_isotimestamp

    s = repo.stats()
    record = {
        "session_ts": session_ts,
        "ts": fmt_isotimestamp(),
        "elapsed": round(s["elapsed"], 2),
        "file_count": s["file_count"],
        "total_bytes": s["total_bytes"],
        "deduped_bytes": s["deduped_bytes"],
        "bytes_transferred": s["bytes_transferred"],
        "errors": s["errors"],
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


def _col_widths(rows: List[Dict[str, str]]) -> Dict[str, int]:
    ## @brief Compute column widths for the summary table.
    ##
    ## @param rows  List of formatted row dicts (output of ``format_row()``).
    ## @return Dict mapping column name to minimum width in characters.
    widths = {"dt": 17, "name": 20, "files": 8, "total": 10, "deduped": 12,
              "tx": 12, "hit": 8, "miss": 8, "errors": 6, "time": 11,
              "removed": 8}
    for r in rows:
        for k, v in r.items():
            if k in widths:
                widths[k] = max(widths[k], len(v))
    return widths


def _pad(s: str, width: int) -> str:
    ## @brief Left-justify *s* to *width*.
    return s.ljust(width) if len(s) < width else s


# Column definitions: (dict_key, heading, alignment)
# "dt" is omitted automatically when all rows have an empty dt value.
_COLS = [
    ("dt", "Date/Time", "left"),
    ("name", "Repository", "left"),
    ("files", "Files", "right"),
    ("total", "Total", "right"),
    ("deduped", "Deduplicated", "right"),
    ("tx", "Transferred", "right"),
    ("hit", "Hit", "right"),
    ("miss", "Miss", "right"),
    ("errors", "Errors", "right"),
    ("time", "Time", "right"),
    ("removed", "Removed", "right"),
]


def print_summary_table(
    rows: List[Dict[str, str]],
    *,
    session_start: str = "",
    session_end: str = "",
    session_elapsed: str = "",
    show_name: bool = True,
    show_total: bool = True,
    title: str = "",
) -> None:
    ## @brief Print a formatted cross-repo summary table to stdout.
    ##
    ## Renders the same table as the post-sync summary, with session
    ## header, column headings, separator lines, data rows, and an
    ## aggregated ``Total`` row.
    ##
    ## @param rows             List of ``format_row()`` dicts.
    ## @param session_start    Optional start timestamp (printed as header).
    ## @param session_end      Optional end timestamp.
    ## @param session_elapsed  Optional elapsed duration string.
    ## @return None
    if not rows:
        return

    from .lib import fmt_duration

    show_dt = any(r.get("dt") for r in rows)
    cols = [
        (k, h, a) for k, h, a in _COLS
        if (k != "dt" or show_dt) and (k != "name" or show_name)
    ]

    cw = _col_widths(rows)
    sep = "  ".join("-" * cw[k] for k, _, _ in cols)

    total_files = sum(int(r["files"].replace(",", "")) for r in rows)
    total_bytes = sum(parse_fmt(r["total"]) for r in rows)
    total_deduped = sum(parse_fmt(r["deduped"]) for r in rows)
    total_tx = sum(parse_fmt(r["tx"]) for r in rows)
    total_hits = sum(int(r["hit"].replace(",", "")) for r in rows)
    total_misses = sum(int(r["miss"].replace(",", "")) for r in rows)
    total_errors = sum(int(r["errors"]) for r in rows)
    total_removed = sum(int(r["removed"].replace(",", "")) for r in rows)

    print("")
    if session_start:
        print(f"  Start:   {session_start}")
    if session_end:
        print(f"  End:     {session_end}")
    if session_elapsed:
        print(f"  Elapsed: {fmt_duration(session_elapsed)}")
    if session_start or session_end or session_elapsed:
        print("")
    if title:
        label = f"  {title}  "
        remaining = max(0, len(sep) - len(label))
        left = remaining // 2
        print("-" * left + label + "-" * (remaining - left))
    else:
        print(sep)

    header = ""
    for key, heading, align in cols:
        w = cw[key]
        header += _pad(heading, w) if align == "left" else heading.rjust(w)
        header += "  "
    print(header.rstrip("  "))
    print(sep)

    for r in rows:
        line = ""
        for key, _, align in cols:
            val = r.get(key, "")
            w = cw[key]
            line += (_pad(val, w) if align == "left" else val.rjust(w)) + "  "
        print(line.rstrip("  "))

    print(sep)
    if show_total:
        total_vals = {
            "dt": "",
            "name": "Total",
            "files": fmt_int(total_files),
            "total": fmt_bytes(total_bytes),
            "deduped": fmt_bytes(total_deduped),
            "tx": fmt_bytes(total_tx),
            "hit": fmt_int(total_hits),
            "miss": fmt_int(total_misses),
            "errors": str(total_errors),
            "time": fmt_duration(session_elapsed) if session_elapsed else "",
            "removed": fmt_int(total_removed),
        }
        total_line = ""
        for key, _, align in cols:
            val = total_vals.get(key, "")
            w = cw[key]
            total_line += (_pad(val, w) if align == "left" else val.rjust(w)) + "  "
        print(total_line.rstrip("  "))
    print("")


def fmt_bytes(b: int) -> str:
    ## @brief Format a byte count as a human-readable string.
    ## @param b  Byte count.
    ## @return e.g. ``"450MB"``, ``"1.2GB"``.
    if b >= 1073741824:
        return f"{b / 1073741824:.1f}GB"
    if b >= 1048576:
        return f"{b / 1048576:.0f}MB"
    if b >= 1024:
        return f"{b / 1024:.0f}KB"
    return f"{b}B"


def fmt_int(n: int) -> str:
    ## @brief Format an integer with thousands separators.
    ## @param n  Integer to format.
    ## @return e.g. ``"1,234"``.
    return f"{n:,}" if n >= 1000 else str(n)


def parse_fmt(s: str) -> int:
    ## @brief Parse a human-readable byte string back to an integer.
    ## @param s  e.g. ``"450MB"``, ``"1.2GB"``.
    ## @return Byte count.
    s = s.strip()
    if s.endswith("GB"):
        return int(float(s[:-2]) * 1073741824)
    if s.endswith("MB"):
        return int(float(s[:-2]) * 1048576)
    if s.endswith("KB"):
        return int(float(s[:-2]) * 1024)
    if s.endswith("B") and not any(c in s for c in "GMK"):
        return int(s[:-1])
    return 0


def format_row(s: Dict, name: str) -> Dict[str, str]:
    ## @brief Convert a raw stats dict into a display row for a summary table.
    ##
    ## @param s     Stats dict (from ``Repo.stats()`` or an NDJSON record).
    ## @param name  Repo name.
    ## @return Dict with string values for each column.
    from datetime import datetime
    from .lib import fmt_duration

    ts = s.get("ts")
    try:
        dt_str = datetime.fromtimestamp(ts).strftime("%x %X") if ts else ""
    except (OSError, OverflowError, ValueError):
        dt_str = ""

    return {
        "dt": dt_str,
        "name": name,
        "files": fmt_int(s.get("file_count", 0)),
        "total": fmt_bytes(s.get("total_bytes", 0)),
        "deduped": fmt_bytes(s.get("deduped_bytes", 0)),
        "tx": fmt_bytes(s.get("bytes_transferred", 0)),
        "hit": fmt_int(s.get("pool_hits", 0)),
        "miss": fmt_int(s.get("pool_misses", 0)),
        "errors": str(s.get("errors", 0)),
        "time": fmt_duration(s.get("elapsed", 0)),
        "removed": fmt_int(s.get("removed", 0)),
    }
