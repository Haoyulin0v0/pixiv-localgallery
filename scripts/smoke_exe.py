"""Start the frozen EXE and verify its embedded page and adjacent data folder."""

import os
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / "dist" / "pixiv-gallery.exe"
if os.name != "nt" or not EXE.is_file():
    raise SystemExit("Build pixiv-gallery.exe on Windows before running this check.")

with tempfile.TemporaryDirectory() as directory:
    target = Path(directory) / EXE.name
    shutil.copy2(EXE, target)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    env = {**os.environ, "GALLERY_OPEN_BROWSER": "0", "GALLERY_PORT": str(port),
           "PIXIV_PHPSESSID": ""}
    process = subprocess.Popen(
        [str(target)], cwd=directory, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    try:
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError(f"EXE exited early with code {process.returncode}")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=1) as response:
                    assert response.status == 200
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1) as response:
                    html = response.read().decode("utf-8")
                    assert "webFollowPendingId" in html
                assert (Path(directory) / "data" / "library.sqlite3").is_file()
                print("Frozen EXE served its embedded page and created data beside itself")
                break
            except (urllib.error.URLError, TimeoutError):
                time.sleep(0.3)
        else:
            raise AssertionError("Frozen EXE did not start its local service")
    finally:
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       capture_output=True, check=False)
        process.wait(timeout=10)
