"""Verify the real WebView2 renderer, adjacent data and shutdown in isolation."""

import os
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / "dist" / "pixiv-gallery.exe"
SOURCE = "--source" in sys.argv
if os.name != "nt" or (not SOURCE and not EXE.is_file()):
    raise SystemExit("Build pixiv-gallery.exe on Windows before running this check.")

temporary = tempfile.TemporaryDirectory()
directory = Path(temporary.name)
process = None
try:
    if SOURCE:
        for name in ("desktop.py", "server.py", "index.html"):
            shutil.copy2(ROOT / name, directory / name)
        shutil.copytree(ROOT / "assets", directory / "assets")
        command = [sys.executable, str(directory / "desktop.py"), "--smoke-test"]
    else:
        target = directory / EXE.name
        shutil.copy2(EXE, target)
        command = [str(target), "--smoke-test"]
    env = {**os.environ, "PIXIV_PHPSESSID": "", "PYTHONIOENCODING": "cp1252"}
    process = subprocess.Popen(
        command, cwd=directory, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW,
    )
    output, _ = process.communicate(timeout=55)
    result_path = directory / "webview-smoke.json"
    result = json.loads(result_path.read_text(encoding="utf-8")) if result_path.is_file() else {}
    assert process.returncode == 0 and result.get("ok"), (result, output.decode("utf-8", "replace")[-3000:])
    assert (directory / "data" / "library.sqlite3").is_file()
    with socket.socket() as probe:
        assert probe.connect_ex(("127.0.0.1", result["port"])) != 0, "Service survived window close"
    print("WebView2 page, icon, tag editor, native history and U shortcut; adjacent data and shutdown passed")
finally:
    if process is not None and process.poll() is None:
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       capture_output=True, check=False)
        process.wait(timeout=10)
    for attempt in range(25):
        try:
            temporary.cleanup()
            break
        except PermissionError:
            if attempt == 24:
                raise
            time.sleep(0.2)
