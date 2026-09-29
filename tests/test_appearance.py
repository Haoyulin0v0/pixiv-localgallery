"""Persistent appearance, wallpaper lifecycle and invalid-request regression checks."""

import base64
import gc
import importlib.util
import json
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("gallery", ROOT / "server.py")
gallery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gallery)
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=")

with tempfile.TemporaryDirectory() as temporary:
    gallery.DATA = Path(temporary) / "data"
    gallery.IMAGES = gallery.DATA / "images"
    gallery.DATABASE = gallery.DATA / "library.sqlite3"
    gallery.connection().close()
    server = gallery.LocalHTTPServer(("127.0.0.1", 0), gallery.Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    address = f"http://127.0.0.1:{server.server_port}"

    def request(path="/api/appearance", payload=None, data=None, headers=None):
        if payload is not None:
            data = json.dumps(payload).encode()
            headers = {"Content-Type": "application/json", **(headers or {})}
        req = urllib.request.Request(address + path, data=data, headers=headers or {})
        try:
            with urllib.request.urlopen(req) as response:
                body = response.read()
                return response.status, json.loads(body) if "application/json" in response.headers["Content-Type"] else body
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    try:
        defaults = request()[1]
        assert defaults["accent"] == "#cfb5f0" and not defaults["wallpaper_url"]
        settings = {"accent": "#8BD8C8", "background": "#102420", "overlay": 40}
        assert request(payload=settings)[0] == 200
        assert request()[1]["accent"] == "#8bd8c8"
        persisted = json.loads((gallery.DATA / "appearance.json").read_text())
        assert persisted["overlay"] == 40
        upload = "/api/appearance/wallpaper?accent=%238bd8c8&background=%23102420&overlay=40&filename=test.png"
        status, saved = request(upload, data=PNG)
        assert status == 200 and saved["wallpaper_url"]
        assert request(saved["wallpaper_url"])[1] == PNG
        wallpaper = gallery.DATA / "appearance" / saved["wallpaper"]
        assert wallpaper.is_file()
        # Changing colors preserves the wallpaper; invalid data leaves both untouched.
        assert request(payload={**settings, "overlay": 0})[1]["wallpaper"] == saved["wallpaper"]
        for invalid in ({"accent": "red"}, {"background": "url(file:///x)"}, {"overlay": 96},
                        {"overlay": True}, {"overlay": "60"}, [], {"remove_wallpaper": "true"}):
            assert request(payload=invalid)[0] == 400, invalid
        assert request(upload, data=b"not an image")[0] == 400
        assert request(upload, data=PNG, headers={"Content-Length": str(gallery.MAX_WALLPAPER+1)})[0] == 400
        assert request()[1]["wallpaper"] == saved["wallpaper"]
        assert request(payload=settings, headers={"Origin": "https://example.com"})[0] == 403
        assert request(payload=settings, headers={"Origin": address})[0] == 200
        # A failed commit must preserve the old settings and wallpaper without orphaning an upload.
        with patch.object(Path, "replace", side_effect=OSError("Simulated disk failure")):
            try:
                gallery.save_appearance(settings, PNG, "new.png")
                raise AssertionError("Expected a failed commit")
            except OSError:
                pass
        assert len(list((gallery.DATA / "appearance").iterdir())) == 1
        assert request()[1]["wallpaper"] == saved["wallpaper"]
        status, updated = request(upload, data=PNG)
        assert status == 200 and updated["wallpaper"] != saved["wallpaper"] and not wallpaper.exists()
        assert request(payload={**gallery.APPEARANCE_DEFAULTS, "remove_wallpaper": True})[1]["wallpaper"] == ""
        assert not list((gallery.DATA / "appearance").iterdir())
        assert request("/api/appearance/wallpaper")[0] == 404
        for content in ('null', '{broken', '{"wallpaper":"../../library.sqlite3"}'):
            (gallery.DATA / "appearance.json").write_text(content)
            assert request()[1] == defaults
        assert request("/api/artworks")[1] == {"artworks": []}
        assert request("/favicon.ico")[0] == 200
        assert request("/assets/app.svg")[0] == 200
    finally:
        server.shutdown()
        server.server_close()
        worker.join()
        gc.collect()  # SQLite context managers commit but do not close the connection.

print("Appearance persistence, upload/replacement/removal, validation, rollback and origin checks passed")
