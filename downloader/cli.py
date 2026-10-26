"""Command-line orchestration, output locking, and human-readable progress."""

import argparse
from contextlib import contextmanager
from dataclasses import asdict
import json
import logging
import math
import os
from pathlib import Path
import sys
import time

from . import __version__
from .discovery import (DiscoveryError, discover_nodes, get_current_slot,
                        measure_speed, probe_incremental, probe_nodes)
from .download import DownloadError, DownloadOptions, Downloader
from .models import http_url, node_url


LOG = logging.getLogger("snapshot_downloader")
NETWORKS = {
    "mainnet": "https://api.mainnet-beta.solana.com",
    "testnet": "https://api.testnet.solana.com",
    "devnet": "https://api.devnet.solana.com",
}


def nonnegative(value):
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise argparse.ArgumentTypeError("must be a finite nonnegative number")
    return result


def positive(value):
    result = nonnegative(value)
    if result == 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return result


def positive_int(value):
    result = int(value)
    if result < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return result


def nonnegative_int(value):
    result = int(value)
    if result < 0:
        raise argparse.ArgumentTypeError("must be a nonnegative integer")
    return result


def parser():
    p = argparse.ArgumentParser(
        description="Find and download matching Solana snapshots. Python standard library only.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--version", action="version", version="%(prog)s " + __version__)
    p.add_argument("--network", choices=NETWORKS, default="mainnet", help="default RPC network")
    p.add_argument("--rpc", action="append", metavar="URL", help="RPC seed URL; repeat for failover")
    p.add_argument("--node", action="append", metavar="URL", help="explicit snapshot node; repeat to skip cluster discovery")
    p.add_argument("--output", type=Path, default=Path("snapshots"), help="directory for archives and download state")
    p.add_argument("--workers", type=positive_int, default=32, help="concurrent node probes (maximum 128)")
    p.add_argument("--timeout", type=positive, default=10.0, help="HTTP connection/read timeout in seconds")
    p.add_argument("--max-slot-age", type=nonnegative_int, default=2500, help="maximum candidate age in slots; 0 disables age limit")
    p.add_argument("--full-only", action="store_true", help="download only a full snapshot; age applies to its slot")
    p.add_argument("--list", action="store_true", help="list candidates without downloading snapshot bodies")
    p.add_argument("--json", action="store_true", help="print machine-readable candidates (requires --list)")
    p.add_argument("--speed-test-count", type=nonnegative_int, default=5, help="sample this many freshest candidates; 0 disables")
    p.add_argument("--speed-test-seconds", type=positive, default=2.0, help="sample duration, also capped at 16 MiB per node")
    p.add_argument("--min-speed", type=nonnegative, default=0.0, help="minimum sampled/sustained speed in MB/s; 0 disables")
    p.add_argument("--speed-window", type=positive, default=30.0, help="sustained download speed measurement window in seconds")
    p.add_argument("--retries", type=nonnegative_int, default=3, help="additional retries per file")
    p.add_argument("--retry-delay", type=nonnegative, default=2.0, help="initial retry delay in seconds")
    p.add_argument("--attempts", type=positive_int, default=3, help="discovery/download rounds before failing")
    p.add_argument("--verbose", action="store_true", help="include per-node failures and diagnostic tracebacks")
    return p


@contextmanager
def output_lock(directory):
    """Use a kernel lock, so stale PID files cannot block a later run."""
    import fcntl

    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ".snapshot-downloader.lock"
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DownloadError("Another downloader is using " + str(directory)) from exc
        os.ftruncate(fd, 0)
        os.write(fd, (str(os.getpid()) + "\n").encode("ascii"))
        try:
            yield
        finally:
            os.ftruncate(fd, 0)
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


class Progress:
    def __init__(self):
        self.last = {}

    def __call__(self, snapshot, downloaded, total, speed):
        now = time.monotonic()
        if now - self.last.get(snapshot.filename, -math.inf) < 2 and downloaded != total:
            return
        self.last[snapshot.filename] = now
        percent = "%.1f%%" % (100 * downloaded / total) if total else "?%%"
        eta = "%.0fs" % ((total - downloaded) / speed) if total and speed > 0 else "?"
        LOG.info("%s: %s, %.1f / %s MiB, %.2f MiB/s, ETA %s",
                 snapshot.filename, percent, downloaded / 1048576,
                 "%.1f" % (total / 1048576) if total else "?", speed / 1048576, eta)


def scan(args, rpc_urls):
    if args.node:
        nodes = list(dict.fromkeys(node_url(value) for value in args.node))
        try:
            current_slot = get_current_slot(rpc_urls, timeout=args.timeout)
        except DiscoveryError:
            if args.max_slot_age:
                raise
            current_slot = None
            LOG.warning("RPC current slot unavailable; age checking is explicitly disabled")
    else:
        nodes, current_slot = discover_nodes(rpc_urls, timeout=args.timeout)
    LOG.info("Probing %d nodes with %d workers; current slot: %s", len(nodes), args.workers, current_slot)
    failures = [0]

    def on_result(node, candidate, error):
        if error is not None:
            failures[0] += 1
            LOG.debug("Skipped %s: %s", node, error)

    candidates = probe_nodes(
        nodes, workers=args.workers, timeout=args.timeout, current_slot=current_slot,
        max_slot_age=args.max_slot_age, full_only=args.full_only, on_result=on_result,
    )
    LOG.info("Found %d matching candidates; skipped %d nodes", len(candidates), failures[0])
    if not candidates:
        raise DiscoveryError("No matching snapshots found. Use --verbose to inspect failures, --rpc for another seed, or --node for a known source.")
    return candidates, current_slot


def list_candidates(candidates, current_slot, json_output):
    if json_output:
        print(json.dumps({"current_slot": current_slot, "candidates": [asdict(c) for c in candidates]}, indent=2))
        return
    print("FULL SLOT     LATEST SLOT   AGE (slots)  LATENCY (ms)  NODE")
    for candidate in candidates:
        age = str(current_slot - candidate.slot) if current_slot is not None else "?"
        print("%-13d %-13d %-12s %-13.1f %s" % (
            candidate.full.slot, candidate.slot, age, candidate.latency * 1000, candidate.node))


def rank_candidates(candidates, args):
    if args.speed_test_count == 0:
        return candidates
    sampled = []
    count = min(args.speed_test_count, len(candidates))
    for candidate in candidates[:count]:
        try:
            speed = measure_speed(candidate.full, duration=args.speed_test_seconds,
                                  max_bytes=16 * 1048576, timeout=args.timeout)
            LOG.info("Sample %s: %.2f MB/s", candidate.node, speed / 1000000)
            if speed < args.min_speed * 1000000:
                LOG.warning("Skipping sample below --min-speed: %s", candidate.node)
                continue
            sampled.append((speed, candidate))
        except (DiscoveryError, OSError, ValueError) as exc:
            LOG.warning("Speed sample failed for %s: %s", candidate.node, exc)
    sampled.sort(key=lambda value: (value[0], value[1].slot), reverse=True)
    return [value[1] for value in sampled] + candidates[count:]


def download_candidate(candidate, downloader, args, rpc_urls):
    full_path = downloader.download(candidate.full, args.output)
    incremental = candidate.incremental
    if not args.full_only:
        # A large full download may take long enough for the incremental alias to advance.
        # Only adopt a newer incremental when it still references the pinned full slot.
        try:
            refreshed = probe_incremental(candidate.node, timeout=args.timeout)
            if (refreshed is not None and refreshed.base_slot == candidate.full.slot
                    and (incremental is None or refreshed.slot >= incremental.slot)):
                incremental = refreshed
        except DiscoveryError as exc:
            LOG.warning("Could not refresh incremental; keeping the pinned candidate: %s", exc)
    effective_slot = incremental.slot if incremental else candidate.full.slot
    if args.max_slot_age:
        tip = get_current_slot(rpc_urls, timeout=args.timeout)
        if tip - effective_slot > args.max_slot_age or effective_slot > tip + 100:
            raise DiscoveryError("Snapshot became stale or inconsistent while downloading the full archive; rediscovery is required")
    paths = [full_path]
    if incremental is not None:
        paths.append(downloader.download(incremental, args.output))
        if args.max_slot_age:
            tip = get_current_slot(rpc_urls, timeout=args.timeout)
            if tip - effective_slot > args.max_slot_age or effective_slot > tip + 100:
                raise DiscoveryError("Downloaded snapshot is no longer within --max-slot-age; rediscovery is required")
    return paths


def run(args):
    rpc_urls = [http_url(value) for value in (args.rpc or [NETWORKS[args.network]])]
    if args.list:
        candidates, current_slot = scan(args, rpc_urls)
        list_candidates(candidates, current_slot, args.json)
        return 0
    downloader = Downloader(DownloadOptions(
        timeout=args.timeout, retries=args.retries, retry_delay=args.retry_delay,
        min_speed=args.min_speed * 1000000, speed_window=args.speed_window,
    ), progress=Progress())
    with output_lock(args.output):
        for attempt in range(1, args.attempts + 1):
            LOG.info("Discovery/download round %d/%d", attempt, args.attempts)
            try:
                candidates, _ = scan(args, rpc_urls)
                ranked = rank_candidates(candidates, args)
                for candidate in ranked:
                    LOG.info("Selected %s: full slot %d, latest slot %d", candidate.node, candidate.full.slot, candidate.slot)
                    try:
                        paths = download_candidate(candidate, downloader, args, rpc_urls)
                    except (DownloadError, DiscoveryError) as exc:
                        LOG.warning("Candidate failed: %s", exc, exc_info=args.verbose)
                        continue
                    LOG.info("Done. Downloaded snapshots:")
                    for path in paths:
                        print(str(path.resolve()))
                    return 0
            except DiscoveryError as exc:
                LOG.warning("Discovery failed: %s", exc, exc_info=args.verbose)
            if attempt < args.attempts:
                time.sleep(args.retry_delay)
    raise DownloadError("No candidate completed successfully. Partial downloads were retained; rerun the same command to retry.")


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    if args.workers > 128:
        p.error("--workers must be between 1 and 128")
    if args.json and not args.list:
        p.error("--json requires --list")
    if sys.version_info < (3, 9):
        p.error("Python 3.9 or newer is required")
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s", stream=sys.stderr)
    try:
        return run(args)
    except KeyboardInterrupt:
        LOG.warning("Interrupted. Partial downloads were kept; rerun the same command to resume when supported.")
        return 130
    except (DiscoveryError, DownloadError, OSError, ValueError) as exc:
        LOG.error("%s", exc, exc_info=args.verbose)
        return 1
