"""Download the Motadata RUM browser SDK bundle into the storefront's static folder.

    python deploy/fetch_rum_sdk.py [dest]     (default: shopsim/storefront/vendor)

Fetches the pinned npm package, checks its sha512 integrity, and extracts the ready-made bundle
(bundle/motadata-rum.js and its lazy-loaded chunks). Standard library only, so the Docker build
needs nothing extra.
"""
from __future__ import annotations

import base64
import hashlib
import io
import sys
import tarfile
import urllib.request
from pathlib import Path

VERSION = "6.21.20"
URL = f"https://registry.npmjs.org/@motadata365/browser-rum/-/browser-rum-{VERSION}.tgz"
INTEGRITY = "sha512-LzO/68PAG+3KKZ+SAM5Ry7nhLiTblByPCdXbf9Eod3KjbCPO9eOm7JPteZ3j/8DwzFsHbnp8QajcTNv+7TyY0g=="


def main() -> None:
    dest = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent
                / "shopsim" / "storefront" / "vendor")
    with urllib.request.urlopen(URL, timeout=60) as resp:
        data = resp.read()
    digest = "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()
    if digest != INTEGRITY:
        sys.exit(f"integrity mismatch for {URL}: got {digest}")
    dest.mkdir(parents=True, exist_ok=True)
    written = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile() or not member.name.startswith("package/bundle/") or not member.name.endswith(".js"):
                continue
            rel = Path(member.name).relative_to("package/bundle")
            if ".." in rel.parts:
                continue
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(tar.extractfile(member).read())
            written += 1
    (dest / "VERSION").write_text(f"@motadata365/browser-rum {VERSION}\n")
    print(f"RUM SDK {VERSION}: {written} file(s) -> {dest}")


if __name__ == "__main__":
    main()
