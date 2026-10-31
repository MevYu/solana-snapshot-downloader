# Changelog

## 0.1.4

- Try the legacy `.bz2` snapshot alias when the `.zst` alias still returns HTTP 405 or 501 after the single-byte GET fallback, including incremental snapshot refreshes.
- Preserve probe failures when no alias succeeds; only HTTP 404 for both aliases means an incremental snapshot is absent.
- Add regression coverage for alias fallback, successful `.zst` GET probes, and HTTP error handling.

## 0.1.3

- Show live search, speed-test and download progress, with byte totals, speed and ETA.
- Keep redirected logs readable and JSON output separate from progress.
- Simplify the default help and getting-started documentation; use `--help-all` for advanced options.
- Store only the current full/incremental download state in one `.download-state.json`, with on-demand migration of older per-file records.

## 0.1.2

Rename the Python package to `downloader`. Run from source with `python3 snapshot_downloader.py` or `python3 -m downloader`.

## 0.1.1

Release packaging revision. The downloader behavior is unchanged from 0.1.0.

## 0.1.0

Initial independent implementation for Linux servers using Python 3.9+ and only the standard library.

- Discover public Solana RPC nodes or use explicitly supplied snapshot nodes.
- Resolve relative and absolute snapshot redirects, including redirects to another port.
- Select compatible full and incremental snapshots with configurable age limits and bounded speed sampling.
- Stream downloads to disk with bounded memory, retries, source-specific resume, and download progress.
- Verify HTTP resource identity, response ranges and transfer length before publishing completed files.
- Keep partial downloads on interruption, lock the output directory, and refuse to overwrite unverified existing archives.
- Include Chinese and English source-install instructions and a universal Python zipapp for Linux amd64 and arm64.

This tool checks transfer completeness, not the cryptographic validity of Solana state. Snapshot validation remains the validator's responsibility. The zipapp requires Python; no pip packages, aria2, or wget are required.
