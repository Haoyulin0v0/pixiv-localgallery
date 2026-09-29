"""Recommendations refresh each request and may repeat prior or saved artworks."""

import importlib.util
import tempfile
from pathlib import Path


spec = importlib.util.spec_from_file_location("gallery", Path(__file__).resolve().parents[1] / "server.py")
gallery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gallery)

with tempfile.TemporaryDirectory() as directory:
    gallery.DATA = Path(directory) / "data"
    gallery.IMAGES = gallery.DATA / "images"
    gallery.DATABASE = gallery.DATA / "library.sqlite3"
    gallery.SESSION_COOKIE = "12345_test_session"
    db = gallery.connection()
    db.execute("CREATE TABLE discovery_seen (user_id TEXT, pixiv_id TEXT, first_shown_at INTEGER)")
    db.execute("INSERT INTO discovery_seen VALUES ('12345','10000001',1)")
    db.execute("INSERT INTO artworks VALUES (?,?,?,?,?,?,?,?,?,?)",
               ("saved", "10000002", "Saved", "Artist", "42", "[]", "", "[]", 1, 1))
    db.commit()
    db.close()
    calls = []

    def recommendations(url):
        calls.append(url)
        return {"thumbnails": {"illust": [
            {"id": artwork_id, "userId": "42", "userName": "Artist", "title": "Example",
             "url": "", "tags": [], "pageCount": 1}
            for artwork_id in ("10000001", "10000001", "10000002")
        ]}}

    gallery.pixiv_json = recommendations
    for _ in range(2):
        assert [item["id"] for item in gallery.pixiv_feed("discover")["artworks"]] == ["10000001", "10000002"]
    assert len(calls) == 2

print("Recommendation refresh, repeat history and response deduplication passed")
