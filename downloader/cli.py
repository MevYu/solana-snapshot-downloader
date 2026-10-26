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
from .progress import ProgressDisplay


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


def parser(full_help=False):
    p = argparse.ArgumentParser(
        description="Find and download matching Solana snapshots. Python standard library only.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    def advanced(help_text):
        return help_text if full_help else argparse.SUPPRESS

    p.add_argument("--version", action="version", version="%(prog)s " + __version__, help="show version and exit")
    p.add_argument("--help-all", action="store_true", help="show all options, including advanced tuning")
    p.add_argument("--network", choices=NETWORKS, default="mainnet", help="default RPC network")
    p.add_argument("--rpc", action="append", metavar="URL", help="RPC seed URL; repeat for failover")
    p.add_argument("--node", action="append", metavar="URL", help="explicit snapshot node; repeat to skip cluster discovery")
    p.add_argument("-o", "--output", type=Path, default=Path("snapshots"), help="directory for archives and download state")
    p.add_argument("--workers", type=positive_int, default=32, help=advanced("concurrent node probes (maximum 128)"))
    p.add_argument("--timeout", type=positive, default=10.0, help=advanced("HTTP connection/read timeout in seconds"))
    p.add_argument("--max-slot-age", type=nonnegative_int, default=2500, help=advanced("maximum candidate age in slots; 0 disables age limit"))
    p.add_argument("--full-only", action="store_true", help="download only a full snapshot; age applies to its slot")
    p.add_argument("--list", action="store_true", help="list candidates without downloading snapshot bodies")
    p.add_argument("--json", action="store_true", help=advanced("print machine-readable candidates (requires --list)"))
    p.add_argument("--speed-test-count", type=nonnegative_int, default=5, help=advanced("sample this many freshest candidates; 0 disables"))
    p.add_argument("--speed-test-seconds", type=positive, default=2.0, help=advanced("sample duration, also capped at 16 MiB per node"))
    p.add_argument("--min-speed", type=nonnegative, default=0.0, help="minimum sampled/sustained speed in MB/s; 0 disables")
    p.add_argument("--speed-window", type=positive, default=30.0, help=advanced("sustained download speed measurement window in seconds"))
    p.add_argument("--retries", type=nonnegative_int, default=3, help=advanced("additional retries per file"))
    p.add_argument("--retry-delay", type=nonnegative, default=2.0, help=advanced("initial retry delay in seconds"))
    p.add_argument("--attempts", type=positive_int, default=3, help=advanced("discovery/download rounds before failing"))
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
    """Adapt transfer byte callbacks without announcing premature success."""

    def __init__(self, display):
        self.display = display

    def __call__(self, snapshot, downloaded, total, speed):
        detail = "Finalizing archive" if total and downloaded >= total else snapshot.filename
        self.display.update(downloaded, total=total, detail=detail, speed=speed)


class ProgressLogHandler(logging.Handler):
    def __init__(self, display):
        super().__init__()
        self.display = display

    def emit(self, record):
        try:
            self.display.message(self.format(record))
        except Exception:
            self.handleError(record)


@contextmanager
def progress_logging(display, verbose):
    handler = ProgressLogHandler(display)
    handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s" if verbose else "%(message)s"))
    previous = LOG.handlers[:], LOG.level, LOG.propagate
    LOG.handlers = [handler]
    LOG.setLevel(logging.DEBUG if verbose else logging.INFO)
    LOG.propagate = False
    try:
        yield
    finally:
        LOG.handlers, LOG.level, LOG.propagate = previous
        handler.close()


def scan(args, rpc_urls, progress=None):
    started = time.monotonic()
    summary = None
    if progress is not None:
        progress.start("[1/3] Search", detail="Waiting for RPC seed")
    try:
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
        LOG.debug("Probing %d nodes with %d workers; current slot: %s", len(nodes), args.workers, current_slot)
        counts = {"completed": 0, "usable": 0, "skipped": 0}
        if progress is not None:
            progress.update(0, total=len(nodes), detail="0 usable, 0 skipped")

        def on_result(node, candidate, error):
            counts["completed"] += 1
            if error is not None:
                counts["skipped"] += 1
                LOG.debug("Skipped %s: %s", node, error)
            else:
                counts["usable"] += 1
            if progress is not None:
                progress.update(counts["completed"], total=len(nodes),
                                detail="%d usable, %d skipped" % (counts["usable"], counts["skipped"]))

        candidates = probe_nodes(
            nodes, workers=args.workers, timeout=args.timeout, current_slot=current_slot,
            max_slot_age=args.max_slot_age, full_only=args.full_only, on_result=on_result,
        )
        summary = "%d usable, %d skipped in %.1fs" % (
            len(candidates), counts["skipped"], time.monotonic() - started)
        if not candidates:
            raise DiscoveryError("No matching snapshots found. Use --verbose to inspect failures, --rpc for another seed, or --node for a known source.")
        if progress is not None:
            progress.finish(summary)
        else:
            LOG.info("Search: %s", summary)
        return candidates, current_slot
    except (Exception, KeyboardInterrupt) as exc:
        if progress is not None:
            message = "Search interrupted" if isinstance(exc, KeyboardInterrupt) else "Search failed"
            if summary is not None:
                message += "; " + summary
            progress.finish(message)
        raise


def list_candidates(candidates, current_slot, json_output):
    if json_output:
        print(json.dumps({"current_slot": current_slot, "candidates": [asdict(c) for c in candidates]}, indent=2))
        return
    print("FULL SLOT     LATEST SLOT   AGE (slots)  LATENCY (ms)  NODE")
    for candidate in candidates:
        age = str(current_slot - candidate.slot) if current_slot is not None else "?"
        print("%-13d %-13d %-12s %-13.1f %s" % (
            candidate.full.slot, candidate.slot, age, candidate.latency * 1000, candidate.node))


def rank_candidates(candidates, args, progress=None):
    count = min(args.speed_test_count, len(candidates))
    if progress is not None:
        progress.start("[2/3] Speed", total=count, detail="Sample starting" if count else "Sample disabled")
    if args.speed_test_count == 0:
        if progress is not None:
            progress.finish("Sample disabled")
        return candidates
    sampled = []
    failures = 0
    try:
        for index, candidate in enumerate(candidates[:count]):
            if progress is not None:
                progress.update(index, total=count, detail="Sample " + candidate.node)
                progress.message("Sample %d/%d: %s" % (index + 1, count, candidate.node))
            result = "Sample failed"
            try:
                speed = measure_speed(candidate.full, duration=args.speed_test_seconds,
                                      max_bytes=16 * 1048576, timeout=args.timeout)
                result = "Sample %.2f MB/s" % (speed / 1000000)
                LOG.debug("Sample %s: %.2f MB/s", candidate.node, speed / 1000000)
                if speed < args.min_speed * 1000000:
                    failures += 1
                    result += "; below --min-speed"
                    LOG.warning("Skipping sample below --min-speed: %s", candidate.node)
                    continue
                sampled.append((speed, candidate))
            except (DiscoveryError, OSError, ValueError) as exc:
                failures += 1
                LOG.warning("Speed sample failed for %s: %s", candidate.node, exc)
            finally:
                if progress is not None:
                    progress.update(index + 1, total=count, detail=result)
        summary = "Sample: %d usable, %d skipped" % (len(sampled), failures)
        if sampled:
            speeds = [value[0] / 1000000 for value in sampled]
            summary += "; %.2f-%.2f MB/s" % (min(speeds), max(speeds))
        if progress is not None:
            progress.finish(summary)
        else:
            LOG.info("%s", summary)
    except (Exception, KeyboardInterrupt) as exc:
        if progress is not None:
            progress.finish("Speed interrupted" if isinstance(exc, KeyboardInterrupt) else "Speed failed")
        raise
    sampled.sort(key=lambda value: (value[0], value[1].slot), reverse=True)
    return [value[1] for value in sampled] + candidates[count:]


def download_candidate(candidate, downloader, args, rpc_urls, progress=None):
    def download(snapshot, label):
        if progress is not None:
            progress.start("[3/3] " + label, unit="bytes", detail="Connecting")
        try:
            path = downloader.download(snapshot, args.output)
        except (Exception, KeyboardInterrupt) as exc:
            if progress is not None:
                progress.finish(label + (" interrupted" if isinstance(exc, KeyboardInterrupt) else " failed"))
            raise
        if progress is not None:
            progress.finish(label + " ready")
        return path

    full_path = download(candidate.full, "Full")
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
        paths.append(download(incremental, "Incremental"))
        if args.max_slot_age:
            tip = get_current_slot(rpc_urls, timeout=args.timeout)
            if tip - effective_slot > args.max_slot_age or effective_slot > tip + 100:
                raise DiscoveryError("Downloaded snapshot is no longer within --max-slot-age; rediscovery is required")
    return paths


def run(args, progress=None):
    rpc_urls = [http_url(value) for value in (args.rpc or [NETWORKS[args.network]])]
    if args.list:
        candidates, current_slot = scan(args, rpc_urls, progress=progress)
        list_candidates(candidates, current_slot, args.json)
        return 0
    downloader = Downloader(DownloadOptions(
        timeout=args.timeout, retries=args.retries, retry_delay=args.retry_delay,
        min_speed=args.min_speed * 1000000, speed_window=args.speed_window,
    ), progress=Progress(progress) if progress is not None else None)
    with output_lock(args.output):
        for attempt in range(1, args.attempts + 1):
            LOG.debug("Discovery/download round %d/%d", attempt, args.attempts)
            try:
                candidates, _ = scan(args, rpc_urls, progress=progress)
                ranked = rank_candidates(candidates, args, progress=progress)
                for candidate in ranked:
                    LOG.info("Selected %s: full slot %d, latest slot %d", candidate.node, candidate.full.slot, candidate.slot)
                    try:
                        paths = download_candidate(candidate, downloader, args, rpc_urls, progress=progress)
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
    argv = list(sys.argv[1:] if argv is None else argv)
    p = parser(full_help="--help-all" in argv)
    args = p.parse_args(argv)
    if args.help_all:
        p.print_help()
        return 0
    if args.workers > 128:
        p.error("--workers must be between 1 and 128")
    if args.json and not args.list:
        p.error("--json requires --list")
    if sys.version_info < (3, 9):
        p.error("Python 3.9 or newer is required")
    with ProgressDisplay() as progress, progress_logging(progress, args.verbose):
        try:
            return run(args, progress=progress)
        except KeyboardInterrupt:
            LOG.warning("Interrupted. Partial downloads were kept; rerun the same command to resume when supported.")
            return 130
        except (DiscoveryError, DownloadError, OSError, ValueError) as exc:
            LOG.error("%s", exc, exc_info=args.verbose)
            return 1
