"""Bounded-memory HTTP downloads with conservative, source-specific resumption."""

from dataclasses import dataclass
from email.utils import parsedate_to_datetime
import http.client
import json
import math
import os
from pathlib import Path
import re
import shutil
import socket
import stat
import tempfile
import time
from typing import Callable, Optional
from urllib.error import HTTPError, URLError

from .models import Snapshot, snapshot_from_url
from .net import open_url


class DownloadError(Exception):
    """The archive could not be downloaded and verified."""


class _UnsafePath(DownloadError):
    pass


class _Restart(DownloadError):
    """The next request must start with an empty partial archive."""


class _StateError(DownloadError):
    pass


@dataclass(frozen=True)
class DownloadOptions:
    timeout: float = 15.0
    retries: int = 3
    retry_delay: float = 2.0
    chunk_size: int = 1048576
    min_speed: float = 0.0
    speed_window: float = 30.0

    def __post_init__(self):
        for name in ("timeout", "retry_delay", "min_speed", "speed_window"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(name + " must be a finite number")
        if self.timeout <= 0 or self.speed_window <= 0:
            raise ValueError("timeout and speed_window must be positive")
        if self.retry_delay < 0 or self.min_speed < 0:
            raise ValueError("retry_delay and min_speed cannot be negative")
        if type(self.retries) is not int or self.retries < 0:
            raise ValueError("retries must be a nonnegative integer")
        if type(self.chunk_size) is not int or self.chunk_size <= 0:
            raise ValueError("chunk_size must be a positive integer")


@dataclass(frozen=True)
class _Paths:
    final: Path
    part: Path
    state: Path
    legacy_metadata: Path
    legacy_receipt: Path


_METADATA_LIMIT = 16384
_STATE_LIMIT = 1024 * 1024
_RANGE = re.compile(r"bytes ([0-9]{1,20})-([0-9]{1,20})/([0-9]{1,20})\Z")
_UNSATISFIED_RANGE = re.compile(r"bytes \*/([0-9]{1,20})\Z")
_STRONG_ETAG = re.compile(r'"[\x21\x23-\x7e\x80-\xff]*"\Z')
_ERROR_JSON = re.compile(br'^\s*\{\s*"(?:error|errors|message|detail)"\s*:', re.I)


def _regular_stat(path):
    try:
        result = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(result.st_mode):
        raise _UnsafePath("Refusing nonregular file or symlink: " + str(path))
    return result


def _open_regular(path, flags, mode=0o600):
    _regular_stat(path)
    fd = os.open(str(path), flags | getattr(os, "O_NOFOLLOW", 0), mode)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise _UnsafePath("Refusing nonregular file: " + str(path))
    return fd


def _unique_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("Duplicate JSON key")
        result[name] = value
    return result


def _load_json(path, limit=_METADATA_LIMIT, strict=False):
    if _regular_stat(path) is None:
        return None
    fd = _open_regular(path, os.O_RDONLY)
    with os.fdopen(fd, "rb") as stream:
        if os.fstat(stream.fileno()).st_size > limit:
            if strict:
                raise _StateError("Download state exceeds its size limit: " + str(path))
            return None
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        if strict:
            raise _StateError("Download state exceeds its size limit: " + str(path))
        return None
    try:
        result = json.loads(raw, object_pairs_hook=_unique_object)
        if not isinstance(result, dict):
            raise ValueError("Expected a JSON object")
    except (ValueError, UnicodeError, RecursionError) as exc:
        if strict:
            raise _StateError("Malformed download state; preserve or move it aside before retrying: " + str(path)) from exc
        return None
    return result


def _write_json(path, value, limit=_METADATA_LIMIT):
    _regular_stat(path)
    raw = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    if len(raw) > limit:
        raise DownloadError("Download metadata exceeds its size limit")
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        _regular_stat(path)
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _sync_directory(path):
    fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _unlink_regular(path):
    if _regular_stat(path) is not None:
        path.unlink()


def _identity(result):
    return {
        "device": result.st_dev, "inode": result.st_ino,
        "size": result.st_size, "mtime_ns": result.st_mtime_ns,
    }


def _state_snapshot(filename):
    if not isinstance(filename, str) or "/" in filename or "\\" in filename:
        raise ValueError("Invalid archive filename")
    snapshot = snapshot_from_url("http://state.invalid/" + filename)
    if snapshot.filename != filename:
        raise ValueError("Invalid archive filename")
    return snapshot


def _validate_state(value, path):
    try:
        if (set(value) != {"version", "files"} or type(value["version"]) is not int
                or value["version"] != 1 or not isinstance(value["files"], dict)
                or len(value["files"]) > 2):
            raise ValueError("Invalid state schema")
        full = incremental = None
        for filename, entry in value["files"].items():
            snapshot = _state_snapshot(filename)
            if snapshot.incremental:
                if incremental is not None:
                    raise ValueError("Multiple incremental archives")
                incremental = snapshot
            else:
                if full is not None:
                    raise ValueError("Multiple full archives")
                full = snapshot
            if (not isinstance(entry, dict) or not entry
                    or not set(entry).issubset({"partial", "complete"})):
                raise ValueError("Invalid archive state entry")
            for record in entry.values():
                if (not isinstance(record, dict) or type(record.get("version")) is not int
                        or record.get("version") != 1
                        or record.get("filename") != filename
                        or not isinstance(record.get("source"), str)
                        or not isinstance(record.get("resolved_url"), str)):
                    raise ValueError("Invalid archive state record")
            if "partial" in entry:
                record = entry["partial"]
                if (not _positive_integer(record.get("total_size"))
                        or type(record.get("partial_device")) is not int
                        or type(record.get("partial_inode")) is not int):
                    raise ValueError("Invalid partial archive state")
            if "complete" in entry:
                identity = entry["complete"].get("stat")
                if (not isinstance(identity, dict)
                        or set(identity) != {"device", "inode", "size", "mtime_ns"}
                        or any(type(number) is not int for number in identity.values())
                        or identity["size"] <= 0):
                    raise ValueError("Invalid complete archive state")
        if full and incremental and incremental.base_slot != full.slot:
            raise ValueError("Full and incremental archive slots do not match")
    except (KeyError, TypeError, ValueError) as exc:
        raise _StateError("Invalid download state structure; preserve or move it aside before retrying: " + str(path)) from exc
    return value


def _read_state(path):
    value = _load_json(path, _STATE_LIMIT, strict=True)
    if value is None:
        return {"version": 1, "files": {}}
    return _validate_state(value, path)


def _write_state(path, value):
    _validate_state(value, path)
    _write_json(path, value, _STATE_LIMIT)


def _select_current_pair(state, filename):
    """Retain only this full snapshot and its one matching incremental."""
    current = _state_snapshot(filename)
    files = state["files"]
    for other_name in list(files):
        if other_name == filename:
            continue
        other = _state_snapshot(other_name)
        if current.incremental:
            keep = not other.incremental and other.slot == current.base_slot
        else:
            # Repeated writes of the same full preserve its current incremental.
            keep = filename in files and other.incremental and other.base_slot == current.slot
        if not keep:
            del files[other_name]


def _record(paths, kind):
    return _read_state(paths.state)["files"].get(paths.final.name, {}).get(kind)


def _update_record(paths, kind, value):
    state = _read_state(paths.state)
    filename = paths.final.name
    if value is None:
        entry = state["files"].get(filename)
        if not entry or kind not in entry:
            return
        del entry[kind]
        if not entry:
            del state["files"][filename]
    else:
        _select_current_pair(state, filename)
        state["files"].setdefault(filename, {})[kind] = value
    _write_state(paths.state, state)


def _positive_integer(value):
    return type(value) is int and value > 0


def _one_header(headers, name, default=None):
    values = headers.get_all(name, [])
    if len(values) > 1:
        raise _Restart("Archive response repeats " + name)
    return values[0] if values else default


def _validator(headers):
    etag = _one_header(headers, "ETag", "")
    if _STRONG_ETAG.fullmatch(etag):
        return "ETag", etag
    # A weak ETag explicitly rules out the date fallback for If-Range.
    if headers.get_all("ETag", []):
        return None, None
    modified = _one_header(headers, "Last-Modified", "")
    server_date = _one_header(headers, "Date", "")
    try:
        parsed = parsedate_to_datetime(modified)
        dated = parsedate_to_datetime(server_date)
        if (parsed.tzinfo is not None and dated.tzinfo is not None
                and "\r" not in modified + server_date and "\n" not in modified + server_date
                and (dated - parsed).total_seconds() >= 60):
            return "Last-Modified", modified
    except (TypeError, ValueError, OverflowError):
        pass
    return None, None


def _content_length(headers):
    values = headers.get_all("Content-Length", [])
    if len(values) != 1 or not re.fullmatch(r"[0-9]{1,20}", values[0].strip()):
        raise DownloadError("Archive response must have one valid Content-Length")
    length = int(values[0])
    if length <= 0:
        raise DownloadError("Archive response is empty")
    return length


def _error_document(prefix):
    text = prefix.lstrip(b" \t\r\n\xef\xbb\xbf").lower()
    return (text.startswith((b"<!doctype html", b"<html", b"<head", b"<body", b"<?xml"))
            or bool(_ERROR_JSON.match(text)))


class Downloader:
    def __init__(self, options: DownloadOptions, progress: Optional[Callable] = None):
        self.options = options
        self.progress = progress

    def download(self, snapshot: Snapshot, output_dir: Path) -> Path:
        """Download a complete archive; failures and interrupts retain safe partials.

        ``retries`` counts additional attempts after the initial request. A final
        archive is reusable only with a matching receipt created by this engine.
        """
        try:
            canonical = snapshot_from_url(snapshot.url)
        except ValueError as exc:
            raise DownloadError(str(exc)) from exc
        if (canonical.filename, canonical.slot, canonical.base_slot) != (
                snapshot.filename, snapshot.slot, snapshot.base_slot):
            raise DownloadError("Snapshot URL and archive identity disagree")
        snapshot = canonical
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        paths = _Paths(
            output_dir / snapshot.filename,
            output_dir / (snapshot.filename + ".part"),
            output_dir / ".download-state.json",
            output_dir / (snapshot.filename + ".part.json"),
            output_dir / (snapshot.filename + ".complete.json"),
        )
        for path in (paths.final, paths.part, paths.state, paths.legacy_metadata, paths.legacy_receipt):
            _regular_stat(path)
        try:
            self._migrate(snapshot, paths)
        except OSError as exc:
            raise DownloadError("Could not migrate legacy download state: " + str(exc)) from exc
        if _regular_stat(paths.final) is not None:
            self._verify_existing(snapshot, paths)
            return paths.final
        last_error = None
        for attempt in range(self.options.retries + 1):
            try:
                return self._attempt(snapshot, paths)
            except (_UnsafePath, _StateError):
                raise
            except (DownloadError, OSError, URLError, http.client.HTTPException) as exc:
                last_error = exc
                if isinstance(exc, _Restart):
                    self._reset(paths)
                if attempt < self.options.retries:
                    time.sleep(min(self.options.retry_delay * (2 ** min(attempt, 10)), 60.0))
        raise DownloadError(
            "Download failed after {} attempt(s): {}".format(self.options.retries + 1, last_error)
        ) from last_error

    def _receipt_valid(self, snapshot, receipt, current):
        if (not receipt or type(receipt.get("version")) is not int or receipt.get("version") != 1
                or receipt.get("source") != snapshot.url
                or receipt.get("filename") != snapshot.filename
                or not current or current.st_size <= 0
                or receipt.get("stat") != _identity(current)):
            return False
        try:
            self._resolved(snapshot, receipt["resolved_url"])
        except (KeyError, ValueError, TypeError, DownloadError):
            return False
        return True

    def _verify_existing(self, snapshot, paths):
        receipt = _record(paths, "complete")
        current = _regular_stat(paths.final)
        if not self._receipt_valid(snapshot, receipt, current):
            raise DownloadError(
                "Existing archive has no matching download receipt; move it aside "
                "or choose another output directory: " + str(paths.final)
            )
        _sync_directory(paths.final.parent)
        # Recover the harmless state left by a crash immediately after rename.
        _update_record(paths, "partial", None)
        if self.progress:
            self.progress(snapshot, current.st_size, current.st_size, 0.0)

    def _reset(self, paths):
        # Removing the identity record first makes a crash during cleanup safe.
        _update_record(paths, "partial", None)
        _unlink_regular(paths.part)

    def _migrate(self, snapshot, paths):
        state = _read_state(paths.state)
        has_metadata = _regular_stat(paths.legacy_metadata) is not None
        has_receipt = _regular_stat(paths.legacy_receipt) is not None
        if not has_metadata and not has_receipt:
            return
        if snapshot.filename in state["files"]:
            # A current central entry is authoritative, including absent fields.
            # Never resurrect its older partial/complete fields from legacy JSON.
            _sync_directory(paths.state.parent)
            _unlink_regular(paths.legacy_metadata)
            _unlink_regular(paths.legacy_receipt)
            return
        metadata = _load_json(paths.legacy_metadata) if has_metadata else None
        receipt = _load_json(paths.legacy_receipt) if has_receipt else None
        partial = _regular_stat(paths.part)
        final = _regular_stat(paths.final)
        # Migrate a record using its own source. A request to another node must
        # still reject reuse, while preserving the original source's receipt.
        def recorded_snapshot(record):
            try:
                original = snapshot_from_url(record["source"])
            except (KeyError, TypeError, ValueError):
                return None
            return original if original.filename == paths.final.name else None

        partial_source = recorded_snapshot(metadata)
        complete_source = recorded_snapshot(receipt)
        valid_partial = partial_source is not None and self._partial_valid(partial_source, metadata, partial)
        valid_receipt = complete_source is not None and self._receipt_valid(complete_source, receipt, final or partial)
        entry = {}
        if valid_partial:
            entry["partial"] = metadata
        if valid_receipt:
            entry["complete"] = receipt
        if entry:
            _select_current_pair(state, snapshot.filename)
            state["files"][snapshot.filename] = entry
        # Even an invalid old sidecar is removed only after a successful central
        # state write. A failed migration retains every legacy recovery artifact.
        _write_state(paths.state, state)
        _unlink_regular(paths.legacy_metadata)
        _unlink_regular(paths.legacy_receipt)
        if has_metadata and not valid_partial and final is None:
            _unlink_regular(paths.part)

    def _resolved(self, snapshot, url):
        try:
            resolved = snapshot_from_url(url)
        except (TypeError, ValueError) as exc:
            raise _Restart("Redirect does not identify a supported snapshot archive") from exc
        if (resolved.filename, resolved.slot, resolved.base_slot) != (
                snapshot.filename, snapshot.slot, snapshot.base_slot):
            raise _Restart("Redirect changed the requested snapshot archive identity")
        return resolved.url

    def _partial_valid(self, snapshot, metadata, partial):
        if partial is None or partial.st_size == 0:
            return False
        valid = (
            metadata and type(metadata.get("version")) is int and metadata.get("version") == 1
            and metadata.get("source") == snapshot.url
            and metadata.get("filename") == snapshot.filename
            and _positive_integer(metadata.get("total_size"))
            and partial.st_size <= metadata["total_size"]
            and metadata.get("partial_device") == partial.st_dev
            and metadata.get("partial_inode") == partial.st_ino
        )
        if valid:
            try:
                self._resolved(snapshot, metadata.get("resolved_url"))
            except (DownloadError, TypeError, ValueError):
                valid = False
        if valid:
            name = metadata.get("validator_name")
            value = metadata.get("validator_value")
            if name == "ETag":
                valid = isinstance(value, str) and bool(_STRONG_ETAG.fullmatch(value))
            elif name == "Last-Modified":
                # Validate persisted If-Range values before placing them in headers.
                from email.message import Message
                headers = Message()
                date = metadata.get("validator_date")
                if (isinstance(value, str) and isinstance(date, str)
                        and "\r" not in value + date and "\n" not in value + date):
                    headers[name] = value
                    headers["Date"] = date
                    valid = _validator(headers) == (name, value)
                else:
                    valid = False
            else:
                valid = False
        return bool(valid)

    def _resume(self, snapshot, paths):
        partial = _regular_stat(paths.part)
        metadata = _record(paths, "partial")
        if partial is None or partial.st_size == 0:
            return 0, None
        if not self._partial_valid(snapshot, metadata, partial):
            self._reset(paths)
            return 0, None
        return partial.st_size, metadata

    def _attempt(self, snapshot, paths):
        if _regular_stat(paths.final) is not None:
            self._verify_existing(snapshot, paths)
            return paths.final
        offset, metadata = self._resume(snapshot, paths)
        headers = {}
        if offset:
            headers = {"Range": "bytes={}-".format(offset), "If-Range": metadata["validator_value"]}
        try:
            response = open_url(snapshot.url, timeout=self.options.timeout, headers=headers)
        except HTTPError as exc:
            with exc:
                if exc.code == 416 and offset:
                    self._confirm_complete(snapshot, paths, offset, metadata, exc)
                    return paths.final
                raise
        with response:
            resolved = self._resolved(snapshot, response.geturl())
            encoding = _one_header(response.headers, "Content-Encoding", "identity").strip().lower()
            if encoding != "identity":
                raise _Restart("Archive response uses unsupported Content-Encoding: " + encoding)
            media_type = _one_header(response.headers, "Content-Type", "").split(";", 1)[0].strip().lower()
            if (media_type in ("text/html", "application/xhtml+xml", "application/json", "text/json")
                    or media_type.endswith("+json")):
                raise _Restart("Archive response contains an HTML or JSON document: " + media_type)
            status = response.getcode()
            if status == 200:
                length = _content_length(response.headers)
                if _one_header(response.headers, "Content-Range") is not None:
                    raise _Restart("Full response unexpectedly includes Content-Range")
                offset = 0
                total = length
                validator_name, validator_value = _validator(response.headers)
                metadata = {
                    "version": 1, "source": snapshot.url, "resolved_url": resolved,
                    "filename": snapshot.filename, "total_size": total,
                    "validator_name": validator_name, "validator_value": validator_value,
                    "validator_date": (_one_header(response.headers, "Date")
                                       if validator_name == "Last-Modified" else None),
                }
            elif status == 206 and offset:
                match = _RANGE.fullmatch(_one_header(response.headers, "Content-Range", ""))
                if (not match or resolved != metadata["resolved_url"]
                        or _validator(response.headers) != (metadata["validator_name"], metadata["validator_value"])):
                    raise _Restart("Resume response changed identity or has an invalid Content-Range")
                start, end, total = map(int, match.groups())
                length = _content_length(response.headers)
                if (start != offset or end < start or end >= total
                        or total != metadata["total_size"] or length != end - start + 1):
                    raise _Restart("Resume response has an inconsistent Content-Range or length")
            else:
                raise _Restart("Unexpected archive HTTP status: " + str(status))
            if shutil.disk_usage(paths.final.parent).free < total - offset:
                raise DownloadError("Insufficient free disk space for the remaining archive")
            flags = os.O_RDWR | os.O_CREAT
            if not offset:
                flags |= os.O_TRUNC
            fd = _open_regular(paths.part, flags)
            try:
                current = os.fstat(fd)
                if offset and (current.st_size != offset
                               or current.st_dev != metadata["partial_device"]
                               or current.st_ino != metadata["partial_inode"]):
                    raise _Restart("Partial archive changed while opening it")
                if not offset:
                    os.fsync(fd)
                    metadata["partial_device"] = current.st_dev
                    metadata["partial_inode"] = current.st_ino
                    _update_record(paths, "partial", metadata)
                os.lseek(fd, offset, os.SEEK_SET)
                self._stream(snapshot, response, fd, offset, length, total)
                os.fsync(fd)
                result = os.fstat(fd)
                if result.st_size != total or result.st_size == 0:
                    raise DownloadError("Archive length does not match its declared total size")
                self._finalize(snapshot, paths, metadata, result)
            finally:
                os.close(fd)
        return paths.final

    def _stream(self, snapshot, response, fd, offset, length, total):
        started = window_start = time.monotonic()
        received = window_bytes = 0
        # Inspect archive beginnings only. Resumed responses start in arbitrary
        # compressed data, so obtain their prefix from the existing local file.
        prefix = os.pread(fd, 512, 0)
        if _error_document(prefix):
            raise _Restart("Archive begins with an HTML or JSON error document")
        if self.progress:
            self.progress(snapshot, offset, total, 0.0)
        while True:
            if self.options.min_speed:
                remaining = self.options.speed_window - (time.monotonic() - window_start)
                connection = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
                if connection is not None:
                    connection.settimeout(min(self.options.timeout, max(remaining, 0.000001)))
            try:
                # read() can wait for a whole MiB while a peer trickles bytes.
                # read1() returns available data after at most one socket read.
                chunk = response.read1(self.options.chunk_size)
            except (socket.timeout, TimeoutError) as exc:
                elapsed = time.monotonic() - window_start
                if (self.options.min_speed and elapsed >= self.options.speed_window
                        and window_bytes / max(elapsed, 1e-9) < self.options.min_speed):
                    raise DownloadError("Download speed is below minimum {:.0f} B/s".format(
                        self.options.min_speed)) from exc
                raise
            if not chunk:
                break
            if received + len(chunk) > length:
                raise _Restart("Archive response exceeded its declared length")
            if len(prefix) < 512:
                prefix += chunk[:512 - len(prefix)]
                if _error_document(prefix):
                    raise _Restart("Archive begins with an HTML or JSON error document")
            view = memoryview(chunk)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("Could not write archive data")
                view = view[written:]
            received += len(chunk)
            window_bytes += len(chunk)
            now = time.monotonic()
            elapsed = now - window_start
            if self.options.min_speed and elapsed >= self.options.speed_window:
                speed = window_bytes / elapsed
                if speed < self.options.min_speed:
                    raise DownloadError("Download speed {:.0f} B/s is below minimum {:.0f} B/s".format(
                        speed, self.options.min_speed))
                window_start, window_bytes = now, 0
            if self.progress:
                self.progress(snapshot, offset + received, total, received / max(now - started, 1e-9))
        if received != length or offset + received != total:
            raise DownloadError("Incomplete archive: received {} of {} bytes".format(offset + received, total))

    def _confirm_complete(self, snapshot, paths, offset, metadata, response):
        resolved = self._resolved(snapshot, response.geturl())
        match = _UNSATISFIED_RANGE.fullmatch(_one_header(response.headers, "Content-Range", ""))
        if (not match or int(match.group(1)) != offset or offset != metadata["total_size"]
                or resolved != metadata["resolved_url"]
                or _validator(response.headers) != (metadata["validator_name"], metadata["validator_value"])
                or _one_header(response.headers, "Content-Encoding", "identity").strip().lower() != "identity"):
            raise _Restart("HTTP 416 did not confirm the complete archive's identity and size")
        fd = _open_regular(paths.part, os.O_RDONLY)
        try:
            current = os.fstat(fd)
            if (current.st_size != offset or current.st_dev != metadata["partial_device"]
                    or current.st_ino != metadata["partial_inode"]):
                raise _Restart("Partial archive changed before completion confirmation")
            os.fsync(fd)
            self._finalize(snapshot, paths, metadata, current)
        finally:
            os.close(fd)
        if self.progress:
            self.progress(snapshot, offset, offset, 0.0)

    def _finalize(self, snapshot, paths, metadata, result):
        _regular_stat(paths.state)
        current = _regular_stat(paths.part)
        if current is None or _identity(current) != _identity(result):
            raise _Restart("Partial archive changed before publication")
        if _regular_stat(paths.final) is not None:
            raise _UnsafePath("Archive appeared during download; refusing to overwrite: " + str(paths.final))
        receipt = {
            "version": 1, "source": snapshot.url, "resolved_url": metadata["resolved_url"],
            "filename": snapshot.filename, "stat": _identity(result),
        }
        _update_record(paths, "complete", receipt)
        if _regular_stat(paths.final) is not None:
            raise _UnsafePath("Archive appeared during download; refusing to overwrite: " + str(paths.final))
        os.replace(paths.part, paths.final)
        _sync_directory(paths.final.parent)
        # The complete archive and receipt are already published; a stale partial
        # field is harmless if cleanup fails (for example, a read-only mount).
        try:
            _update_record(paths, "partial", None)
        except OSError:
            pass
