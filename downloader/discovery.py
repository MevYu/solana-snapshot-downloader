"""Discover public RPC nodes and freeze compatible snapshot archive URLs."""

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import ipaddress
import math
import socket
import time
from typing import Callable, Iterable, List, Optional, Tuple
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit

from .models import Candidate, Snapshot, node_url, snapshot_from_url
from .net import open_url, rpc_call


class DiscoveryError(Exception):
    """No usable RPC response or snapshot could be obtained."""


def _positive_timeout(timeout: float) -> None:
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be positive and finite")


def _slot_from_rpc(url: str, timeout: float) -> int:
    slot = rpc_call(url, "getSlot", timeout=timeout,
                    params=[{"commitment": "finalized"}])
    if isinstance(slot, bool) or not isinstance(slot, int) or slot < 0:
        raise ValueError("getSlot returned an invalid slot")
    return slot


def get_current_slot(rpc_urls: Iterable[str], timeout: float = 10.0) -> int:
    """Read a finalized slot, failing over through the supplied RPC endpoints."""
    _positive_timeout(timeout)
    failures = []
    for url in rpc_urls:
        try:
            return _slot_from_rpc(url, timeout)
        except Exception as exc:
            failures.append(str(exc))
    detail = "; ".join(failures) if failures else "no RPC endpoints supplied"
    raise DiscoveryError("Could not get the current finalized slot: " + detail)


def _public_rpc_endpoint(value) -> Optional[str]:
    if not isinstance(value, str) or not value:
        return None
    try:
        endpoint = node_url(value)
        address = ipaddress.ip_address(urlsplit(endpoint).hostname)
    except ValueError:
        return None
    if (not address.is_global or address.is_multicast or address.is_reserved
            or address.is_unspecified):
        return None
    return endpoint


def discover_nodes(rpc_urls: Iterable[str], timeout: float = 10.0
                   ) -> Tuple[List[str], int]:
    """Return public advertised RPC endpoints and an authoritative current slot.

    Only ``rpc`` fields are considered; gossip ports and hostnames are never
    promoted to RPC endpoints. Explicit user-supplied nodes bypass this filter.
    """
    _positive_timeout(timeout)
    seeds = list(rpc_urls)
    failures = []
    nodes = None
    for url in seeds:
        try:
            records = rpc_call(url, "getClusterNodes", timeout=timeout)
            if not isinstance(records, list):
                raise ValueError("getClusterNodes returned an invalid node list")
            found = {}
            for record in records:
                endpoint = _public_rpc_endpoint(record.get("rpc")) if isinstance(record, dict) else None
                if endpoint:
                    found[endpoint] = None
            if not found:
                raise ValueError("getClusterNodes contained no public RPC endpoints")
            nodes = list(found)
            break
        except Exception as exc:
            failures.append(str(exc))
    if nodes is None:
        detail = "; ".join(failures) if failures else "no RPC endpoints supplied"
        raise DiscoveryError("Could not discover public RPC nodes: " + detail)
    return nodes, get_current_slot(seeds, timeout)


def _check_response(response, *, incremental: Optional[bool] = None) -> Snapshot:
    if response.getcode() not in (200, 206):
        raise DiscoveryError("Snapshot request returned HTTP " + str(response.getcode()))
    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    if content_type in ("text/html", "application/xhtml+xml"):
        raise DiscoveryError("Snapshot request returned an HTML page")
    snapshot = snapshot_from_url(response.geturl())
    if incremental is not None and snapshot.incremental != incremental:
        raise DiscoveryError("Snapshot alias resolved to the wrong archive kind")
    return snapshot


def _probe_alias(url: str, timeout: float, incremental: bool) -> Snapshot:
    try:
        response = open_url(url, timeout=timeout, method="HEAD")
    except HTTPError as exc:
        status = exc.code
        exc.close()
        if status not in (405, 501):
            raise
        # Some validators do not implement HEAD. Never consume the archive
        # body, including when an endpoint ignores this single-byte range.
        try:
            response = open_url(url, timeout=timeout,
                                headers={"Range": "bytes=0-0"})
        except HTTPError as error:
            error.close()
            raise
    with response:
        return _check_response(response, incremental=incremental)


def _probe_archive(node: str, timeout: float, incremental: bool) -> Optional[Snapshot]:
    prefix = "incremental-snapshot" if incremental else "snapshot"
    method_error = None
    for extension in ("zst", "bz2"):
        alias = urljoin(node, "/" + prefix + ".tar." + extension)
        try:
            return _probe_alias(alias, timeout, incremental)
        except HTTPError as exc:
            if exc.code in (405, 501):
                if method_error is None:
                    method_error = exc
            elif exc.code != 404:
                raise
    # Unsupported methods can be specific to an archive format. Only treat
    # the incremental as absent when every supported format returned 404.
    if method_error is not None:
        raise method_error
    if incremental:
        return None
    raise DiscoveryError("Node has no full snapshot archive")


def _probe_node(node: str, timeout: float, full_only: bool) -> Candidate:
    _positive_timeout(timeout)
    started = time.monotonic()
    try:
        endpoint = node_url(node)
        for _ in range(2):
            full = _probe_archive(endpoint, timeout, False)
            incremental = None if full_only else _probe_archive(endpoint, timeout, True)
            if incremental is None or incremental.base_slot == full.slot:
                return Candidate(endpoint, full, incremental, time.monotonic() - started)
            # Aliases may move while probing at a snapshot rollover. Re-read
            # both, so the candidate always contains a frozen compatible pair.
        raise DiscoveryError("Full and incremental snapshot base slots do not match after retry")
    except DiscoveryError:
        raise
    except Exception as exc:
        raise DiscoveryError("Could not probe " + str(node) + ": " + str(exc)) from exc


def probe_node(node: str, timeout: float = 10.0) -> Candidate:
    """Resolve full/incremental aliases without downloading archive bodies."""
    return _probe_node(node, timeout, False)


def probe_incremental(node: str, timeout: float = 10.0) -> Optional[Snapshot]:
    """Freeze the current incremental alias, returning None only for HTTP 404."""
    _positive_timeout(timeout)
    try:
        return _probe_archive(node_url(node), timeout, True)
    except DiscoveryError:
        raise
    except Exception as exc:
        raise DiscoveryError("Could not probe incremental snapshot: " + str(exc)) from exc


def probe_nodes(nodes: Iterable[str], workers: int = 32, timeout: float = 10.0,
                current_slot: Optional[int] = None, max_slot_age: int = 2500,
                full_only: bool = False,
                on_result: Optional[Callable] = None) -> List[Candidate]:
    """Probe with bounded concurrency and rank compatible, fresh candidates."""
    _positive_timeout(timeout)
    if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
        raise ValueError("workers must be a positive integer")
    if isinstance(max_slot_age, bool) or not isinstance(max_slot_age, int) or max_slot_age < 0:
        raise ValueError("max_slot_age must be a nonnegative integer")
    if current_slot is not None and (isinstance(current_slot, bool)
                                     or not isinstance(current_slot, int) or current_slot < 0):
        raise ValueError("current_slot must be a nonnegative integer")
    unique = iter(dict.fromkeys(nodes))
    candidates = []

    def submit_next(pool, pending):
        try:
            node = next(unique)
        except StopIteration:
            return False
        future = pool.submit(_probe_node, node, timeout, True) if full_only else pool.submit(probe_node, node, timeout)
        pending[future] = node
        return True

    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending = {}
        for _ in range(workers):
            if not submit_next(pool, pending):
                break
        while pending:
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                node = pending.pop(future)
                candidate, error = None, None
                try:
                    candidate = future.result()
                    if current_slot is not None:
                        if candidate.slot > current_slot + 100:
                            raise DiscoveryError("Snapshot slot is more than 100 slots ahead of the current slot")
                        if max_slot_age and current_slot - candidate.slot > max_slot_age:
                            raise DiscoveryError("Snapshot is older than max_slot_age")
                    candidates.append(candidate)
                except Exception as exc:
                    candidate, error = None, exc
                if on_result is not None:
                    on_result(node, candidate, error)
                submit_next(pool, pending)
    return sorted(candidates, key=lambda item: (-item.slot, item.latency))


def _set_read_timeout(response, timeout: float) -> None:
    # HTTPResponse.read1 performs one buffered/socket read. Tighten the socket
    # timeout each iteration so a stalled read cannot exceed the sample budget.
    stream = getattr(response, "fp", None)
    raw = getattr(stream, "raw", stream)
    sock = getattr(raw, "_sock", None)
    if sock is not None:
        sock.settimeout(timeout)


def measure_speed(snapshot: Snapshot, duration: float = 2.0,
                  max_bytes: int = 16 * 1024 * 1024, timeout: float = 10.0) -> float:
    """Sample a pinned full archive within a total time and byte budget."""
    _positive_timeout(timeout)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("duration must be positive and finite")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")
    if snapshot.incremental:
        raise ValueError("Speed sampling requires a full snapshot")
    started = time.monotonic()
    deadline = started + duration
    received = 0
    prefix = bytearray()
    try:
        with open_url(snapshot.url, timeout=min(timeout, duration), deadline=deadline,
                      headers={"Range": "bytes=0-" + str(max_bytes - 1)}) as response:
            actual = _check_response(response, incremental=False)
            if actual.filename != snapshot.filename:
                raise DiscoveryError("Pinned snapshot redirected to a different archive")
            read = getattr(response, "read1", response.read)
            while received < max_bytes:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                _set_read_timeout(response, min(timeout, remaining))
                try:
                    chunk = read(min(64 * 1024, max_bytes - received))
                except (TimeoutError, socket.timeout):
                    if received:
                        break
                    raise
                if not chunk:
                    break
                if len(prefix) < 512:
                    prefix.extend(chunk[:512 - len(prefix)])
                    beginning = bytes(prefix).lstrip().lower()
                    if beginning.startswith(b"\xef\xbb\xbf"):
                        beginning = beginning[3:].lstrip()
                    if beginning.startswith((b"<!doctype html", b"<html")):
                        raise DiscoveryError("Snapshot request returned an HTML body")
                received += len(chunk)
        if not received:
            raise DiscoveryError("Snapshot speed sample returned no data")
        return received / max(time.monotonic() - started, 1e-9)
    except DiscoveryError:
        raise
    except Exception as exc:
        if isinstance(exc, HTTPError):
            exc.close()
        raise DiscoveryError("Could not measure snapshot download speed: " + str(exc)) from exc
