"""Real local HTTP exchanges for the download engine; no network dependencies."""

from contextlib import contextmanager
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from solana_snapshot_downloader.download import DownloadError, DownloadOptions, Downloader
from solana_snapshot_downloader.models import snapshot_from_url


NAME = "snapshot-123-" + "A" * 32 + ".tar.zst"
OTHER_NAME = "snapshot-124-" + "B" * 32 + ".tar.zst"
DATA = bytes(range(256)) * 64


@contextmanager
def server(callback):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            self.close_connection = True
            callback(self)

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:{}/".format(httpd.server_port)
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def reply(handler, body=DATA, status=200, *, length=None, etag='"v1"', headers=None, cut=None, date=None):
    handler.send_response_only(status)
    if date is not False:
        handler.send_header("Date", date if date is not None else formatdate(usegmt=True))
    if length is not False:
        handler.send_header("Content-Length", str(len(body) if length is None else length))
    if etag is not None:
        handler.send_header("ETag", etag)
    for name, value in (headers or {}).items():
        handler.send_header(name, value)
    handler.send_header("Connection", "close")
    handler.end_headers()
    if body:
        handler.wfile.write(body if cut is None else body[:cut])
        handler.wfile.flush()
    if cut is not None:
        handler.connection.shutdown(socket.SHUT_WR)


def range_reply(handler, body=DATA, *, etag='"v1"', headers=None, date=None):
    offset = int(handler.headers["Range"].removeprefix("bytes=").removesuffix("-"))
    if offset >= len(body):
        extra = {"Content-Range": "bytes */{}".format(len(body))}
        extra.update(headers or {})
        reply(handler, b"", 416, etag=etag, headers=extra, date=date)
    else:
        extra = {"Content-Range": "bytes {}-{}/{}".format(offset, len(body) - 1, len(body))}
        extra.update(headers or {})
        reply(handler, body[offset:], 206, etag=etag, headers=extra, date=date)


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name)
        self.options = DownloadOptions(retries=0, retry_delay=0, chunk_size=1024, timeout=1)

    @property
    def partial(self):
        return self.output / (NAME + ".part")

    @property
    def metadata(self):
        return self.output / (NAME + ".part.json")

    @property
    def final(self):
        return self.output / NAME

    def download(self, url, **kwargs):
        options = kwargs.pop("options", self.options)
        return Downloader(options, **kwargs).download(snapshot_from_url(url), self.output)

    def interrupt(self, url, count=4096):
        def progress(snapshot, done, total, speed):
            if done >= count:
                raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.download(url, progress=progress)
        self.assertEqual(self.partial.stat().st_size, count)
        self.assertFalse(self.final.exists())

    def test_complete_download_progress_and_receipt_reuse(self):
        requests = []

        def handler(request):
            requests.append(request.headers)
            reply(request)

        with server(handler) as base:
            updates = []
            url = base + NAME
            path = self.download(url, progress=lambda *args: updates.append(args))
            self.assertEqual(path.read_bytes(), DATA)
            self.assertFalse(self.partial.exists())
            self.assertFalse(self.metadata.exists())
            self.assertTrue((self.output / (NAME + ".complete.json")).is_file())
            self.assertEqual(self.download(url), path)
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0]["Accept-Encoding"], "identity")
            self.assertEqual(updates[-1][1:3], (len(DATA), len(DATA)))

    def test_truncation_resumes_with_strong_etag(self):
        ranges = []

        def handler(request):
            ranges.append((request.headers.get("Range"), request.headers.get("If-Range")))
            if len(ranges) == 1:
                reply(request, cut=4096)
            else:
                range_reply(request)

        options = DownloadOptions(retries=1, retry_delay=0, chunk_size=1024, timeout=1)
        with server(handler) as base:
            self.assertEqual(self.download(base + NAME, options=options).read_bytes(), DATA)
        self.assertEqual(ranges, [(None, None), ("bytes=4096-", '"v1"')])

    def test_keyboard_interrupt_preserves_resumable_partial(self):
        ranges = []

        def handler(request):
            ranges.append(request.headers.get("Range"))
            if request.headers.get("Range"):
                range_reply(request)
            else:
                reply(request)

        with server(handler) as base:
            url = base + NAME
            self.interrupt(url)
            self.assertEqual(self.download(url).read_bytes(), DATA)
        self.assertEqual(ranges, [None, "bytes=4096-"])

    def test_ignored_range_restarts_without_appending(self):
        ranges = []

        def handler(request):
            ranges.append(request.headers.get("Range"))
            reply(request)

        with server(handler) as base:
            url = base + NAME
            self.interrupt(url)
            self.assertEqual(self.download(url).read_bytes(), DATA)
        self.assertEqual(ranges, [None, "bytes=4096-"])

    def test_changed_etag_on_partial_response_forces_new_full_request(self):
        ranges = []
        changed = b"Z" * len(DATA)

        def handler(request):
            ranges.append(request.headers.get("Range"))
            if len(ranges) == 1:
                reply(request)
            elif request.headers.get("Range"):
                range_reply(request, changed, etag='"v2"')
            else:
                reply(request, changed, etag='"v2"')

        with server(handler) as base:
            url = base + NAME
            self.interrupt(url)
            options = DownloadOptions(retries=1, retry_delay=0, chunk_size=1024)
            self.assertEqual(self.download(url, options=options).read_bytes(), changed)
        self.assertEqual(ranges, [None, "bytes=4096-", None])

    def test_wrong_content_range_fails_and_discards_partial(self):
        requests = 0

        def handler(request):
            nonlocal requests
            requests += 1
            if requests == 1:
                reply(request)
            else:
                range_reply(request, headers={"Content-Range": "bytes 0-12287/16384"})

        with server(handler) as base:
            url = base + NAME
            self.interrupt(url)
            with self.assertRaisesRegex(DownloadError, "Content-Range"):
                self.download(url)
        self.assertFalse(self.final.exists())
        self.assertFalse(self.partial.exists())

    def test_inconsistent_resume_headers_never_publish(self):
        cases = [
            {"headers": {"Content-Range": "bytes 4096-16383/16385"}},
            {"length": len(DATA) - 4097},
            {"etag": None},
            {"headers": {"Content-Range": "bytes " + "1" * 5000 + "-2/3"}},
        ]
        for kwargs in cases:
            with self.subTest(response=kwargs):
                def handler(request):
                    if request.headers.get("Range"):
                        extra = {"Content-Range": "bytes 4096-16383/16384"}
                        extra.update(kwargs.get("headers", {}))
                        reply(request, DATA[4096:], 206,
                              etag=kwargs.get("etag", '"v1"'),
                              length=kwargs.get("length"), headers=extra)
                    else:
                        reply(request)

                with server(handler) as base:
                    url = base + NAME
                    self.interrupt(url)
                    with self.assertRaises(DownloadError):
                        self.download(url)
                self.assertFalse(self.final.exists())
                self.assertFalse(self.partial.exists())

    def test_source_url_change_never_reuses_other_nodes_bytes(self):
        seen = []

        def handler(request):
            seen.append(request.headers.get("Range"))
            reply(request, b"Z" * len(DATA) if request.path.startswith("/second/") else DATA)

        with server(handler) as base:
            self.interrupt(base + NAME)
            result = self.download(base + "second/" + NAME)
            self.assertEqual(result.read_bytes(), b"Z" * len(DATA))
        self.assertEqual(seen, [None, None])

    def test_changed_resolved_url_forces_fresh_download(self):
        resolved_version = ["one"]
        requests = []

        def handler(request):
            if request.path == "/" + NAME:
                reply(request, b"", 302, headers={"Location": "/{}/{}".format(resolved_version[0], NAME)})
            else:
                requests.append((request.path, request.headers.get("Range")))
                body = DATA if request.path.startswith("/one/") else b"Z" * len(DATA)
                if request.headers.get("Range"):
                    range_reply(request, body)
                else:
                    reply(request, body)

        with server(handler) as base:
            url = base + NAME
            self.interrupt(url)
            resolved_version[0] = "two"
            options = DownloadOptions(retries=1, retry_delay=0, chunk_size=1024)
            self.assertEqual(self.download(url, options=options).read_bytes(), b"Z" * len(DATA))
        self.assertEqual([r[1] for r in requests], [None, "bytes=4096-", None])

    def test_absolute_redirect_is_followed_and_identity_is_checked(self):
        with server(lambda request: reply(request)) as destination:
            with server(lambda request: reply(request, b"", 302,
                                             headers={"Location": destination + NAME})) as source:
                self.assertEqual(self.download(source + NAME).read_bytes(), DATA)

    def test_redirect_to_different_archive_is_rejected(self):
        def handler(request):
            if request.path == "/" + NAME:
                reply(request, b"", 302, headers={"Location": "/" + OTHER_NAME})
            else:
                reply(request)

        with server(handler) as base:
            with self.assertRaisesRegex(DownloadError, "identity"):
                self.download(base + NAME)
        self.assertFalse(self.final.exists())

    def test_missing_and_weak_validators_restart_from_zero(self):
        for etag in (None, 'W/"weak"'):
            with self.subTest(etag=etag):
                seen = []

                def handler(request):
                    seen.append(request.headers.get("Range"))
                    reply(request, etag=etag)

                with server(handler) as base:
                    url = base + NAME
                    self.interrupt(url)
                    self.assertEqual(self.download(url).read_bytes(), DATA)
                self.assertEqual(seen, [None, None])
                self.final.unlink()
                (self.output / (NAME + ".complete.json")).unlink()

    def test_last_modified_and_size_allow_resume(self):
        modified = "Wed, 21 Oct 2015 07:28:00 GMT"
        dated = "Wed, 21 Oct 2015 07:29:00 GMT"
        seen = []

        def handler(request):
            seen.append(request.headers.get("If-Range"))
            if request.headers.get("Range"):
                range_reply(request, etag=None, headers={"Last-Modified": modified}, date=dated)
            else:
                reply(request, etag=None, headers={"Last-Modified": modified}, date=dated)

        with server(handler) as base:
            url = base + NAME
            self.interrupt(url)
            self.assertEqual(json.loads(self.metadata.read_text())["validator_date"], dated)
            self.assertEqual(self.download(url).read_bytes(), DATA)
        self.assertEqual(seen, [None, modified])

    def test_unproven_or_weak_last_modified_never_resumes(self):
        modified = "Wed, 21 Oct 2015 07:28:00 GMT"
        dated = "Wed, 21 Oct 2015 07:29:00 GMT"
        for date, etag in ((modified, None), (False, None), (dated, 'W/"weak"')):
            with self.subTest(date=date, etag=etag):
                seen = []

                def handler(request):
                    seen.append(request.headers.get("Range"))
                    reply(request, etag=etag, headers={"Last-Modified": modified}, date=date)

                with server(handler) as base:
                    url = base + NAME
                    self.interrupt(url)
                    self.assertEqual(self.download(url).read_bytes(), DATA)
                self.assertEqual(seen, [None, None])
                self.final.unlink()
                (self.output / (NAME + ".complete.json")).unlink()

    def test_last_modified_metadata_without_original_date_restarts(self):
        seen = []

        def handler(request):
            seen.append(request.headers.get("Range"))
            reply(request, etag=None,
                  headers={"Last-Modified": "Wed, 21 Oct 2015 07:28:00 GMT"},
                  date="Wed, 21 Oct 2015 07:29:00 GMT")

        with server(handler) as base:
            url = base + NAME
            self.interrupt(url)
            metadata = json.loads(self.metadata.read_text())
            metadata.pop("validator_date")
            self.metadata.write_text(json.dumps(metadata))
            self.assertEqual(self.download(url).read_bytes(), DATA)
        self.assertEqual(seen, [None, None])

    def test_complete_partial_requires_confirmed_416(self):
        seen = []

        def handler(request):
            seen.append(request.headers.get("Range"))
            if request.headers.get("Range"):
                range_reply(request)
            else:
                reply(request)

        with server(handler) as base:
            url = base + NAME
            self.interrupt(url, len(DATA))
            self.assertEqual(self.download(url).read_bytes(), DATA)
        self.assertEqual(seen, [None, "bytes={}-".format(len(DATA))])

    def test_untrustworthy_416_does_not_finalize_partial(self):
        def handler(request):
            if request.headers.get("Range"):
                reply(request, b"", 416, etag=None,
                      headers={"Content-Range": "bytes */{}".format(len(DATA))})
            else:
                reply(request)

        with server(handler) as base:
            url = base + NAME
            self.interrupt(url, len(DATA))
            with self.assertRaisesRegex(DownloadError, "416"):
                self.download(url)
        self.assertFalse(self.final.exists())
        self.assertFalse(self.partial.exists())

    def test_unknown_length_empty_encoding_and_server_errors_never_publish(self):
        responses = [
            {"length": False}, {"body": b""},
            {"headers": {"Content-Encoding": "gzip"}},
            {"headers": {"Content-Length": str(len(DATA) + 1)}},
            {"headers": {"ETag": '"conflicting"'}},
            {"body": b"error", "status": 503}, {"cut": 4096},
        ]
        for kwargs in responses:
            with self.subTest(response=kwargs):
                with server(lambda request: reply(request, **kwargs)) as base:
                    with self.assertRaises(DownloadError):
                        self.download(base + NAME)
                self.assertFalse(self.final.exists())
                self.assertFalse((self.output / (NAME + ".complete.json")).exists())

    def test_html_and_json_error_responses_never_publish(self):
        cases = [
            (b"archive?", {"Content-Type": "text/html; charset=utf-8"}),
            (b"archive?", {"Content-Type": "application/xhtml+xml"}),
            (b"archive?", {"Content-Type": "application/problem+json"}),
            (b"  <!DOCTYPE html><html>upstream failed</html>", {}),
            (b'<html><body>upstream failed</body></html>', {"Content-Type": "application/octet-stream"}),
            (b'{"error":"upstream failed"}', {}),
        ]
        options = DownloadOptions(retries=0, retry_delay=0, chunk_size=1, timeout=1)
        for body, headers in cases:
            with self.subTest(body=body, headers=headers):
                with server(lambda request: reply(request, body, headers=headers)) as base:
                    with self.assertRaisesRegex(DownloadError, "HTML or JSON"):
                        self.download(base + NAME, options=options)
                self.assertFalse(self.final.exists())
                self.assertFalse(self.partial.exists())

    def test_resume_does_not_sniff_arbitrary_middle_of_archive(self):
        body = DATA[:4096] + b"<html>compressed middle</html>" + DATA[4096:]

        def handler(request):
            if request.headers.get("Range"):
                range_reply(request, body)
            else:
                reply(request, body)

        with server(handler) as base:
            url = base + NAME
            self.interrupt(url)
            self.assertEqual(self.download(url).read_bytes(), body)

    def test_error_prefix_split_across_interrupted_resume_is_rejected(self):
        body = b"<html><body>proxy error</body></html>"

        def handler(request):
            if request.headers.get("Range"):
                range_reply(request, body)
            else:
                reply(request, body)

        options = DownloadOptions(retries=0, retry_delay=0, chunk_size=1, timeout=1)
        with server(handler) as base:
            url = base + NAME

            def progress(snapshot, done, total, speed):
                if done == 2:
                    raise KeyboardInterrupt

            with self.assertRaises(KeyboardInterrupt):
                self.download(url, options=options, progress=progress)
            with self.assertRaisesRegex(DownloadError, "HTML or JSON"):
                self.download(url, options=options)
        self.assertFalse(self.final.exists())
        self.assertFalse(self.partial.exists())

    def test_existing_archive_without_receipt_is_never_overwritten(self):
        self.final.write_bytes(b"existing data")
        with server(lambda request: self.fail("Must not make a request")) as base:
            with self.assertRaisesRegex(DownloadError, "move it aside"):
                self.download(base + NAME)
        self.assertEqual(self.final.read_bytes(), b"existing data")

    def test_archive_appearing_during_download_is_never_overwritten(self):
        def progress(snapshot, done, total, speed):
            if done == total:
                self.final.write_bytes(b"another writer")

        with server(lambda request: reply(request)) as base:
            with self.assertRaisesRegex(DownloadError, "refusing to overwrite"):
                self.download(base + NAME, progress=progress)
        self.assertEqual(self.final.read_bytes(), b"another writer")
        self.assertEqual(self.partial.read_bytes(), DATA)

    def test_receipt_detects_changed_final_file(self):
        with server(lambda request: reply(request)) as base:
            url = base + NAME
            self.download(url)
            self.final.write_bytes(b"changed")
            with self.assertRaisesRegex(DownloadError, "receipt"):
                self.download(url)
        self.assertEqual(self.final.read_bytes(), b"changed")

    def test_symlinks_are_rejected_for_all_managed_paths(self):
        target = self.output / "target"
        target.write_bytes(b"keep")
        for suffix in ("", ".part", ".part.json", ".complete.json"):
            with self.subTest(suffix=suffix):
                path = self.output / (NAME + suffix)
                path.symlink_to(target)
                with server(lambda request: self.fail("Must not make a request")) as base:
                    with self.assertRaisesRegex(DownloadError, "symlink"):
                        self.download(base + NAME)
                self.assertEqual(target.read_bytes(), b"keep")
                path.unlink()

    def test_large_or_malformed_partial_metadata_restarts_safely(self):
        for raw in (b"{" * 20000, b"not JSON", b"[]"):
            with self.subTest(length=len(raw)):
                self.partial.write_bytes(b"bad prefix")
                self.metadata.write_bytes(raw)
                seen = []

                def handler(request):
                    seen.append(request.headers.get("Range"))
                    reply(request)

                with server(handler) as base:
                    self.assertEqual(self.download(base + NAME).read_bytes(), DATA)
                self.assertEqual(seen, [None])
                self.final.unlink()
                (self.output / (NAME + ".complete.json")).unlink()

    def test_disk_preflight_does_not_create_partial(self):
        with server(lambda request: reply(request)) as base:
            with patch("solana_snapshot_downloader.download.shutil.disk_usage") as usage:
                usage.return_value.free = 0
                with self.assertRaisesRegex(DownloadError, "disk space"):
                    self.download(base + NAME)
        self.assertFalse(self.final.exists())
        self.assertFalse(self.partial.exists())

    def test_speed_minimum_raises_without_publication(self):
        options = DownloadOptions(retries=0, retry_delay=0, chunk_size=1024,
                                  min_speed=10**9, speed_window=0.5)
        with server(lambda request: reply(request)) as base:
            with patch("solana_snapshot_downloader.download.time.monotonic", side_effect=[0.0, 0.0, 1.0]):
                with self.assertRaisesRegex(DownloadError, "speed"):
                    self.download(base + NAME, options=options)
        self.assertFalse(self.final.exists())
        self.assertEqual(self.partial.stat().st_size, 1024)

    def test_slow_trickle_is_bounded_by_speed_window_not_chunk_size(self):
        stop = threading.Event()

        def handler(request):
            request.send_response(200)
            request.send_header("Content-Length", str(len(DATA)))
            request.send_header("ETag", '"v1"')
            request.end_headers()
            try:
                for byte in DATA:
                    request.wfile.write(bytes([byte]))
                    request.wfile.flush()
                    if stop.wait(0.02):
                        break
            except (BrokenPipeError, ConnectionResetError):
                pass

        options = DownloadOptions(retries=0, retry_delay=0, timeout=2,
                                  chunk_size=1048576, min_speed=1000, speed_window=0.12)
        with server(handler) as base:
            started = time.monotonic()
            try:
                with self.assertRaisesRegex(DownloadError, "speed"):
                    self.download(base + NAME, options=options)
            finally:
                stop.set()
            self.assertLess(time.monotonic() - started, 0.8)
        self.assertFalse(self.final.exists())
        self.assertGreater(self.partial.stat().st_size, 0)

    def test_stalled_body_is_bounded_by_speed_window(self):
        stop = threading.Event()

        def handler(request):
            request.send_response(200)
            request.send_header("Content-Length", str(len(DATA)))
            request.send_header("ETag", '"v1"')
            request.end_headers()
            request.wfile.write(DATA[:1])
            request.wfile.flush()
            stop.wait(2)

        options = DownloadOptions(retries=0, retry_delay=0, timeout=2,
                                  chunk_size=1048576, min_speed=1000, speed_window=0.12)
        with server(handler) as base:
            started = time.monotonic()
            try:
                with self.assertRaisesRegex(DownloadError, "speed"):
                    self.download(base + NAME, options=options)
            finally:
                stop.set()
            self.assertLess(time.monotonic() - started, 0.8)
        self.assertFalse(self.final.exists())

    def test_http_failures_have_bounded_attempts_and_backoff(self):
        requests = []

        def handler(request):
            requests.append(request.path)
            reply(request, b"unavailable", 503)

        options = DownloadOptions(retries=2, retry_delay=0.1)
        with server(handler) as base:
            with patch("solana_snapshot_downloader.download.time.sleep") as sleep:
                with self.assertRaisesRegex(DownloadError, "3 attempt"):
                    self.download(base + NAME, options=options)
                self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.1, 0.2])
        self.assertEqual(len(requests), 3)
        self.assertFalse(self.final.exists())

    def test_options_reject_invalid_values(self):
        for kwargs in ({"timeout": 0}, {"retries": -1}, {"chunk_size": 0},
                       {"speed_window": 0}, {"min_speed": -1}, {"timeout": float("nan")}):
            with self.subTest(options=kwargs), self.assertRaises(ValueError):
                DownloadOptions(**kwargs)


if __name__ == "__main__":
    unittest.main()
