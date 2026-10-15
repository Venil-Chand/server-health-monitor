"""Automated tests for monitor.py.

Run from the project folder with:
    python -m unittest discover -s tests -v

Every test uses temporary files and databases, so real files such as
backup.log and system_health.db are never touched. System metrics are
mocked so results do not depend on the computer running the tests.
"""

import io
import logging
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

# Make monitor.py (in the parent folder) importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import monitor  # noqa: E402

# Keep expected error log messages out of the test output.
logging.disable(logging.CRITICAL)


def make_metrics(cpu=20.0, memory=50.0, disk=50.0, errors=None):
    """Build a fake metrics dictionary like collect_system_metrics returns."""
    return {
        "timestamp": datetime(2026, 10, 11, 23, 15, 42, tzinfo=timezone.utc),
        "hostname": "TEST-SERVER-01",
        "disk_path": "/",
        "cpu_usage": cpu,
        "memory_usage": memory,
        "memory_available": 8 * 1024 ** 3,
        "disk_usage": disk,
        "disk_free": 100 * 1024 ** 3,
        "errors": errors or [],
    }


SUCCESS_BACKUP = {"status": "SUCCESS", "timestamp": datetime(2026, 10, 11, 23, 0), "detail": ""}


class TempDirTestCase(unittest.TestCase):
    """Gives each test its own temporary folder, deleted afterwards."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write_log(self, content, mode="w"):
        path = self.tmp / "backup.log"
        if mode == "wb":
            path.write_bytes(content)
        else:
            # Written as bytes so line endings are kept exactly as given.
            path.write_bytes(content.encode("utf-8"))
        return path


class BackupLogTests(TempDirTestCase):

    def test_successful_backup_detected(self):
        path = self.write_log(
            "2026-10-10 23:00:00 FAILED Backup interrupted\n"
            "2026-10-11 23:00:00 SUCCESS Daily backup completed\n"
        )
        result = monitor.check_backup_log(path)
        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(result["timestamp"], datetime(2026, 10, 11, 23, 0, 0))

    def test_failed_backup_detected(self):
        path = self.write_log(
            "2026-10-11 23:00:00 SUCCESS Daily backup completed\n"
            "2026-10-12 23:00:00 FAILED Backup interrupted\n"
        )
        self.assertEqual(monitor.check_backup_log(path)["status"], "FAILED")

    def test_missing_log(self):
        result = monitor.check_backup_log(self.tmp / "does_not_exist.log")
        self.assertEqual(result["status"], "LOG_MISSING")

    def test_empty_log(self):
        path = self.write_log("")
        self.assertEqual(monitor.check_backup_log(path)["status"], "LOG_EMPTY")

    def test_whitespace_only_log_is_empty(self):
        path = self.write_log("\n   \n\t\n")
        self.assertEqual(monitor.check_backup_log(path)["status"], "LOG_EMPTY")

    def test_malformed_entry_is_unknown(self):
        path = self.write_log("this is not a log entry\n")
        self.assertEqual(monitor.check_backup_log(path)["status"], "UNKNOWN")

    def test_invalid_timestamp_is_unknown(self):
        path = self.write_log("2026-13-45 99:00:00 SUCCESS Daily backup completed\n")
        self.assertEqual(monitor.check_backup_log(path)["status"], "UNKNOWN")

    def test_unrecognised_status_is_unknown(self):
        path = self.write_log("2026-10-11 23:00:00 PARTIAL Some files skipped\n")
        self.assertEqual(monitor.check_backup_log(path)["status"], "UNKNOWN")

    def test_status_word_inside_message_is_not_trusted(self):
        # "SUCCESS" appears only in the message, not in the status field.
        path = self.write_log("2026-10-11 23:00:00 INFO previous SUCCESS was slow\n")
        self.assertEqual(monitor.check_backup_log(path)["status"], "UNKNOWN")

    def test_lowercase_status_is_unknown(self):
        path = self.write_log("2026-10-11 23:00:00 success Daily backup completed\n")
        self.assertEqual(monitor.check_backup_log(path)["status"], "UNKNOWN")

    def test_trailing_blank_lines_and_windows_line_endings(self):
        path = self.write_log(
            "2026-10-11 23:00:00 FAILED Backup interrupted\r\n"
            "2026-10-12 23:00:00 SUCCESS Daily backup completed\r\n\r\n   \r\n"
        )
        self.assertEqual(monitor.check_backup_log(path)["status"], "SUCCESS")

    def test_large_file_read_in_blocks(self):
        lines = ["2026-10-11 23:00:00 FAILED Backup interrupted"] * 5000
        lines.append("2026-10-12 23:00:00 SUCCESS " + "x" * 300)
        path = self.write_log("\n".join(lines) + "\n")
        # A small block size forces several backwards reads, including a
        # final line longer than one block.
        line = monitor.read_last_non_empty_line(path, block_size=64)
        self.assertTrue(line.startswith("2026-10-12 23:00:00 SUCCESS"))
        self.assertTrue(line.endswith("x" * 300))

    def test_invalid_utf8_is_unknown(self):
        path = self.write_log(b"2026-10-11 23:00:00 SUCCESS \xff\xfe bad bytes\n", mode="wb")
        self.assertEqual(monitor.check_backup_log(path)["status"], "UNKNOWN")

    def test_unreadable_log_is_reported(self):
        path = self.write_log("2026-10-11 23:00:00 SUCCESS ok\n")
        with mock.patch.object(Path, "open", side_effect=PermissionError("denied")):
            result = monitor.check_backup_log(path)
        self.assertEqual(result["status"], "LOG_UNREADABLE")
        self.assertIn("denied", result["detail"])


class HealthEvaluationTests(unittest.TestCase):

    def test_normal_status_is_ok(self):
        status, warnings = monitor.evaluate_health(make_metrics(), SUCCESS_BACKUP)
        self.assertEqual(status, "OK")
        self.assertEqual(warnings, [])

    def test_memory_threshold_warning(self):
        status, warnings = monitor.evaluate_health(make_metrics(memory=94.2), SUCCESS_BACKUP)
        self.assertEqual(status, "WARNING")
        self.assertIn("Memory usage is 94.2%, exceeding the 90% threshold.", warnings)

    def test_disk_threshold_warning(self):
        status, warnings = monitor.evaluate_health(make_metrics(disk=91.0), SUCCESS_BACKUP)
        self.assertEqual(status, "WARNING")
        self.assertIn("Disk usage is 91.0%, exceeding the 85% threshold.", warnings)

    def test_values_exactly_at_threshold_do_not_warn(self):
        status, warnings = monitor.evaluate_health(
            make_metrics(memory=90.0, disk=85.0), SUCCESS_BACKUP
        )
        self.assertEqual(status, "OK")
        self.assertEqual(warnings, [])

    def test_values_just_above_threshold_warn(self):
        status, warnings = monitor.evaluate_health(
            make_metrics(memory=90.1, disk=85.1), SUCCESS_BACKUP
        )
        self.assertEqual(status, "WARNING")
        self.assertEqual(len(warnings), 2)

    def test_high_cpu_does_not_warn_in_version_1(self):
        status, _ = monitor.evaluate_health(make_metrics(cpu=100.0), SUCCESS_BACKUP)
        self.assertEqual(status, "OK")

    def test_failed_backup_warns(self):
        backup = {"status": "FAILED", "timestamp": None, "detail": "Backup interrupted"}
        status, warnings = monitor.evaluate_health(make_metrics(), backup)
        self.assertEqual(status, "WARNING")
        self.assertTrue(warnings[0].startswith("Latest backup status is FAILED."))

    def test_non_success_backup_statuses_warn(self):
        for code in ("LOG_MISSING", "LOG_EMPTY", "LOG_UNREADABLE", "UNKNOWN"):
            with self.subTest(code=code):
                backup = {"status": code, "timestamp": None, "detail": ""}
                status, _ = monitor.evaluate_health(make_metrics(), backup)
                self.assertEqual(status, "WARNING")

    def test_missing_metric_is_never_reported_healthy(self):
        status, warnings = monitor.evaluate_health(
            make_metrics(memory=None, disk=None), SUCCESS_BACKUP
        )
        self.assertEqual(status, "WARNING")
        self.assertEqual(len(warnings), 2)

    def test_missing_cpu_is_never_reported_healthy(self):
        status, warnings = monitor.evaluate_health(make_metrics(cpu=None), SUCCESS_BACKUP)
        self.assertEqual(status, "WARNING")
        self.assertIn("CPU usage could not be collected.", warnings)

    def test_custom_thresholds(self):
        status, _ = monitor.evaluate_health(
            make_metrics(disk=75.0), SUCCESS_BACKUP, disk_threshold=70
        )
        self.assertEqual(status, "WARNING")

    def test_stale_backup_warns_only_when_enabled(self):
        now = datetime(2026, 10, 13, 12, 0)  # 37 hours after the backup
        status, _ = monitor.evaluate_health(make_metrics(), SUCCESS_BACKUP, now=now)
        self.assertEqual(status, "OK")
        status, warnings = monitor.evaluate_health(
            make_metrics(), SUCCESS_BACKUP, max_backup_age_hours=26, now=now
        )
        self.assertEqual(status, "WARNING")
        self.assertIn("37.0 hours old", warnings[0])


class MetricCollectionTests(unittest.TestCase):

    def fake_memory(self):
        return mock.Mock(percent=67.34, available=4 * 1024 ** 3)

    def fake_disk(self):
        return mock.Mock(percent=74.25, free=50 * 1024 ** 3)

    def test_metrics_collected_and_rounded(self):
        with mock.patch.object(monitor.psutil, "cpu_percent", return_value=22.54) as cpu, \
             mock.patch.object(monitor.psutil, "virtual_memory", return_value=self.fake_memory()), \
             mock.patch.object(monitor.psutil, "disk_usage", return_value=self.fake_disk()):
            metrics = monitor.collect_system_metrics("/", cpu_interval=0.5)
        cpu.assert_called_once_with(interval=0.5)  # a real sampling interval
        self.assertEqual(metrics["cpu_usage"], 22.5)
        self.assertEqual(metrics["memory_usage"], 67.3)
        self.assertEqual(metrics["disk_usage"], 74.2)
        self.assertEqual(metrics["errors"], [])
        self.assertIsNotNone(metrics["timestamp"].tzinfo)

    def test_disk_failure_recorded_and_others_still_collected(self):
        with mock.patch.object(monitor.psutil, "cpu_percent", return_value=10.0), \
             mock.patch.object(monitor.psutil, "virtual_memory", return_value=self.fake_memory()), \
             mock.patch.object(monitor.psutil, "disk_usage",
                               side_effect=OSError("drive not ready")), \
             redirect_stderr(io.StringIO()):
            metrics = monitor.collect_system_metrics("Z:\\", cpu_interval=0.1)
        self.assertIsNone(metrics["disk_usage"])
        self.assertEqual(metrics["memory_usage"], 67.3)
        self.assertEqual(len(metrics["errors"]), 1)
        self.assertIn("drive not ready", metrics["errors"][0])


class DatabaseTests(TempDirTestCase):

    def record(self, overall="OK", warnings=""):
        return {
            "timestamp": "2026-10-11T23:15:42+00:00",
            "hostname": "TEST-SERVER-01",
            "cpu_usage": 22.5,
            "memory_usage": 67.3,
            "disk_usage": 74.2,
            "backup_status": "SUCCESS",
            "overall_status": overall,
            "warning_details": warnings,
        }

    def test_insert_and_retrieve(self):
        db = self.tmp / "test.db"
        monitor.init_database(db)
        monitor.save_health_check(db, self.record())
        monitor.save_health_check(db, self.record("WARNING", "Disk usage is 91.0%"))
        rows = monitor.get_recent_checks(db, 10)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["overall_status"], "WARNING")  # newest first
        self.assertEqual(rows[1]["memory_usage"], 67.3)

    def test_history_limit(self):
        db = self.tmp / "test.db"
        monitor.init_database(db)
        for _ in range(5):
            monitor.save_health_check(db, self.record())
        self.assertEqual(len(monitor.get_recent_checks(db, 3)), 3)

    def test_init_is_safe_to_repeat(self):
        db = self.tmp / "test.db"
        monitor.init_database(db)
        monitor.save_health_check(db, self.record())
        monitor.init_database(db)  # must not drop or recreate the table
        self.assertEqual(len(monitor.get_recent_checks(db, 10)), 1)

    def test_parameterised_insert_stores_quotes_safely(self):
        db = self.tmp / "test.db"
        monitor.init_database(db)
        tricky = "x'); DROP TABLE health_checks; --"
        monitor.save_health_check(db, self.record(warnings=tricky))
        self.assertEqual(monitor.get_recent_checks(db, 1)[0]["warning_details"], tricky)


class CommandLineTests(TempDirTestCase):

    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = monitor.main(argv)
        return code, out.getvalue()

    def test_invalid_history_value_rejected(self):
        for bad in ("0", "-5", "abc", "5000"):
            with self.subTest(value=bad), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as ctx:
                    monitor.parse_arguments(["--history", bad])
                self.assertEqual(ctx.exception.code, 2)

    def test_invalid_disk_path_rejected(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
            monitor.parse_arguments(["--disk-path", str(self.tmp / "missing")])
        self.assertEqual(ctx.exception.code, 2)

    def test_full_run_ok_then_warning_then_history(self):
        db = self.tmp / "health.db"
        log = self.write_log("2026-10-11 23:00:00 SUCCESS Daily backup completed\n")
        args = ["--database", str(db), "--backup-log", str(log), "--disk-path", str(self.tmp)]

        with mock.patch.object(monitor, "collect_system_metrics", return_value=make_metrics()):
            code, output = self.run_main(args)
        self.assertEqual(code, monitor.EXIT_OK)
        self.assertIn("Overall Health : OK", output)
        self.assertIn("Monitoring record saved successfully.", output)

        self.write_log("2026-10-12 23:00:00 FAILED Backup interrupted\n")
        with mock.patch.object(monitor, "collect_system_metrics",
                               return_value=make_metrics(memory=94.2)):
            code, output = self.run_main(args)
        self.assertEqual(code, monitor.EXIT_WARNING)
        self.assertIn("WARNING: Memory usage is 94.2%, exceeding the 90% threshold.", output)
        self.assertIn("WARNING: Latest backup status is FAILED.", output)

        code, output = self.run_main(["--database", str(db), "--history", "5"])
        self.assertEqual(code, monitor.EXIT_OK)
        self.assertIn("FAILED", output)
        self.assertEqual(len(monitor.get_recent_checks(db, 10)), 2)

    def test_database_failure_returns_error_code(self):
        log = self.write_log("2026-10-11 23:00:00 SUCCESS ok\n")
        bad_db = self.tmp / "no_such_folder" / "health.db"
        args = ["--database", str(bad_db), "--backup-log", str(log), "--disk-path", str(self.tmp)]
        with mock.patch.object(monitor, "collect_system_metrics", return_value=make_metrics()):
            code, output = self.run_main(args)
        self.assertEqual(code, monitor.EXIT_ERROR)
        self.assertIn("could NOT be saved", output)

    def test_cpu_collection_failure_gives_warning_exit_code(self):
        # Regression test (v1.0.1): a CPU collection error used to be printed
        # as a warning while the run was still saved and exited as OK.
        db = self.tmp / "health.db"
        log = self.write_log("2026-10-11 23:00:00 SUCCESS ok\n")
        args = ["--database", str(db), "--backup-log", str(log), "--disk-path", str(self.tmp)]
        with mock.patch.object(monitor.psutil, "cpu_percent", side_effect=OSError("sensor gone")), \
             mock.patch.object(monitor.psutil, "virtual_memory",
                               return_value=mock.Mock(percent=50.0, available=4 * 1024 ** 3)), \
             mock.patch.object(monitor.psutil, "disk_usage",
                               return_value=mock.Mock(percent=50.0, free=50 * 1024 ** 3)):
            code, output = self.run_main(args)
        self.assertEqual(code, monitor.EXIT_WARNING)
        self.assertIn("Overall Health : WARNING", output)
        self.assertEqual(monitor.get_recent_checks(db, 1)[0]["overall_status"], "WARNING")

    def test_missing_psutil_returns_error_code(self):
        with mock.patch.object(monitor, "psutil", None):
            code, _ = self.run_main(["--disk-path", str(self.tmp)])
        self.assertEqual(code, monitor.EXIT_ERROR)

    def test_history_without_database(self):
        code, output = self.run_main(["--database", str(self.tmp / "none.db"), "--history", "3"])
        self.assertEqual(code, monitor.EXIT_OK)
        self.assertIn("No monitoring records found", output)
        self.assertFalse((self.tmp / "none.db").exists())


if __name__ == "__main__":
    unittest.main()
