"""End-to-end CLI tests against local HTTP servers; no public network required."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from downloader.cli import download_candidate, output_lock, parser
from downloader.discovery import DiscoveryError
from downloader.download import DownloadError
from downloader.models import Candidate, snapshot_from_url


ROOT = Path(__file__).resolve().parents[1]
HASH = "A" * 44
FULL = "snapshot-1000-" + HASH + ".tar.zst"
INC = "incremental-snapshot-1000-1100-" + HASH + ".tar.zst"
OTHER = "incremental-snapshot-2000-2100-" + HASH + ".tar.zst"
DATA = {FULL: b"full test archive" * 8192, INC: b"incremental test archive" * 1024}


@contextmanager
def fixture(fail=False, rollover=False, stale=False):
    state = {"gets": [], "rpc": 0, "incremental_heads": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["rpc"] += 1
            result = 9000 if stale and state["rpc"] > 1 else 1105
            payload = json.dumps({"jsonrpc": "2.0", "id": data["id"], "result": result}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_HEAD(self):
            self.serve(False)

        def do_GET(self):
            self.serve(True)

        def serve(self, body):
            name = self.path.lstrip("/")
            if name == "snapshot.tar.zst":
                target = FULL
            elif name == "incremental-snapshot.tar.zst":
                state["incremental_heads"] += 1
                target = OTHER if rollover and state["incremental_heads"] > 1 else INC
            else:
                target = None
            if target:
                self.send_response(302)
                # A complete Location, as in the user's failing legacy log.
                self.send_header("Location", "http://127.0.0.1:%d/%s" % (self.server.server_port, target))
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            payload = DATA.get(name, b"new base incremental")
            if body:
                state["gets"].append(name)
                if fail:
                    self.send_error(503)
                    return
            offset, end = 0, len(payload) - 1
            range_value = self.headers.get("Range") if body else None
            if range_value:
                first, last = range_value.removeprefix("bytes=").split("-", 1)
                offset = int(first)
                end = min(int(last) if last else end, end)
            self.send_response(206 if range_value else 200)
            if range_value:
                self.send_header("Content-Range", "bytes %d-%d/%d" % (offset, end, len(payload)))
            payload = payload[offset:end + 1]
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("ETag", '"test-archive"')
            self.end_headers()
            if body:
                self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d" % server.server_port, state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class CLITests(unittest.TestCase):
    def command(self, node, output, *args):
        return subprocess.run(
            [sys.executable, str(ROOT / "snapshot_downloader.py"), "--node", node,
             "--rpc", node + "/rpc", "--output", str(output), "--attempts", "1",
             "--retries", "0", "--retry-delay", "0", "--speed-test-count", "0",
             "--timeout", "1", *args], text=True, capture_output=True, timeout=15,
        )

    def test_download_pair_and_reuse_receipts(self):
        with tempfile.TemporaryDirectory() as temp, fixture() as (url, state):
            output = Path(temp) / "snapshots"
            first = self.command(url, output)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertIn("Done.", first.stderr)
            for filename in (FULL, INC):
                self.assertEqual((output / filename).read_bytes(), DATA[filename])
                self.assertTrue((output / (filename + ".complete.json")).is_file())
                self.assertFalse((output / (filename + ".part")).exists())
            self.assertEqual(state["gets"], [FULL, INC])
            second = self.command(url, output)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(state["gets"], [FULL, INC])
            self.assertEqual((output / ".snapshot-downloader.lock").read_text(), "")

    def test_list_json_has_no_output_directory_or_archive_gets(self):
        with tempfile.TemporaryDirectory() as temp, fixture() as (url, state):
            output = Path(temp) / "not-created"
            result = self.command(url, output, "--list", "--json")
            self.assertEqual(result.returncode, 0, result.stderr)
            listing = json.loads(result.stdout)
            self.assertEqual(listing["candidates"][0]["incremental"]["base_slot"], 1000)
            self.assertFalse(output.exists())
            self.assertEqual(state["gets"], [])

    def test_http_failure_is_nonzero_and_never_done(self):
        with tempfile.TemporaryDirectory() as temp, fixture(fail=True) as (url, _):
            result = self.command(url, Path(temp))
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertNotIn("Done.", result.stderr)
            self.assertFalse((Path(temp) / FULL).exists())
            self.assertFalse((Path(temp) / INC).exists())

    def test_speed_sampling_and_download_work_together(self):
        with tempfile.TemporaryDirectory() as temp, fixture() as (url, state):
            result = self.command(url, Path(temp), "--speed-test-count", "1")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Sample", result.stderr)
            self.assertEqual(state["gets"], [FULL, FULL, INC])

    def test_rollover_does_not_mix_another_base_incremental(self):
        with tempfile.TemporaryDirectory() as temp, fixture(rollover=True) as (url, state):
            result = self.command(url, Path(temp))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(state["gets"], [FULL, INC])
            self.assertFalse((Path(temp) / OTHER).exists())

    def test_becoming_stale_after_full_fails_without_downloading_incremental(self):
        with tempfile.TemporaryDirectory() as temp, fixture(stale=True) as (url, state):
            result = self.command(url, Path(temp))
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertNotIn("Done.", result.stderr)
            self.assertEqual(state["gets"], [FULL])

    def test_becoming_stale_during_incremental_is_not_success(self):
        candidate = Candidate("http://node/", snapshot_from_url("http://node/" + FULL),
                              snapshot_from_url("http://node/" + INC))
        args = parser().parse_args([])
        args.output = Path("unused")
        from unittest.mock import Mock
        downloader = Mock()
        downloader.download.side_effect = [Path(FULL), Path(INC)]
        with patch("downloader.cli.probe_incremental", return_value=candidate.incremental), \
                patch("downloader.cli.get_current_slot", side_effect=[1105, 9000]):
            with self.assertRaises(DiscoveryError):
                download_candidate(candidate, downloader, args, ["http://rpc/"])

    def test_output_lock_prevents_two_downloaders(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            with output_lock(path):
                with self.assertRaises(DownloadError):
                    with output_lock(path):
                        self.fail("second lock acquired")
            with output_lock(path):
                pass

    def test_cli_rejects_invalid_arguments(self):
        for args in (("--timeout", "nan"), ("--min-speed", "inf"), ("--workers", "129"),
                     ("--json",), ("--attempts", "0"), ("--retries", "-1")):
            with self.subTest(args=args):
                result = subprocess.run([sys.executable, str(ROOT / "snapshot_downloader.py"), *args],
                                        text=True, capture_output=True, timeout=5)
                self.assertEqual(result.returncode, 2, result.stderr)


if __name__ == "__main__":
    unittest.main()
