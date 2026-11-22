"""Extract Python test sources from the official pinned SQLGlot GitHub archive."""

import io
from pathlib import Path, PurePosixPath
import shutil
import tarfile
from urllib.request import urlopen


PINNED = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
DEST = Path("/opt/sqlglot")
URL = f"https://github.com/tobymao/sqlglot/archive/{PINNED}.tar.gz"
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024


def main():
    with urlopen(URL, timeout=90) as response:
        archive = response.read(MAX_ARCHIVE_BYTES + 1)
    if len(archive) > MAX_ARCHIVE_BYTES:
        raise ValueError("pinned archive exceeds size limit")

    prefix = f"sqlglot-{PINNED}"
    written = set()
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        for member in tar:
            parts = PurePosixPath(member.name).parts
            if not parts or parts[0] != prefix or ".." in parts:
                raise ValueError("unexpected archive path")
            if len(parts) < 3 or parts[1] != "tests" or not member.name.endswith(".py"):
                continue
            relative = PurePosixPath(*parts[1:])
            if not member.isfile() or relative in written:
                raise ValueError(f"invalid test source: {relative}")
            target = DEST.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with tar.extractfile(member) as source, target.open("wb") as destination:
                shutil.copyfileobj(source, destination)
            written.add(relative)

    if PurePosixPath("tests/test_optimizer.py") not in written:
        raise ValueError("pinned test sources are incomplete")
    (DEST / "SOURCE_COMMIT").write_text(PINNED + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
