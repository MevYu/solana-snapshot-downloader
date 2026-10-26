"""Discovery integration tests use only local HTTP servers and tiny bodies."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
import unittest
from unittest.mock import patch

from downloader.discovery import (
    DiscoveryError, discover_nodes, get_current_slot, measure_speed,
    probe_incremental, probe_node, probe_nodes,
)
from downloader.models import Candidate, snapshot_from_url


HASH = "1" * 32


def full_path(slot=1000, extension="zst"):
    return "/snapshot-{}-{}.tar.{}".format(slot, HASH, extension)


def incremental_path(base=1000, slot=1100, extension="zst"):
    return "/incremental-snapshot-{}-{}-{}.tar.{}".format(base, slot, HASH, extension)


def redirect(target):
    return 302, {"Location": target}, b""


@contextmanager
def serve(routes):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_HEAD(self):
            self.dispatch()

        def do_GET(self):
            self.dispatch()

        def do_POST(self):
            self.dispatch()

        def dispatch(self):
            requests.append((self.command, self.path, self.headers.get("Range")))
            route = routes.get((self.command, self.path), routes.get(self.path))
            result = route(self) if callable(route) else route
            if result is False:
                return
            if result is None:
                result = (404, {}, b"")
            status, headers, body = result
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            if "Content-Length" not in headers:
                self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever,
                              kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:" + str(server.server_port), requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def archive_routes(full=1000, incremental=1100, extension="zst"):
    routes = {
        "/snapshot.tar." + extension: redirect(full_path(full, extension)),
        full_path(full, extension): (200, {}, b"tiny archive"),
    }
    if incremental is not None:
        routes["/incremental-snapshot.tar." + extension] = redirect(
            incremental_path(full, incremental, extension))
        routes[incremental_path(full, incremental, extension)] = (200, {}, b"tiny incremental")
    return routes


def rpc_route(results, calls):
    def respond(handler):
        request = json.loads(handler.rfile.read(int(handler.headers["Content-Length"])))
        calls.append(request)
        result = results.get(request["method"])
        payload = {"jsonrpc": "2.0", "id": request["id"], "result": result}
        return 200, {"Content-Type": "application/json"}, json.dumps(payload).encode()
    return respond


class DiscoveryRPCTests(unittest.TestCase):
    def test_uses_public_rpc_fields_only_deduplicates_and_reads_finalized_slot(self):
        calls = []
        records = [
            {"rpc": None, "gossip": "8.8.4.4:8001"},
            {}, None, "invalid record", {"rpc": False},
            {"rpc": "8.8.8.8:8899"}, {"rpc": "http://8.8.8.8:8899/"},
            {"rpc": "[2606:4700:4700::1111]:8899"},
        ]
        for address in ("127.0.0.1", "10.1.2.3", "192.168.1.1", "172.19.0.1",
                        "100.64.0.1", "169.254.1.1", "0.0.0.0", "224.0.0.1", "example.com"):
            records.append({"rpc": address + ":8899"})
        records.extend([{"rpc": "[::1]:8899"}, {"rpc": "[fe80::1]:8899"}])
        results = {"getClusterNodes": records, "getSlot": 1200}
        with serve({("POST", "/"): rpc_route(results, calls)}) as (url, _):
            nodes, slot = discover_nodes([url])
        self.assertEqual(nodes, ["http://8.8.8.8:8899/", "http://[2606:4700:4700::1111]:8899/"])
        self.assertEqual(slot, 1200)
        self.assertEqual(calls[-1]["params"], [{"commitment": "finalized"}])

    def test_seed_failure_fails_over_for_both_rpc_methods(self):
        calls = []
        results = {"getClusterNodes": [{"rpc": "8.8.8.8:8899"}], "getSlot": 1200}
        with serve({}) as (bad, _), serve({("POST", "/"): rpc_route(results, calls)}) as (good, _):
            self.assertEqual(discover_nodes([bad, good])[1], 1200)
            self.assertEqual(get_current_slot([bad, good]), 1200)

    def test_invalid_current_slots_raise_instead_of_fabricating_a_slot(self):
        for value in (None, True, -1, "1200", {}, []):
            with self.subTest(value=value):
                with patch("downloader.discovery.rpc_call", return_value=value):
                    with self.assertRaises(DiscoveryError):
                        get_current_slot(["http://example.test"])
        with self.assertRaises(DiscoveryError):
            get_current_slot([])

    def test_valid_cluster_without_current_slot_is_an_error(self):
        calls = []
        results = {"getClusterNodes": [{"rpc": "8.8.8.8:8899"}], "getSlot": None}
        with serve({("POST", "/"): rpc_route(results, calls)}) as (url, _):
            with self.assertRaises(DiscoveryError):
                discover_nodes([url])

    def test_empty_private_or_malformed_cluster_is_an_error(self):
        for result in ({}, None, [], [{"rpc": None}], [{"rpc": "127.0.0.1:8899"}]):
            with self.subTest(result=result):
                with patch("downloader.discovery.rpc_call", return_value=result):
                    with self.assertRaises(DiscoveryError):
                        discover_nodes(["http://example.test"])


class SnapshotProbeTests(unittest.TestCase):
    def test_absolute_redirect_to_other_port_uses_exact_final_url(self):
        with serve({full_path(): (200, {}, b"tiny archive"),
                    incremental_path(): (200, {}, b"tiny incremental")}) as (target, target_requests):
            routes = {
                "/snapshot.tar.zst": redirect(target + full_path()),
                "/incremental-snapshot.tar.zst": redirect(target + incremental_path()),
            }
            with serve(routes) as (node, source_requests):
                candidate = probe_node(node)
        self.assertEqual(candidate.full.url, target + full_path())
        self.assertEqual(candidate.incremental.url, target + incremental_path())
        self.assertEqual(candidate.slot, 1100)
        self.assertTrue(all(method == "HEAD" for method, _, _ in source_requests + target_requests))

    def test_relative_redirect_and_legacy_bz2_alias_fallback(self):
        routes = archive_routes(extension="bz2")
        routes["/snapshot.tar.bz2"] = redirect(full_path(extension="bz2").lstrip("/"))
        with serve(routes) as (url, requests):
            candidate = probe_node(url)
        self.assertEqual(candidate.full.url, url + full_path(extension="bz2"))
        self.assertTrue(candidate.incremental.filename.endswith(".tar.bz2"))
        self.assertIn(("HEAD", "/snapshot.tar.zst", None), requests)
        self.assertIn(("HEAD", "/incremental-snapshot.tar.zst", None), requests)

    def test_head_not_supported_falls_back_to_single_byte_get(self):
        routes = archive_routes(incremental=None)
        routes[("HEAD", "/snapshot.tar.zst")] = (405, {}, b"")
        routes[("HEAD", full_path())] = (501, {}, b"")
        with serve(routes) as (url, requests):
            candidate = probe_node(url)
        self.assertIsNone(candidate.incremental)
        get_requests = [entry for entry in requests if entry[0] == "GET"]
        self.assertTrue(get_requests)
        self.assertTrue(all(request[2] == "bytes=0-0" for request in get_requests))

    def test_head_unsupported_after_redirect_also_falls_back(self):
        routes = archive_routes(incremental=None)
        routes[("HEAD", full_path())] = (405, {}, b"")
        with serve(routes) as (url, requests):
            self.assertEqual(probe_node(url).full.slot, 1000)
        self.assertIn(("GET", full_path(), "bytes=0-0"), requests)

    def test_absent_incremental_is_safe_full_only_candidate(self):
        with serve(archive_routes(incremental=None)) as (url, _):
            self.assertIsNone(probe_node(url).incremental)
            self.assertIsNone(probe_incremental(url))

    def test_missing_full_archive_is_rejected_and_reported_in_full_only_mode(self):
        callbacks = []
        with serve({}) as (url, _):
            with self.assertRaisesRegex(DiscoveryError, "no full snapshot"):
                probe_node(url)
            self.assertEqual(probe_nodes([url], full_only=True,
                                         on_result=lambda *args: callbacks.append(args)), [])
        self.assertIsNone(callbacks[0][1])
        self.assertIsInstance(callbacks[0][2], DiscoveryError)

    def test_incremental_errors_are_not_silently_accepted_as_absent(self):
        for route in ((503, {}, b""), redirect("/not-an-archive"),
                      redirect(full_path()), (200, {}, b"not redirected")):
            with self.subTest(route=route):
                routes = archive_routes()
                routes["/incremental-snapshot.tar.zst"] = route
                routes["/not-an-archive"] = (200, {}, b"")
                with serve(routes) as (url, _):
                    with self.assertRaises(DiscoveryError):
                        probe_node(url)

    def test_rollover_reprobes_both_aliases_once_and_freezes_matching_pair(self):
        full_calls = []

        def rotating_full(handler):
            full_calls.append(handler.path)
            return redirect(full_path(1000 if len(full_calls) == 1 else 1050))

        routes = archive_routes(full=1050, incremental=1150)
        routes["/snapshot.tar.zst"] = rotating_full
        routes[full_path(1000)] = (200, {}, b"old full")
        with serve(routes) as (url, _):
            candidate = probe_node(url)
        self.assertEqual(candidate.full.slot, 1050)
        self.assertEqual(candidate.incremental.base_slot, 1050)
        self.assertEqual(len(full_calls), 2)

    def test_persistent_pair_mismatch_is_rejected_after_two_pairs(self):
        routes = archive_routes()
        routes["/incremental-snapshot.tar.zst"] = redirect(incremental_path(1050, 1150))
        routes[incremental_path(1050, 1150)] = (200, {}, b"new incremental")
        with serve(routes) as (url, requests):
            with self.assertRaisesRegex(DiscoveryError, "do not match after retry"):
                probe_node(url)
        self.assertEqual(sum(path == "/snapshot.tar.zst" for _, path, _ in requests), 2)
        self.assertEqual(sum(path == "/incremental-snapshot.tar.zst" for _, path, _ in requests), 2)

    def test_full_only_ignores_incremental_and_filters_by_full_slot(self):
        routes = archive_routes()
        routes["/incremental-snapshot.tar.zst"] = (500, {}, b"")
        with serve(routes) as (url, requests):
            candidate = probe_nodes([url], current_slot=1100, max_slot_age=100,
                                    full_only=True)[0]
            self.assertEqual(candidate.slot, 1000)
            self.assertIsNone(candidate.incremental)
            self.assertEqual(probe_nodes([url], current_slot=1101, max_slot_age=100,
                                         full_only=True), [])
        self.assertFalse(any("incremental" in path for _, path, _ in requests))

    def test_probe_closes_responses_without_reading_archive_bodies(self):
        class FakeResponse:
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.closed = True

            def geturl(self):
                return "http://example.test" + full_path()

            def getcode(self):
                return 200

            def read(self, *args):
                raise AssertionError("archive body read during probe")

        response = FakeResponse()
        with patch("downloader.discovery.open_url", return_value=response):
            candidates = probe_nodes(["http://example.test"], full_only=True)
        self.assertEqual(len(candidates), 1)
        self.assertTrue(response.closed)


class ProbeRankingTests(unittest.TestCase):
    @staticmethod
    def candidate(node, slot, latency=0):
        return Candidate(node, snapshot_from_url("http://example.test" + full_path(slot)),
                         latency=latency)

    def test_age_future_dedup_ranking_and_callbacks(self):
        candidates = {
            "old": self.candidate("old", 800),
            "fresh": self.candidate("fresh", 1000, 0.5),
            "fast": self.candidate("fast", 1000, 0.1),
            "newest": self.candidate("newest", 1050, 5),
            "future": self.candidate("future", 1101),
        }
        callbacks = []
        with patch("downloader.discovery.probe_node", side_effect=lambda node, timeout: candidates[node]) as probe:
            result = probe_nodes(list(candidates) + ["fresh"], workers=2,
                                 current_slot=1000, max_slot_age=100,
                                 on_result=lambda *args: callbacks.append(args))
        self.assertEqual([candidate.node for candidate in result], ["newest", "fast", "fresh"])
        self.assertEqual(probe.call_count, 5)
        self.assertEqual(len(callbacks), 5)
        self.assertEqual({node for node, _, error in callbacks if error}, {"old", "future"})

    def test_age_zero_disables_age_limit_but_not_future_limit(self):
        candidates = {node: self.candidate(node, slot)
                      for node, slot in (("old", 1), ("edge", 1100), ("future", 1101))}
        with patch("downloader.discovery.probe_node", side_effect=lambda node, timeout: candidates[node]):
            result = probe_nodes(candidates, current_slot=1000, max_slot_age=0)
        self.assertEqual([candidate.node for candidate in result], ["edge", "old"])

    def test_worker_failures_are_reported_and_do_not_abort_other_nodes(self):
        callbacks = []

        def probe(node, timeout):
            if node == "bad":
                raise DiscoveryError("probe failed")
            return self.candidate(node, 1000)

        with patch("downloader.discovery.probe_node", side_effect=probe):
            result = probe_nodes(["bad", "good"], on_result=lambda *args: callbacks.append(args))
        self.assertEqual([candidate.node for candidate in result], ["good"])
        bad = next(entry for entry in callbacks if entry[0] == "bad")
        self.assertIsNone(bad[1])
        self.assertIsInstance(bad[2], DiscoveryError)


class SpeedSampleTests(unittest.TestCase):
    def test_samples_pinned_url_with_bounded_range(self):
        path = full_path()
        with serve({path: (200, {}, b"x" * 8192)}) as (url, requests):
            speed = measure_speed(snapshot_from_url(url + path), duration=1, max_bytes=1024)
        self.assertGreater(speed, 0)
        self.assertEqual(requests, [("GET", path, "bytes=0-1023")])

    def test_rejects_unexpected_filename_html_and_failed_http(self):
        for response in (redirect(full_path(1001)), (200, {"Content-Type": "text/html"}, b"oops"),
                         (200, {}, b"<!DOCTYPE html><html>oops</html>"), (503, {}, b"failed"),
                         redirect("/index.html")):
            with self.subTest(response=response):
                routes = {full_path(): response, full_path(1001): (200, {}, b"other archive"),
                          "/index.html": (200, {}, b"index")}
                with serve(routes) as (url, _):
                    with self.assertRaises(DiscoveryError):
                        measure_speed(snapshot_from_url(url + full_path()), duration=0.5)

    def test_sample_deadline_includes_stalled_headers(self):
        def stalled(handler):
            time.sleep(0.4)
            return 200, {}, b"late data"

        with serve({full_path(): stalled}) as (url, _):
            started = time.monotonic()
            with self.assertRaises(DiscoveryError):
                measure_speed(snapshot_from_url(url + full_path()), duration=0.08, timeout=5)
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.35)

    def test_sample_deadline_is_shared_by_slow_redirect_hops(self):
        def slow_redirect(target):
            def respond(handler):
                time.sleep(0.04)
                return redirect(target)
            return respond

        routes = {full_path(): slow_redirect("/hop1"),
                  "/hop1": slow_redirect("/hop2"),
                  "/hop2": slow_redirect("/hop3"),
                  "/hop3": slow_redirect("/hop4"),
                  "/hop4": (200, {}, b"not reached")}
        with serve(routes) as (url, requests):
            started = time.monotonic()
            with self.assertRaises(DiscoveryError):
                measure_speed(snapshot_from_url(url + full_path()), duration=0.09, timeout=5)
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.16)
        self.assertLess(len(requests), 5)

    def test_redirect_response_body_is_closed_without_downloading_it(self):
        def redirect_with_unbounded_body(handler):
            handler.send_response(302)
            handler.send_header("Location", full_path())
            handler.send_header("Content-Length", str(1024 * 1024 * 1024))
            handler.end_headers()
            handler.wfile.flush()
            time.sleep(0.4)
            return False

        path = "/redirect" + full_path()
        routes = {path: redirect_with_unbounded_body,
                  full_path(): (200, {}, b"tiny archive")}
        with serve(routes) as (url, _):
            speed = measure_speed(snapshot_from_url(url + path), duration=0.1)
        self.assertGreater(speed, 0)

    def test_sample_deadline_includes_stalled_body(self):
        def stalled(handler):
            handler.send_response(200)
            handler.send_header("Content-Length", "100")
            handler.end_headers()
            handler.wfile.flush()
            time.sleep(0.4)
            return False

        with serve({full_path(): stalled}) as (url, _):
            started = time.monotonic()
            with self.assertRaises(DiscoveryError):
                measure_speed(snapshot_from_url(url + full_path()), duration=0.08, timeout=5)
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.35)

    def test_empty_sample_is_rejected(self):
        with serve({full_path(): (200, {}, b"")}) as (url, _):
            with self.assertRaisesRegex(DiscoveryError, "no data"):
                measure_speed(snapshot_from_url(url + full_path()))


if __name__ == "__main__":
    unittest.main()
