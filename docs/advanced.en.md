# Advanced guide

[Back to quick start](../README.en.md) · [中文](advanced.md)

Requires Linux amd64 / arm64 and Python 3.9+, with no third-party Python packages, virtual environment, `aria2` or `wget`.
If Python is missing, use your distribution's package manager, for example `sudo apt install python3` on Ubuntu/Debian or `sudo dnf install python3` on Fedora, then check `python3 --version`.
From the source directory, `python3 -m downloader` accepts the same arguments as `python3 snapshot_downloader.py`.

## Options

`--help` shows common options; `--help-all` shows every option. Existing advanced options remain directly usable.

| Option | Default | Purpose |
| --- | --- | --- |
| `-o PATH`, `--output PATH` | `snapshots` | Snapshot directory, relative to the current working directory |
| `--network mainnet\|testnet\|devnet` | `mainnet` | Default RPC network; does not verify custom endpoint clusters |
| `--rpc URL` | Built-in network endpoint | Repeatable discovery seed / fallback RPC; replaces the built-in RPC |
| `--node HTTP_URL_OR_IP:PORT` | Discover nodes | Repeatable direct probe; skips discovery but still uses RPC for the current slot |
| `--workers N` | `32` | Concurrent node probes, from `1` to `128` |
| `--timeout SECONDS` | `10` | Network request timeout |
| `--max-slot-age N` | `2500` | Maximum snapshot lag behind the current slot; `0` disables the bound |
| `--full-only` | Off | Download only the full snapshot; freshness uses its slot |
| `--list` | Off | Discover and probe only; no snapshot download or output directory creation |
| `--json` | Off | JSON output with `--list` |
| `--speed-test-count N` | `5` | Number of candidates to speed-test; `0` disables sampling |
| `--speed-test-seconds SECONDS` | `2` | Sample duration per candidate, also limited to 16 MiB |
| `--min-speed MBPS` | `0` | Minimum sampled and sustained speed in decimal MB/s (1 MB = 1,000,000 bytes); `0` disables it |
| `--speed-window SECONDS` | `30` | Sustained download speed window |
| `--retries N` | `3` | Additional file retries after the first attempt; up to 4 attempts by default |
| `--retry-delay SECONDS` | `2` | Initial exponential retry delay, capped at 60 seconds; also the fixed delay between scan rounds |
| `--attempts N` | `3` | Discovery, probing and download rounds |
| `--verbose` | Off | Detailed logs and diagnostics |
| `-h`, `--help` | — | Show common options |
| `--help-all` | — | Show all options |
| `--version` | — | Print the version |

## Networks, full snapshots and speed

`--network` only selects a default RPC. Custom RPC endpoints and nodes are not checked for cluster membership; ensure they belong to the same intended network. Replace the example addresses:

```bash
python3 snapshot_downloader.py --network testnet -o "$HOME/testnet-snapshots"
python3 snapshot_downloader.py --rpc https://your-rpc.example --rpc https://backup-rpc.example
```

By default, the full snapshot is downloaded first, followed by a matching incremental snapshot when available. With `--full-only`, freshness uses the full snapshot's own slot. It may be older than the latest incremental snapshot, so the default 2,500-slot limit can exclude candidates. Increase it as needed:

```bash
python3 snapshot_downloader.py --full-only --max-slot-age 50000 -o /mnt/ledger/snapshots
```

The default speed sample covers up to five candidates for two seconds each, capped at 16 MiB each. `--speed-test-count 0` disables sampling. To reject slow sources, for example below 10 MB/s:

```bash
python3 snapshot_downloader.py --min-speed 10 --speed-window 30 -o /mnt/ledger/snapshots
```

Node load, network conditions and disk performance affect actual speed; sampling does not guarantee subsequent throughput.

## Progress and JSON output

Progress and logs go to **stderr**. On a terminal (TTY), the current line refreshes in place; redirection produces periodic plain-text updates.
Search shows checked and usable node counts, speed testing shows candidate progress and speed, and downloads show full/incremental progress bars, percentages, sizes, speeds and ETA.

Candidate tables from `--list` and JSON from `--list --json` go to **stdout**, without progress text mixed in:

```bash
python3 snapshot_downloader.py --list --json > candidates.json
```

This downloads no snapshot data and creates no snapshot output directory; the shell creates the redirected `candidates.json` file.

## Files and recovery limits

Use a writable directory with enough free space; `chmod 777` is unnecessary. Stop other downloads or validator snapshot fetches using the same directory.
**Ctrl+C** exits with status `130` and preserves partial data. Run the same command again to attempt a resume.

Resume requires HTTP Range support and unchanged source, archive and identity metadata. Identity must use a strong ETag, or a sufficiently old Last-Modified relative to Date when no ETag exists. Otherwise the file is downloaded again; partial bytes are never combined across nodes.
A valid Content-Length is required. HTTP errors, error pages, truncated responses and identity mismatches are not promoted to completed archives.

| File | Purpose |
| --- | --- |
| `snapshot-….tar.zst`, `incremental-snapshot-….tar.zst`, etc. | Completed archives, preserving the source filename and compression format |
| `<archive>.part` | Partial download |
| `.download-state.json` | Current task state for at most one full and one incremental snapshot; no download history |
| `.snapshot-downloader.lock` | Directory lock; an empty file remaining after exit is normal |

An active download uses an archive `.part` file; completion leaves the final archive. Keep `.download-state.json`: resume and completed-file reuse depend on its records.
Selecting new snapshots replaces the old state records. The program does not automatically delete completed archives or scan user directories to build a download history.
Older per-file `.part.json` / `.complete.json` records are migrated when their archive is used. The program persists them in the shared state before deleting the old files, preserving resume information without manual cleanup.

Matching completion records in the current task allow the program to reuse its own completed files from the same URL, not same-named archives from other nodes. Existing archives without matching records are not overwritten; use a separate empty output directory.
Do not delete the lock while running or let other programs modify downloads and metadata. This tool does not prune snapshots, extract archives or stop/restart a validator.

## Verification limits

Completion checks cover HTTP transfer length and resource identity; they **do not prove the cryptographic validity of Solana state**. The hash in a filename is not the archive's SHA-256 checksum.
Choose nodes according to your trust requirements. The Solana validator performs final loading and state validation.

## Release packages and development

Check [Releases](https://github.com/MevYu/solana-snapshot-downloader/releases) for published versions. A `.pyz` is a Python zipapp shared across amd64 / arm64 and still requires Python 3.9+.
Use your browser to download the same version's `.pyz`, source `.tar.gz` and `SHA256SUMS` into one directory, then verify and run.
Replace the `<VERSION>` placeholder below with the version in the downloaded filename:

```bash
sha256sum -c SHA256SUMS
python3 "solana-snapshot-downloader-<VERSION>.pyz" --output /mnt/ledger/snapshots
```

Test and build from the source directory without installing dependencies:

```bash
python3 -m unittest discover -s tests -v
python3 tools/build_release.py
```

Outputs are `dist/solana-snapshot-downloader-<VERSION>.pyz`, `dist/solana-snapshot-downloader-<VERSION>-source.tar.gz` and `dist/SHA256SUMS`.
Enter `dist` to use the verification and run commands above. `SHA256SUMS` checks release-file consistency, not snapshot state.

[MIT License](../LICENSE). Inspired by [solana-snapshot-finder](https://github.com/coderigo/solana-snapshot-finder); this is an independent implementation with no code copied from that project.
