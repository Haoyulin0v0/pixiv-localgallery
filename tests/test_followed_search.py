"""Full followed-artist indexing, retry, account isolation and tag search."""

import importlib.util
import json
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path
from threading import Thread
from http.server import ThreadingHTTPServer


spec = importlib.util.spec_from_file_location("gallery", Path(__file__).resolve().parents[1] / "server.py")
gallery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gallery)

with tempfile.TemporaryDirectory() as directory:
    gallery.DATA = Path(directory) / "data"
    gallery.IMAGES = gallery.DATA / "images"
    gallery.DATABASE = gallery.DATA / "library.sqlite3"
    gallery.SESSION_COOKIE = "123456_test_session"
    gallery.FOLLOWED_INDEX_DELAY = 0
    followed = ["42", "43"]
    fail_artist_once = [True]
    rate_limited = [False]
    detail_calls = []
    ids = {
        "42": [str(20000000 + number) for number in range(1, 31)],
        "43": ["30000001", "30000002"],
    }

    def following(page):
        current = followed[page - 1:page]
        return {"artists": [{"id": uid, "name": f"Artist {uid}"} for uid in current],
                "page": page, "total": len(followed), "is_last_page": page >= len(followed)}

    def pixiv_json(url):
        parsed = urllib.parse.urlparse(url)
        uid = parsed.path.split("/")[3]
        if parsed.path.endswith("/profile/all"):
            if rate_limited[0]:
                raise ValueError("Pixiv 请求失败：HTTP 429。")
            if uid == "43" and fail_artist_once[0]:
                fail_artist_once[0] = False
                raise ValueError("temporary Pixiv failure")
            return {"illusts": {pid: None for pid in ids[uid]}}
        if parsed.path.endswith("/profile/illusts"):
            selected = urllib.parse.parse_qs(parsed.query)["ids[]"]
            detail_calls.append((uid, selected))
            return {"works": {pid: {
                "id": pid, "title": "Moonlight" if pid == "20000030" else f"Work {pid}",
                "userId": uid, "userName": f"Artist {uid}",
                "tags": ["星空", "夜景"] if int(pid) % 2 == 0 else ["星空"],
                "url": "", "pageCount": 1,
            } for pid in selected}}
        raise AssertionError(url)

    gallery.pixiv_followed_artists = following
    gallery.pixiv_json = pixiv_json

    gallery.start_followed_index()
    gallery.FOLLOWED_INDEX_WORKERS["123456"].join(timeout=5)
    assert gallery.followed_index_status()["status"] == "partial"
    assert gallery.search_followed_index("#星空", 1)["total"] == 30
    assert sum(uid == "42" for uid, _ in detail_calls) == 2, "Only the newest 24 works were indexed"

    old_detail_calls = len(detail_calls)
    gallery.start_followed_index()
    gallery.FOLLOWED_INDEX_WORKERS["123456"].join(timeout=5)
    assert [uid for uid, _ in detail_calls[old_detail_calls:]] == ["43"], "Indexed works were fetched again"
    status = gallery.followed_index_status()
    assert status["status"] == "complete" and status["indexed_works"] == 32, status
    assert len(gallery.search_followed_index("#星空", 1)["artworks"]) == 24
    assert len(gallery.search_followed_index("#星空", 2)["artworks"]) == 8
    assert gallery.search_followed_index("#星空 #夜景", 1)["total"] == 16
    assert gallery.search_followed_index("moonLIGHT", 1)["artworks"][0]["id"] == "20000030"
    assert gallery.search_followed_index("#不存在", 1)["total"] == 0

    gallery.SESSION_COOKIE = "999999_other_account"
    assert gallery.search_followed_index("#星空", 1)["total"] == 0
    gallery.SESSION_COOKIE = "123456_test_session"
    followed[:] = ["42"]
    gallery.start_followed_index(force=True)
    gallery.FOLLOWED_INDEX_WORKERS["123456"].join(timeout=5)
    assert gallery.search_followed_index("#星空", 1)["total"] == 30
    assert gallery.followed_index_status()["indexed_works"] == 30
    with gallery.followed_index_connection() as db:
        assert db.execute("SELECT COUNT(*) FROM followed_index_works WHERE artist_id='43'").fetchone()[0] == 0

    server = ThreadingHTTPServer(("127.0.0.1", 0), gallery.Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        with urllib.request.urlopen(base + "/api/followed-index/search?q=%23%E6%98%9F%E7%A9%BA&page=2") as response:
            result = json.load(response)
            assert result["total"] == 30 and len(result["artworks"]) == 6
        request = urllib.request.Request(base + "/api/followed-index/start",
            data=b'{"force":false}', method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request) as response:
            assert json.load(response)["status"] == "complete"
    finally:
        server.shutdown()
        server.server_close()

    rate_limited[0] = True
    gallery.start_followed_index(force=True)
    gallery.FOLLOWED_INDEX_WORKERS["123456"].join(timeout=5)
    assert gallery.followed_index_status()["status"] == "error"
    assert gallery.search_followed_index("#星空", 1)["total"] == 30

print("Followed artist history indexing, retry, search, pagination and account isolation passed")
