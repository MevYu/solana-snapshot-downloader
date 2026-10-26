"""Shared immutable snapshot metadata and URL validation."""

from dataclasses import dataclass
from typing import Optional
import re
from urllib.parse import unquote, urlsplit, urlunsplit


_ARCHIVE = re.compile(
    r"^(?P<kind>snapshot|incremental-snapshot)-(?P<first>[0-9]+)-"
    r"(?:(?P<second>[0-9]+)-)?(?P<hash>[1-9A-HJ-NP-Za-km-z]{32,64})"
    r"\.tar(?:\.(?:zst|bz2|gz))?$"
)


def http_url(value: str) -> str:
    """Accept only absolute HTTP(S) URLs, with no embedded credentials."""
    if not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("URL is empty or contains control characters")
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Expected an absolute http:// or https:// URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Credentials in URLs are not supported")
    # Accessing port also checks for malformed and out-of-range values.
    if parsed.port == 0:
        raise ValueError("URL port must be positive")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, ""))


def node_url(value: str) -> str:
    """Normalize a node endpoint; explicit HTTP(S) schemes are recommended."""
    if "://" not in value:
        value = "http://" + value
    return http_url(value)


@dataclass(frozen=True)
class Snapshot:
    url: str
    filename: str
    slot: int
    base_slot: Optional[int] = None

    @property
    def incremental(self) -> bool:
        return self.base_slot is not None


def snapshot_from_url(value: str) -> Snapshot:
    url = http_url(value)
    filename = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
    match = _ARCHIVE.fullmatch(filename)
    if not match:
        raise ValueError("URL does not identify a supported Solana snapshot archive")
    first = int(match.group("first"))
    second = match.group("second")
    if match.group("kind") == "incremental-snapshot":
        if second is None or int(second) <= first:
            raise ValueError("Incremental snapshot must have a slot after its base slot")
        return Snapshot(url, filename, int(second), first)
    if second is not None:
        raise ValueError("Full snapshot filename has an unexpected second slot")
    return Snapshot(url, filename, first)


@dataclass(frozen=True)
class Candidate:
    node: str
    full: Snapshot
    incremental: Optional[Snapshot] = None
    latency: float = 0.0

    @property
    def slot(self) -> int:
        return self.incremental.slot if self.incremental else self.full.slot

    @property
    def snapshots(self):
        return (self.full, self.incremental) if self.incremental else (self.full,)

    def __post_init__(self):
        if self.full.incremental:
            raise ValueError("Candidate full snapshot is incremental")
        if self.incremental and self.incremental.base_slot != self.full.slot:
            raise ValueError("Full and incremental snapshot base slots do not match")
