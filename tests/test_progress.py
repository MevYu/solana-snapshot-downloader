"""Progress display checks use memory streams and never download files."""

from contextlib import redirect_stderr, redirect_stdout
import io
import os
import threading
import unittest
from unittest.mock import patch

from downloader.progress import ProgressDisplay


class Terminal(io.StringIO):
    def isatty(self):
        return True


class HeartbeatStream(Terminal):
    def __init__(self):
        super().__init__()
        self.heartbeat = threading.Event()

    def write(self, text):
        result = super().write(text)
        if "elapsed 0:01" in text:
            self.heartbeat.set()
        return result


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {"TERM": "xterm"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        width = patch("downloader.progress.shutil.get_terminal_size",
                      return_value=os.terminal_size((160, 24)))
        self.width = width.start()
        self.addCleanup(width.stop)

    def test_terminal_bar_has_stage_and_counts(self):
        stream = Terminal()
        with ProgressDisplay(stream, interval=60) as progress:
            progress.start("[1/3] Search", total=10)
            progress.update(3, detail="2 candidates")
            progress.finish("2 candidates found")
        output = stream.getvalue()
        self.assertIn("\r\033[2K", output)
        self.assertIn("[1/3] Search [###-------] 3/10 nodes", output)
        self.assertIn("2 candidates found", output)
        self.assertTrue(output.endswith("\n"))

    def test_default_stream_is_runtime_stderr_and_plain_logs_are_throttled(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("downloader.progress.time.monotonic", return_value=100):
            with redirect_stdout(stdout), redirect_stderr(stderr), ProgressDisplay(interval=60) as progress:
                progress.start("[1/3] Search", total=100)
                for completed in range(1, 100):
                    progress.update(completed)
                progress.finish("ready")
        lines = stderr.getvalue().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertIn("0/100 nodes", lines[0])
        self.assertIn("99/100 nodes", lines[1])
        self.assertEqual(lines[2], "ready")
        self.assertEqual(stdout.getvalue(), "")
        self.assertNotIn("\r", stderr.getvalue())
        self.assertNotIn("\033", stderr.getvalue())

    def test_redirected_logs_refresh_only_after_log_interval(self):
        stream = io.StringIO()
        with patch("downloader.progress.time.monotonic", return_value=100) as clock:
            with ProgressDisplay(stream, interval=60, log_interval=5) as progress:
                progress.start("Search", total=10)
                clock.return_value = 104
                progress.update(1)
                self.assertEqual(len(stream.getvalue().splitlines()), 1)
                clock.return_value = 105
                progress.update(2)
                self.assertEqual(len(stream.getvalue().splitlines()), 2)
                progress.finish()
        self.assertEqual(len(stream.getvalue().splitlines()), 3)

    def test_unknown_to_known_total_is_immediately_visible_in_logs(self):
        stream = io.StringIO()
        with ProgressDisplay(stream, interval=60) as progress:
            progress.start("Search", detail="waiting for RPC")
            progress.update(0, total=20, detail="0 usable, 0 skipped")
            self.assertEqual(len(stream.getvalue().splitlines()), 2)
            self.assertIn("0/20 nodes", stream.getvalue().splitlines()[-1])

    def test_changed_terminal_detail_is_visible_before_a_blocking_call(self):
        stream = Terminal()
        with ProgressDisplay(stream, interval=60) as progress:
            progress.start("Speed", total=2, detail="first node")
            progress.update(1, detail="second node")
            self.assertIn("second node", stream.getvalue())

    def test_idle_ticker_refreshes_waiting_stage_and_elapsed(self):
        stream = HeartbeatStream()
        with patch("downloader.progress.time.monotonic", return_value=100) as clock:
            with ProgressDisplay(stream, interval=0.01) as progress:
                progress.start("[1/3] Search", detail="waiting for RPC")
                clock.return_value = 101
                self.assertTrue(stream.heartbeat.wait(timeout=1), "idle stage did not refresh")
        self.assertIn("waiting for RPC", stream.getvalue())
        self.assertIn("elapsed 0:01", stream.getvalue())

    def test_idle_ticker_also_refreshes_redirected_logs_without_controls(self):
        stream = HeartbeatStream()
        with patch.object(stream, "isatty", return_value=False):
            with patch("downloader.progress.time.monotonic", return_value=100) as clock:
                with ProgressDisplay(stream, interval=0.01, log_interval=0.1) as progress:
                    progress.start("Search", detail="waiting for RPC")
                    clock.return_value = 101
                    self.assertTrue(stream.heartbeat.wait(timeout=1), "redirected idle stage did not refresh")
                    self.assertEqual(len(stream.getvalue().splitlines()), 2)
        self.assertNotIn("\r", stream.getvalue())
        self.assertNotIn("\033", stream.getvalue())

    def test_context_stops_the_ticker_on_error_and_keyboard_interrupt(self):
        for error in (RuntimeError("network failed"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                stream = Terminal()
                progress = ProgressDisplay(stream, interval=0.01)
                with self.assertRaises(type(error)):
                    with progress:
                        progress.start("Search", detail="waiting")
                        thread = progress._thread
                        self.assertTrue(thread.daemon)
                        raise error
                self.assertFalse(thread.is_alive())
                self.assertTrue(stream.getvalue().endswith("\n"))
                self.assertNotIn("Done", stream.getvalue())

    def test_bytes_show_shared_binary_units_speed_and_eta(self):
        stream = Terminal()
        mib = 1024 * 1024
        with patch("downloader.progress.time.monotonic", return_value=100) as clock:
            with ProgressDisplay(stream, interval=0.1) as progress:
                progress.start("[3/3] Full", total=4 * mib, unit="bytes")
                clock.return_value = 102
                progress.update(mib, speed=mib)
                self.assertIn("25% 1.0/4.0 MiB 1.0 MiB/s ETA 0:03", stream.getvalue())
                self.assertNotIn("elapsed", stream.getvalue())

    def test_full_byte_count_does_not_claim_success_before_finish(self):
        stream = Terminal()
        with patch("downloader.progress.time.monotonic", return_value=100) as clock:
            with ProgressDisplay(stream, interval=0.1) as progress:
                progress.start("Full", total=1024, unit="bytes")
                clock.return_value = 101
                progress.update(1024, detail="verifying", speed=1024)
                self.assertIn("100% 1.0/1.0 KiB", stream.getvalue())
                self.assertIn("verifying", stream.getvalue())
                self.assertNotIn("Done", stream.getvalue())
                self.assertFalse(stream.getvalue().endswith("\n"))
                progress.finish("saved")
                self.assertTrue(stream.getvalue().endswith("saved\n"))

    def test_message_clears_then_redraws_the_current_terminal_stage(self):
        stream = Terminal()
        with ProgressDisplay(stream, interval=60) as progress:
            progress.start("Search", total=10)
            progress.update(2)
            progress.message("Retrying node")
            output = stream.getvalue()
            self.assertIn("\r\033[2KRetrying node\n\r\033[2KSearch", output)
            self.assertIn("2/10 nodes", output)

    def test_stage_transition_preserves_both_labels_and_messages(self):
        stream = io.StringIO()
        with ProgressDisplay(stream, interval=60) as progress:
            progress.start("[1/3] Search", total=1)
            progress.update(1, detail="1 candidate")
            progress.start("[2/3] Speed", total=1)
            progress.finish("skipped")
        lines = stream.getvalue().splitlines()
        self.assertIn("[1/3] Search", lines[1])
        self.assertIn("1 candidate", lines[1])
        self.assertIn("[2/3] Speed", lines[2])
        self.assertIn("[2/3] Speed", lines[3])
        self.assertEqual(lines[4], "skipped")

    def test_narrow_terminal_never_uses_the_last_column(self):
        stream = Terminal()
        self.width.return_value = os.terminal_size((24, 24))
        with ProgressDisplay(stream, interval=60) as progress:
            progress.start("[1/3] Search", total=1000, detail="a very long hostname" * 10)
            progress.message("retrying")
        rendered = stream.getvalue().replace("\033[2K", "").replace("\n", "\r").split("\r")
        self.assertTrue(all(len(line) <= 23 for line in rendered), rendered)

    def test_eighty_columns_keep_download_stats_before_long_filename(self):
        stream = Terminal()
        self.width.return_value = os.terminal_size((80, 24))
        gib = 1024 ** 3
        with ProgressDisplay(stream, interval=60) as progress:
            progress.start("[3/3] Full", total=20 * gib, unit="bytes")
            progress.update(10 * gib, speed=100 * 1024 ** 2, detail="snapshot-" + "A" * 100)
            last = stream.getvalue().rsplit("\r\033[2K", 1)[-1]
            self.assertIn("50% 10.0/20.0 GiB", last)
            self.assertIn("100.0 MiB/s ETA 1:43", last)
            self.assertLessEqual(len(last), 79)

    def test_eighty_columns_keep_full_and_incremental_final_status_visible(self):
        self.width.return_value = os.terminal_size((80, 24))
        gib = 1024 ** 3
        for kind in ("Full", "Incremental"):
            for status in ("ready", "failed"):
                with self.subTest(kind=kind, status=status):
                    stream = Terminal()
                    with ProgressDisplay(stream, interval=60) as progress:
                        progress.start("[4/4] " + kind, total=100 * gib, unit="bytes")
                        progress.update(100 * gib, speed=100 * 1024 ** 2,
                                        detail="Finalizing archive " + "A" * 100)
                        self.assertNotIn(kind + " " + status, stream.getvalue())
                        progress.finish(kind + " " + status)
                    final_bar, message = stream.getvalue().splitlines()[-2:]
                    self.assertEqual(message, kind + " " + status)
                    self.assertIn("100% 100.0/100.0 GiB", final_bar)
                    self.assertIn("100.0 MiB/s ETA 0:00", final_bar)
                    self.assertLessEqual(len(final_bar.replace("\033[2K", "")), 79)

    def test_redirected_finish_emits_one_summary_without_repeated_updates(self):
        stream = io.StringIO()
        with patch("downloader.progress.time.monotonic", return_value=100):
            with ProgressDisplay(stream, interval=60) as progress:
                progress.start("Full", total=1000, unit="bytes")
                for completed in range(1001):
                    progress.update(completed, detail="receiving")
                progress.finish("Full failed")
                progress.finish("Full failed")
        lines = stream.getvalue().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertIn("100% 1000/1000 B", lines[1])
        self.assertEqual(lines[-1], "Full failed")

    def test_dumb_terminal_uses_clean_lines_and_control_characters_are_removed(self):
        stream = Terminal()
        with patch.dict(os.environ, {"TERM": "dumb"}), ProgressDisplay(stream, interval=60) as progress:
            progress.start("Search\nnext", detail="waiting\rfor\033RPC")
            progress.message("retry\nnode")
            progress.finish()
        output = stream.getvalue()
        self.assertNotIn("\r", output)
        self.assertNotIn("\033", output)
        self.assertIn("Search next", output)
        self.assertIn("retry node\n", output)


if __name__ == "__main__":
    unittest.main()
