# Solana Snapshot Downloader

[中文](README.md)

A Python standard-library tool to discover Solana snapshot nodes, probe candidates and download a full snapshot followed by its matching incremental snapshot. It supports speed sampling, recovery from slow downloads and conditional resume.

Targets **Linux amd64 / arm64 with Python 3.9 or newer**. No third-party Python packages, `pip`, virtual environment, `aria2` or `wget` are required at runtime.

## Download the source and run

Check your interpreter first:

```bash
python3 --version
```

You need Python 3.9 or newer. Python is preinstalled on some Linux systems. If missing, install it with your distribution's package manager, for example `sudo apt install python3` on Ubuntu/Debian or `sudo dnf install python3` on Fedora, then check the version again.

With Git:

```bash
git clone https://github.com/MevYu/solana-snapshot-downloader.git
cd solana-snapshot-downloader
python3 snapshot_downloader.py --output /mnt/ledger/snapshots
```

Without Git, open the [repository](https://github.com/MevYu/solana-snapshot-downloader), choose **Code → Download ZIP**, extract the entire archive and open a terminal in the extracted project directory. Then run the same `python3 snapshot_downloader.py` command. Keep the `solana_snapshot_downloader/` package alongside the script; downloading only the entry script is insufficient.

Choose an output directory with enough free space that your user can write to. For example:

```bash
python3 snapshot_downloader.py --output "$HOME/solana-snapshots"
```

The module entry point also works from the project directory:

```bash
python3 -m solana_snapshot_downloader --output /mnt/ledger/snapshots
python3 snapshot_downloader.py --help
```

Stop other downloads or validator snapshot fetches using the same output directory. Use a directory you own or have permission to write to; setting permissions to `777` is unnecessary.

## Examples

List candidates without downloading snapshot data or creating the snapshot output directory:

```bash
python3 snapshot_downloader.py --list
python3 snapshot_downloader.py --list --json > candidates.json
```

Candidate tables and JSON go to stdout; logs and download progress go to stderr.

Select a network or download only a full snapshot:

```bash
python3 snapshot_downloader.py --network testnet --output "$HOME/testnet-snapshots"
python3 snapshot_downloader.py --full-only --max-slot-age 50000 --output /mnt/ledger/snapshots
```

With `--full-only`, freshness is checked against the full snapshot's own slot. It can be older than the latest incremental snapshot, so the default 2,500-slot limit may exclude candidates. Increase the limit as needed, for example to `50000` above; `--max-slot-age 0` disables it.

Probe specific nodes instead of discovering them. Replace these example addresses. RPC is still used to obtain the current slot:

```bash
python3 snapshot_downloader.py \
  --node http://your-node.example:8899 \
  --node http://another-node.example:8899 \
  --output /mnt/ledger/snapshots
```

Use `--rpc URL` repeatedly to provide discovery seeds or fallback RPC endpoints, replacing the built-in RPC. `--network` only selects a default RPC; it does not verify the cluster of custom RPC endpoints or nodes. You must ensure they belong to the same intended network:

```bash
python3 snapshot_downloader.py \
  --network mainnet \
  --rpc https://your-rpc.example \
  --rpc https://backup-rpc.example \
  --output /mnt/ledger/snapshots
```

Require 10 MB/s during sampling and sustained downloads, using a 30-second speed window:

```bash
python3 snapshot_downloader.py \
  --min-speed 10 --speed-window 30 \
  --output /mnt/ledger/snapshots
```

The default speed sample covers up to five candidates for two seconds each, capped at 16 MiB per candidate. `--speed-test-count 0` disables sampling. Sampled speeds do not guarantee download throughput; node load, network conditions and disk performance can change.

## Options

| Option | Default | Purpose |
| --- | --- | --- |
| `--output PATH` | `snapshots` | Snapshot directory, relative to the current working directory |
| `--network mainnet\|testnet\|devnet` | `mainnet` | Default RPC network; does not verify custom endpoint clusters |
| `--rpc URL` | Built-in network endpoints | Repeatable discovery seed / fallback RPC; replaces the built-in RPC |
| `--node HTTP_URL_OR_IP:PORT` | Discover nodes | Repeatable direct node probe; skips discovery but still uses RPC for the current slot |
| `--workers N` | `32` | Concurrent node probes, from `1` to `128` |
| `--timeout SECONDS` | `10` | Network request timeout |
| `--max-slot-age N` | `2500` | Maximum snapshot lag behind the current slot; `0` disables the bound |
| `--full-only` | Off | Download only the full snapshot; freshness uses its slot |
| `--list` | Off | Discover and probe only; no snapshot download or output directory creation |
| `--json` | Off | JSON output with `--list` |
| `--speed-test-count N` | `5` | Number of candidates to speed-test; `0` disables sampling |
| `--speed-test-seconds SECONDS` | `2` | Sample duration per candidate, also limited to 16 MiB |
| `--min-speed MBPS` | `0` | Minimum sample and sustained download speed; decimal MB/s (1 MB = 1,000,000 bytes); `0` disables it |
| `--speed-window SECONDS` | `30` | Sustained download speed window |
| `--retries N` | `3` | Additional retries per file after the first attempt; up to 4 attempts by default |
| `--retry-delay SECONDS` | `2` | Initial exponential retry delay, capped at 60 seconds; also the fixed delay between scan rounds |
| `--attempts N` | `3` | Discovery, probing and download rounds |
| `--verbose` | Off | Detailed logging |
| `--version` | — | Print the version |

## Files and resume

The full snapshot is downloaded before its matching incremental snapshot. **Ctrl+C** exits with status `130` and preserves partial data. Run the same command again to attempt a resume.

Resume requires HTTP Range support and unchanged source, archive and identity metadata. Identity must use a strong ETag, or a sufficiently old Last-Modified relative to Date when no ETag exists. Otherwise the file is downloaded again; partial bytes are never combined across nodes. HTTP errors, error pages, truncated responses and identity mismatches are not promoted to completed archives. A valid Content-Length is required to verify transfer completeness.

The output directory can contain:

| File | Purpose |
| --- | --- |
| `snapshot-….tar.zst`, `incremental-snapshot-….tar.zst`, etc. | Completed archives, preserving the source filename and compression format |
| `<archive>.part` | Partial download |
| `<archive>.part.json` | Resume metadata |
| `<archive>.complete.json` | Completion receipt used when checking whether a previous download can be reused |
| `.snapshot-downloader.lock` | Directory lock; an empty file remaining after exit is normal |

The program can reuse its own completed downloads from the same URL using their receipts. It does not automatically reuse same-named archives across nodes or combine their partial bytes. Existing archives without matching receipts are not overwritten. Select the original node again or use a separate empty output directory in that case. Do not delete the lock while a download is running or let another program modify active downloads and metadata.

This tool does not prune old snapshots, extract archives or stop/restart a Solana validator.

## Verification limits

Completion checks cover HTTP transfer length and resource identity; they **do not prove the cryptographic validity of Solana state**. The hash in a snapshot filename is not the archive's SHA-256 checksum. Choose nodes according to your trust requirements. The Solana validator performs final loading and state validation.

## Releases and development

Check [Releases](https://github.com/MevYu/solana-snapshot-downloader/releases) for versions that have actually been published. You can run the source directly as described above. A release `.pyz`, when available, is a Python zipapp and still requires Python 3.9 or newer; it is not a standalone binary.

Once a version is published, use your browser to download its `.pyz`, source `.tar.gz` and `SHA256SUMS` from Releases into the same directory. Verify the files before running. This example uses `0.1.1` filenames; availability depends on the Releases page:

```bash
sha256sum -c SHA256SUMS
python3 solana-snapshot-downloader-0.1.1.pyz --output /mnt/ledger/snapshots
```

From the source directory:

```bash
python3 -m unittest discover -s tests -v
python3 tools/build_release.py
```

For version `0.1.1`, the build generates `dist/solana-snapshot-downloader-0.1.1.pyz`, `dist/solana-snapshot-downloader-0.1.1-source.tar.gz` and `dist/SHA256SUMS`. The zipapp works across amd64 and arm64 without separate builds. `SHA256SUMS` checks release-file consistency, not Solana snapshot state. After building:

```bash
cd dist
sha256sum -c SHA256SUMS
python3 solana-snapshot-downloader-0.1.1.pyz --output /mnt/ledger/snapshots
```

## License and acknowledgments

[MIT License](LICENSE). The functionality was inspired by [solana-snapshot-finder](https://github.com/coderigo/solana-snapshot-finder). This project is an independent implementation and does not copy that project's code.
