"""Automated Server Health & Backup Log Monitor.

Runs a single health check of the machine it is executed on:

* CPU, memory and disk utilisation (collected with psutil)
* the status of the most recent entry in a backup log file

Results are evaluated against warning thresholds, printed as a terminal
dashboard and saved to a local SQLite database. The script runs once and
exits, so it can be scheduled with Windows Task Scheduler or Linux cron.

Exit codes (useful for schedulers and other automation):
    0  OK       - every check passed
    1  WARNING  - the monitored system needs attention
    2  USAGE    - invalid command-line arguments
    3  ERROR    - the monitoring tool itself failed (e.g. database error,
                  missing dependency)
"""

from __future__ import annotations

import argparse
import logging
import os
import platform
import socket
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

try:
    import psutil
except ImportError:  # Reported clearly in main() instead of crashing on import.
    psutil = None  # type: ignore[assignment]

__version__ = "1.0.1"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Default files live next to this script, NOT in the current working
# directory. Task Scheduler and cron often start programs in a different
# folder (e.g. C:\Windows\System32 or the user's home directory).
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_BACKUP_LOG = SCRIPT_DIR / "backup.log"
DEFAULT_DATABASE = SCRIPT_DIR / "system_health.db"

DEFAULT_DISK_THRESHOLD = 85.0    # Warn when disk usage is GREATER than this.
DEFAULT_MEMORY_THRESHOLD = 90.0  # Warn when memory usage is GREATER than this.
DEFAULT_CPU_INTERVAL = 1.0       # Seconds to sample CPU usage over.

EXIT_OK = 0
EXIT_WARNING = 1
EXIT_USAGE = 2
EXIT_ERROR = 3

STATUS_OK = "OK"
STATUS_WARNING = "WARNING"

# Backup status values returned by check_backup_log().
BACKUP_SUCCESS = "SUCCESS"
BACKUP_FAILED = "FAILED"
BACKUP_LOG_MISSING = "LOG_MISSING"
BACKUP_LOG_EMPTY = "LOG_EMPTY"
BACKUP_LOG_UNREADABLE = "LOG_UNREADABLE"
BACKUP_UNKNOWN = "UNKNOWN"
RECOGNISED_LOG_STATUSES = {BACKUP_SUCCESS, BACKUP_FAILED}

LOG_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"
MAX_HISTORY = 1000
SEPARATOR = "=" * 40

logger = logging.getLogger("server_health_monitor")


# ---------------------------------------------------------------------------
# 1. System metrics
# ---------------------------------------------------------------------------

def default_disk_path() -> str:
    """Return the system drive on Windows (usually C:\\) or / elsewhere."""
    if platform.system() == "Windows":
        return os.environ.get("SystemDrive", "C:") + "\\"
    return "/"


def collect_system_metrics(
    disk_path: str, cpu_interval: float = DEFAULT_CPU_INTERVAL
) -> dict[str, Any]:
    """Collect CPU, memory and disk metrics for this machine.

    Each metric is collected independently: if one fails, its value is
    ``None``, a message is added to ``errors`` and collection continues.
    Percentages are rounded to one decimal place so that the value shown
    on screen is exactly the value compared against thresholds.
    """
    metrics: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc),
        "hostname": socket.gethostname(),
        "disk_path": disk_path,
        "cpu_usage": None,
        "memory_usage": None,
        "memory_available": None,
        "disk_usage": None,
        "disk_free": None,
        "errors": [],
    }

    # cpu_percent(interval=...) measures over a real time window. Calling it
    # with no interval the first time always returns a meaningless 0.0.
    try:
        metrics["cpu_usage"] = round(psutil.cpu_percent(interval=cpu_interval), 1)
    except (psutil.Error, OSError) as exc:
        metrics["errors"].append(f"CPU usage could not be collected: {exc}")

    try:
        memory = psutil.virtual_memory()
        metrics["memory_usage"] = round(memory.percent, 1)
        metrics["memory_available"] = memory.available
    except (psutil.Error, OSError) as exc:
        metrics["errors"].append(f"Memory usage could not be collected: {exc}")

    try:
        disk = psutil.disk_usage(disk_path)
        metrics["disk_usage"] = round(disk.percent, 1)
        metrics["disk_free"] = disk.free
    except (psutil.Error, OSError) as exc:
        metrics["errors"].append(
            f"Disk usage for {disk_path} could not be collected: {exc}"
        )

    for message in metrics["errors"]:
        logger.error(message)
    return metrics


# ---------------------------------------------------------------------------
# 2. Backup log
# ---------------------------------------------------------------------------

def read_last_non_empty_line(path: Path, block_size: int = 4096) -> Optional[str]:
    """Return the last non-blank line of a file, or None if there is none.

    The file is read backwards in small blocks, so a multi-gigabyte log
    does not have to be loaded into memory just to find its final entry.
    Raises OSError if the file cannot be read and UnicodeDecodeError if
    the line is not valid UTF-8.
    """
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        position = handle.tell()
        buffer = b""
        while position > 0:
            read_size = min(block_size, position)
            position -= read_size
            handle.seek(position)
            buffer = handle.read(read_size) + buffer
            lines = buffer.splitlines()
            # Unless we have reached the start of the file, the first line
            # in the buffer may be only the tail end of a longer line.
            complete_lines = lines if position == 0 else lines[1:]
            for line in reversed(complete_lines):
                if line.strip():
                    return line.decode("utf-8").lstrip("\ufeff").strip()
    return None


def parse_backup_line(line: str) -> tuple[str, Optional[datetime], str]:
    """Parse one log line into (status, timestamp, explanation).

    Expected format: ``YYYY-MM-DD HH:MM:SS STATUS message``.
    The status must be the third whitespace-separated field, so a message
    such as "INFO previous SUCCESS was slow" is not mistaken for SUCCESS.
    """
    parts = line.split(maxsplit=3)
    if len(parts) < 3:
        return BACKUP_UNKNOWN, None, f"Malformed log entry: {line!r}"

    try:
        timestamp = datetime.strptime(f"{parts[0]} {parts[1]}", LOG_TIMESTAMP_FORMAT)
    except ValueError:
        return BACKUP_UNKNOWN, None, f"Invalid timestamp in log entry: {line!r}"

    status = parts[2]
    if status not in RECOGNISED_LOG_STATUSES:
        return BACKUP_UNKNOWN, timestamp, f"Unrecognised status {status!r} in log entry"

    message = parts[3] if len(parts) > 3 else ""
    return status, timestamp, message


def check_backup_log(path: Path) -> dict[str, Any]:
    """Determine the status of the most recent backup recorded in ``path``.

    Returns a dictionary with ``status``, ``timestamp`` (naive local time
    as written in the log, or None) and ``detail`` (human-readable text).
    """
    result: dict[str, Any] = {"status": BACKUP_UNKNOWN, "timestamp": None, "detail": ""}

    if not path.exists():
        result.update(status=BACKUP_LOG_MISSING, detail=f"Backup log not found: {path}")
        return result

    try:
        line = read_last_non_empty_line(path)
    except UnicodeDecodeError:
        result["detail"] = "Latest log entry is not valid UTF-8 text"
        logger.error("Could not decode %s: latest entry is not valid UTF-8", path)
        return result
    except OSError as exc:  # PermissionError, IsADirectoryError, etc.
        result.update(
            status=BACKUP_LOG_UNREADABLE, detail=f"Backup log could not be read: {exc}"
        )
        logger.error("Could not read backup log %s: %s", path, exc)
        return result

    if line is None:
        result.update(status=BACKUP_LOG_EMPTY, detail="Backup log contains no entries")
        return result

    status, timestamp, detail = parse_backup_line(line)
    result.update(status=status, timestamp=timestamp, detail=detail)
    return result


# ---------------------------------------------------------------------------
# 3. Health evaluation
# ---------------------------------------------------------------------------

def evaluate_health(
    metrics: dict[str, Any],
    backup: dict[str, Any],
    disk_threshold: float = DEFAULT_DISK_THRESHOLD,
    memory_threshold: float = DEFAULT_MEMORY_THRESHOLD,
    max_backup_age_hours: Optional[float] = None,
    now: Optional[datetime] = None,
) -> tuple[str, list[str]]:
    """Compare metrics with thresholds and return (overall_status, warnings).

    A value exactly equal to a threshold does NOT trigger a warning; only a
    value greater than the threshold does. A metric that could not be
    collected always produces a warning, so the system is never reported
    healthy on the basis of missing data. CPU usage is not compared with a
    threshold in version 1, but a missing CPU reading still warns.
    """
    warnings: list[str] = []

    if metrics.get("cpu_usage") is None:
        warnings.append("CPU usage could not be collected.")

    memory = metrics.get("memory_usage")
    if memory is None:
        warnings.append("Memory usage could not be collected.")
    elif memory > memory_threshold:
        warnings.append(
            f"Memory usage is {memory:.1f}%, exceeding the {memory_threshold:g}% threshold."
        )

    disk = metrics.get("disk_usage")
    if disk is None:
        warnings.append(
            f"Disk usage for {metrics.get('disk_path', 'the selected path')} "
            "could not be collected."
        )
    elif disk > disk_threshold:
        warnings.append(
            f"Disk usage is {disk:.1f}%, exceeding the {disk_threshold:g}% threshold."
        )

    status = backup.get("status", BACKUP_UNKNOWN)
    if status != BACKUP_SUCCESS:
        message = f"Latest backup status is {status}."
        if backup.get("detail"):
            message += f" ({backup['detail']})"
        warnings.append(message)
    elif max_backup_age_hours is not None and backup.get("timestamp") is not None:
        # Log timestamps have no time zone, so they are treated as the local
        # time of the machine that wrote the log (assumed to be this one).
        current = now or datetime.now()
        age = current - backup["timestamp"]
        if age > timedelta(hours=max_backup_age_hours):
            hours = age.total_seconds() / 3600
            warnings.append(
                f"Latest successful backup is {hours:.1f} hours old, exceeding the "
                f"{max_backup_age_hours:g}-hour limit."
            )

    return (STATUS_WARNING if warnings else STATUS_OK), warnings


# ---------------------------------------------------------------------------
# 4-6. SQLite database
# ---------------------------------------------------------------------------

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS health_checks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT    NOT NULL,
    hostname        TEXT    NOT NULL,
    cpu_usage       REAL,
    memory_usage    REAL,
    disk_usage      REAL,
    backup_status   TEXT    NOT NULL,
    overall_status  TEXT    NOT NULL,
    warning_details TEXT    NOT NULL DEFAULT ''
)
"""


def init_database(db_path: Path) -> None:
    """Create the database file and table if they do not already exist."""
    # closing() guarantees the connection is closed; "with conn" commits on
    # success and rolls back if an exception occurs inside the block.
    with closing(sqlite3.connect(db_path)) as conn:
        with conn:
            conn.execute(CREATE_TABLE_SQL)


def save_health_check(db_path: Path, record: dict[str, Any]) -> int:
    """Insert one monitoring record and return its new row id."""
    with closing(sqlite3.connect(db_path)) as conn:
        with conn:
            cursor = conn.execute(
                """
                INSERT INTO health_checks (
                    timestamp, hostname, cpu_usage, memory_usage, disk_usage,
                    backup_status, overall_status, warning_details
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["timestamp"],
                    record["hostname"],
                    record["cpu_usage"],
                    record["memory_usage"],
                    record["disk_usage"],
                    record["backup_status"],
                    record["overall_status"],
                    record["warning_details"],
                ),
            )
            return int(cursor.lastrowid)


def get_recent_checks(db_path: Path, limit: int) -> list[dict[str, Any]]:
    """Return up to ``limit`` most recent records, newest first."""
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM health_checks ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# 7. Terminal output
# ---------------------------------------------------------------------------

def format_percent(value: Optional[float]) -> str:
    """Format a percentage to one decimal place, or 'N/A' if missing."""
    return "N/A" if value is None else f"{value:.1f}%"


def format_bytes(value: Optional[int]) -> str:
    """Format a byte count as GB with one decimal place."""
    return "N/A" if value is None else f"{value / 1024 ** 3:.1f} GB"


def print_dashboard(
    metrics: dict[str, Any],
    backup: dict[str, Any],
    overall: str,
    warnings: list[str],
) -> None:
    """Print the health report. Uses plain ASCII so it works in any console."""
    timestamp = metrics["timestamp"].strftime("%Y-%m-%d %H:%M:%S UTC")
    rows = [
        ("Hostname", metrics["hostname"]),
        ("Timestamp", timestamp),
        ("CPU Usage", format_percent(metrics["cpu_usage"])),
        ("Memory Usage", format_percent(metrics["memory_usage"])),
        ("Memory Free", format_bytes(metrics["memory_available"])),
        ("Disk Path", metrics["disk_path"]),
        ("Disk Usage", format_percent(metrics["disk_usage"])),
        ("Disk Free", format_bytes(metrics["disk_free"])),
        ("Backup Status", backup["status"]),
        ("Overall Health", overall),
    ]
    print(SEPARATOR)
    print("SERVER HEALTH MONITOR".center(len(SEPARATOR)).rstrip())
    print(SEPARATOR)
    for label, value in rows:
        print(f"{label:<15}: {value}")
    if warnings:
        print(SEPARATOR)
        for warning in warnings:
            print(f"WARNING: {warning}")
    print(SEPARATOR)


def print_history(records: list[dict[str, Any]]) -> None:
    """Print saved monitoring records as a simple table."""
    if not records:
        print("No monitoring records found.")
        return
    header = (
        f"{'ID':>5}  {'Timestamp (UTC)':<25}  {'Hostname':<18}  "
        f"{'CPU':>6}  {'Memory':>6}  {'Disk':>6}  {'Backup':<14}  Status"
    )
    print(header)
    print("-" * len(header))
    for row in records:
        print(
            f"{row['id']:>5}  {row['timestamp']:<25}  {row['hostname'][:18]:<18}  "
            f"{format_percent(row['cpu_usage']):>6}  "
            f"{format_percent(row['memory_usage']):>6}  "
            f"{format_percent(row['disk_usage']):>6}  "
            f"{row['backup_status']:<14}  {row['overall_status']}"
        )
        if row["warning_details"]:
            for warning in row["warning_details"].split("\n"):
                print(f"{'':>7}- {warning}")


# ---------------------------------------------------------------------------
# 8. Command-line arguments
# ---------------------------------------------------------------------------

def _bounded_number(kind: type, minimum: float, maximum: float, name: str):
    """Build an argparse type function that enforces a numeric range."""
    def convert(text: str):
        try:
            value = kind(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{name} must be a number, got {text!r}")
        if not minimum <= value <= maximum:
            raise argparse.ArgumentTypeError(
                f"{name} must be between {minimum:g} and {maximum:g}, got {text}"
            )
        return value
    return convert


def parse_arguments(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """Parse and validate command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="monitor.py",
        description="Run one server health and backup log check, then exit.",
        epilog="Exit codes: 0 = OK, 1 = WARNING, 2 = invalid arguments, "
               "3 = monitoring tool error.",
    )
    parser.add_argument(
        "--history", metavar="N",
        type=_bounded_number(int, 1, MAX_HISTORY, "--history"),
        help=f"show the N most recent saved checks (1-{MAX_HISTORY}) instead of "
             "running a new check",
    )
    parser.add_argument(
        "--disk-path", default=default_disk_path(),
        help="disk or mount point to monitor (default: %(default)s)",
    )
    parser.add_argument(
        "--backup-log", type=Path, default=DEFAULT_BACKUP_LOG,
        help="backup log file to inspect (default: backup.log next to monitor.py)",
    )
    parser.add_argument(
        "--database", type=Path, default=DEFAULT_DATABASE,
        help="SQLite database file (default: system_health.db next to monitor.py)",
    )
    parser.add_argument(
        "--disk-threshold", type=_bounded_number(float, 0, 100, "--disk-threshold"),
        default=DEFAULT_DISK_THRESHOLD,
        help="warn when disk usage is greater than this percent (default: %(default)s)",
    )
    parser.add_argument(
        "--memory-threshold", type=_bounded_number(float, 0, 100, "--memory-threshold"),
        default=DEFAULT_MEMORY_THRESHOLD,
        help="warn when memory usage is greater than this percent (default: %(default)s)",
    )
    parser.add_argument(
        "--cpu-interval", type=_bounded_number(float, 0.1, 10, "--cpu-interval"),
        default=DEFAULT_CPU_INTERVAL,
        help="seconds to sample CPU usage over (default: %(default)s)",
    )
    parser.add_argument(
        "--max-backup-age-hours",
        type=_bounded_number(float, 0.1, 24 * 365, "--max-backup-age-hours"),
        help="optional: warn when the latest SUCCESS entry is older than this",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="show diagnostic log messages"
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    args = parser.parse_args(argv)
    if args.history is None and not Path(args.disk_path).exists():
        parser.error(f"--disk-path does not exist: {args.disk_path}")
    return args


# ---------------------------------------------------------------------------
# 9. Program control
# ---------------------------------------------------------------------------

def run_check(args: argparse.Namespace) -> int:
    """Run one complete monitoring cycle and return the process exit code."""
    metrics = collect_system_metrics(args.disk_path, args.cpu_interval)
    backup = check_backup_log(args.backup_log)
    overall, warnings = evaluate_health(
        metrics, backup,
        disk_threshold=args.disk_threshold,
        memory_threshold=args.memory_threshold,
        max_backup_age_hours=args.max_backup_age_hours,
    )
    # Collection errors carry technical detail, so they are appended as
    # extra diagnostic warnings (the plain-English warning already exists).
    warnings.extend(f"Diagnostic: {error}" for error in metrics["errors"])
    # Re-derive the overall status so that ANY warning shown on screen is
    # also reflected in the saved status and the exit code.
    overall = STATUS_WARNING if warnings else STATUS_OK

    print_dashboard(metrics, backup, overall, warnings)

    record = {
        "timestamp": metrics["timestamp"].isoformat(timespec="seconds"),
        "hostname": metrics["hostname"],
        "cpu_usage": metrics["cpu_usage"],
        "memory_usage": metrics["memory_usage"],
        "disk_usage": metrics["disk_usage"],
        "backup_status": backup["status"],
        "overall_status": overall,
        "warning_details": "\n".join(warnings),
    }
    try:
        init_database(args.database)
        save_health_check(args.database, record)
    except (sqlite3.Error, OSError) as exc:
        # A failure of the monitoring tool itself, not of the monitored system.
        logger.error("Could not save monitoring record to %s: %s", args.database, exc)
        print(f"ERROR: Monitoring record could NOT be saved ({exc}).")
        print(SEPARATOR)
        return EXIT_ERROR

    print("Monitoring record saved successfully.")
    print(SEPARATOR)
    return EXIT_OK if overall == STATUS_OK else EXIT_WARNING


def show_history(args: argparse.Namespace) -> int:
    """Print saved history and return the process exit code."""
    if not args.database.exists():
        print(f"No monitoring records found (database {args.database} does not exist yet).")
        return EXIT_OK
    try:
        records = get_recent_checks(args.database, args.history)
    except sqlite3.Error as exc:
        logger.error("Could not read history from %s: %s", args.database, exc)
        print(f"ERROR: Could not read monitoring history ({exc}).")
        return EXIT_ERROR
    print_history(records)
    return EXIT_OK


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point: parse arguments, run the requested action, return exit code."""
    args = parse_arguments(argv)  # Exits with code 2 on invalid arguments.
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    if args.history is not None:
        return show_history(args)

    if psutil is None:
        print(
            "ERROR: The 'psutil' package is not installed.\n"
            "Install it with:  python -m pip install -r requirements.txt",
            file=sys.stderr,
        )
        return EXIT_ERROR

    return run_check(args)


if __name__ == "__main__":
    sys.exit(main())
