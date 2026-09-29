"""Build the Windows one-file launcher. Requires PyInstaller on the build machine."""

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if os.name != "nt":
    raise SystemExit("Build the Windows EXE on Windows.")

command = [
    sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile",
    "--windowed", "--noupx", "--name", "pixiv-gallery",
    "--icon", str(ROOT / "assets" / "app.ico"),
    "--add-data", f"{ROOT / 'index.html'}{os.pathsep}.",
    "--add-data", f"{ROOT / 'assets'}{os.pathsep}assets",
    "--distpath", str(ROOT / "dist"),
    "--workpath", str(ROOT / "build" / "pyinstaller"),
    "--specpath", str(ROOT / "build"),
    str(ROOT / "desktop.py"),
]
subprocess.run(command, check=True, cwd=ROOT)
output = ROOT / "dist" / "pixiv-gallery.exe"
if not output.is_file() or output.stat().st_size < 1_000_000:
    raise SystemExit("The Windows EXE was not created correctly.")
print(output)
