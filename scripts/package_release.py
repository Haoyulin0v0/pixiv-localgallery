"""Build a code-only Windows download without local accounts or artwork data."""

import sys
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


ROOT = Path(__file__).resolve().parents[1]
VERSION = sys.argv[1] if len(sys.argv) > 1 else "v0.3.0"
if not VERSION.startswith("v") or not VERSION[1:].replace(".", "").isdigit():
    raise SystemExit("Version must look like v0.1.0")
OUTPUT = ROOT / "dist" / f"pixiv-gallery-{VERSION}.zip"
FILES = ("index.html", "server.py", "desktop.py", "requirements-desktop.txt", "start.bat", "README.md")

OUTPUT.parent.mkdir(exist_ok=True)
with ZipFile(OUTPUT, "w") as archive:
    for name in FILES:
        info = ZipInfo(f"pixiv-gallery/{name}", date_time=(1980, 1, 1, 0, 0, 0))
        info.compress_type = ZIP_DEFLATED
        archive.writestr(info, (ROOT / name).read_bytes())
with ZipFile(OUTPUT) as archive:
    if archive.testzip() is not None or archive.namelist() != [f"pixiv-gallery/{name}" for name in FILES]:
        raise SystemExit("Release archive verification failed")
print(OUTPUT)
