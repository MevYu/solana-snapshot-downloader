# Solana Snapshot Downloader

[中文](README.md)

Find Solana snapshot nodes, test their speed and download a full snapshot with its matching incremental snapshot, with live progress.

**Linux amd64 / arm64, Python 3.9+, standard library only. No pip or external downloader required.**

## Download and run

Check `python3 --version`, then get the source and start:

```bash
git clone https://github.com/MevYu/solana-snapshot-downloader.git
cd solana-snapshot-downloader
python3 snapshot_downloader.py --output /mnt/ledger/snapshots
```

Choose a writable output directory with enough free space; `-o` is a short form of `--output`.
With no arguments, the program downloads mainnet snapshots into `./snapshots` under the current directory:

```bash
python3 snapshot_downloader.py
```

Without Git, open the [repository](https://github.com/MevYu/solana-snapshot-downloader), choose **Code → Download ZIP**,
extract it and run the same command from the project directory. Keep the entire project, including `downloader/`; the entry script alone is insufficient.

Stop other downloads or validator snapshot fetches using the same output directory before starting.

## What you will see

Progress covers node search, speed testing and downloading. **Illustrative output only; these numbers are not an actual download record:**

```text
[1/3] Searching nodes 120/800 | usable 20
[2/3] Testing speed 3/5 | 32.4 MB/s
[3/3] full        [====>.....] 45%  4.5/10.0 GiB   32.4 MB/s  ETA 03:02
[3/3] incremental [==>.......] 20%  200/1000 MiB   20.0 MB/s  ETA 00:42
```

In a terminal, the current progress line refreshes in place; redirected output uses periodic plain-text updates.
Press **Ctrl+C** to preserve partial data. Run the same command again to attempt a resume when the server supports it and the file identity is unchanged.
Keep the downloaded files and the current task's hidden state file, `.download-state.json`, in the download directory; it does not accumulate download history.

## Common commands

List available nodes without downloading snapshots:

```bash
python3 snapshot_downloader.py --list
```

Use your own node (replace this example address):

```bash
python3 snapshot_downloader.py --node http://your-node.example:8899
```

Show common options or all advanced options:

```bash
python3 snapshot_downloader.py --help
python3 snapshot_downloader.py --help-all
```

## More details

The [advanced guide](docs/advanced.en.md) covers all options, JSON output, resume limits, files, verification and release builds.
Check [Releases](https://github.com/MevYu/solana-snapshot-downloader/releases) for downloadable packages; a `.pyz` still requires Python 3.9+.

[MIT License](LICENSE). Inspired by [solana-snapshot-finder](https://github.com/coderigo/solana-snapshot-finder), with an independent implementation.
