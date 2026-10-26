#!/usr/bin/env python3
"""Build reproducible source and zipapp archives without installing anything."""

import gzip
import hashlib
import io
from pathlib import Path
import re
import tarfile
import zipfile


ROOT = Path(__file__).resolve().parents[1]
VERSION_TEXT = (ROOT / "solana_snapshot_downloader" / "__init__.py").read_text(encoding="utf-8")
VERSION = re.search(r'^__version__ = "([0-9]+\.[0-9]+\.[0-9]+)"$', VERSION_TEXT, re.M).group(1)
NAME = "solana-snapshot-downloader-" + VERSION
ENTRY = b'''import sys
if sys.version_info < (3, 9):
    sys.exit("Python 3.9 or newer is required")
from solana_snapshot_downloader.cli import main
sys.exit(main())
'''


def source_files():
    files = [ROOT / name for name in (
        "snapshot_downloader.py", "README.md", "README.en.md", "LICENSE", ".gitignore", "CHANGELOG.md",
    )]
    for folder in ("solana_snapshot_downloader", "tests", "tools", ".github"):
        files.extend(path for path in (ROOT / folder).rglob("*")
                     if path.is_file() and "__pycache__" not in path.parts
                     and path.suffix in (".py", ".yml", ".yaml"))
    return sorted(files, key=lambda path: path.relative_to(ROOT).as_posix())


def build():
    output = ROOT / "dist"
    output.mkdir(exist_ok=True)
    app = output / (NAME + ".pyz")
    entries = {"__main__.py": ENTRY}
    for path in sorted((ROOT / "solana_snapshot_downloader").glob("*.py")):
        entries[path.relative_to(ROOT).as_posix()] = path.read_bytes()
    entries["LICENSE"] = (ROOT / "LICENSE").read_bytes()
    with app.open("wb") as stream:
        stream.write(b"#!/usr/bin/env python3\n")
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in sorted(entries.items()):
                entry = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
                entry.compress_type = zipfile.ZIP_DEFLATED
                entry.create_system = 3
                entry.external_attr = 0o100644 << 16
                archive.writestr(entry, data)
    app.chmod(0o755)
    source = output / (NAME + "-source.tar.gz")
    with source.open("wb") as stream:
        with gzip.GzipFile(filename="", mode="wb", fileobj=stream, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                for path in source_files():
                    data = path.read_bytes()
                    entry = tarfile.TarInfo(NAME + "/" + path.relative_to(ROOT).as_posix())
                    entry.size = len(data)
                    entry.mode = 0o644
                    entry.mtime = 0
                    archive.addfile(entry, io.BytesIO(data))
    checksums = output / "SHA256SUMS"
    checksums.write_text("".join(
        hashlib.sha256(path.read_bytes()).hexdigest() + "  " + path.name + "\n"
        for path in sorted((app, source))
    ), encoding="utf-8")
    for path in (app, source, checksums):
        print(path.relative_to(ROOT))


if __name__ == "__main__":
    build()
