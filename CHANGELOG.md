# Changelog

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
