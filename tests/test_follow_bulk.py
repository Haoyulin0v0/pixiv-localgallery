import importlib.util
import io
import json
import tempfile
import threading
import urllib.request
import urllib.error
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs


spec = importlib.util.spec_from_file_location("gallery", Path(__file__).resolve().parents[1] / "server.py")
gallery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gallery)


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class FakeOpener:
    def __init__(self):
        self.requests = []

    def open(self, request, timeout=25):
        self.requests.append(request)
        if request.full_url.endswith("bookmark_add.php"):
            return FakeResponse(b"[]")
        state = json.dumps({"api": {"token": "a" * 32}})
        page = json.dumps({"props": {"pageProps": {"serverSerializedPreloadedState": state}}})
        html = f'<script id="__NEXT_DATA__" type="application/json">{page}</script>'
        return FakeResponse(html.encode())


with tempfile.TemporaryDirectory() as temp:
    gallery.DATA = Path(temp) / "data"
    gallery.IMAGES = gallery.DATA / "images"
    gallery.DATABASE = gallery.DATA / "library.sqlite3"
    gallery.SESSION_COOKIE = "12345_test_session"
    with closing(gallery.connection()) as db:
        with db:
            for uid, artist, tags in (("one", "Old", ["a", "b"]), ("two", "Old", ["b"])):
                db.execute("INSERT INTO artworks VALUES (?,?,?,?,?,?,?,?,?,?)",
                           (uid, None, uid, artist, "7", json.dumps(tags), "", "[]", 1, 1))

    def records():
        with closing(gallery.connection()) as db:
            return {row["id"]: row for row in db.execute("SELECT * FROM artworks")}

    try:
        gallery.bulk_update_artworks({"ids": ["one", "missing"],
                                      "artist": {"name": "New", "id": "42"}})
        raise AssertionError("missing ID accepted")
    except ValueError:
        assert records()["one"]["artist"] == "Old"

    assert gallery.bulk_update_artworks({"ids": ["one", "two"],
                                          "artist": {"name": "New", "id": "42"},
                                          "tags": {"mode": "add", "values": "b，c"}})["updated"] == 2
    assert all(row["artist"] == "New" and row["artist_id"] == "42" for row in records().values())
    assert json.loads(records()["one"]["tags"]) == ["a", "b", "c"]
    assert json.loads(records()["two"]["tags"]) == ["b", "c"]
    gallery.bulk_update_artworks({"ids": ["one", "two"],
                                  "tags": {"mode": "remove", "values": ["b"]}})
    assert json.loads(records()["one"]["tags"]) == ["a", "c"]
    gallery.bulk_update_artworks({"ids": ["one"],
                                  "tags": {"mode": "replace", "values": []}})
    assert json.loads(records()["one"]["tags"]) == []
    assert json.loads(records()["two"]["tags"]) == ["c"]

    followed = False
    def fake_pixiv_json(url):
        assert url == "https://www.pixiv.net/ajax/user/42"
        return {"name": "Artist", "isFollowed": followed}

    gallery.pixiv_json = fake_pixiv_json
    class FollowOpener(FakeOpener):
        def open(self, request, timeout=25):
            global followed
            if request.full_url.endswith("bookmark_add.php"):
                followed = True
            return super().open(request, timeout)

    gallery.PIXIV_OPENER = FollowOpener()
    result = gallery.pixiv_follow_artist("42", "private")
    assert result["following"] and not result["already_following"]
    request = gallery.PIXIV_OPENER.requests[1]
    form = parse_qs(request.data.decode())
    assert request.full_url == "https://www.pixiv.net/bookmark_add.php"
    assert request.get_header("X-csrf-token") == "a" * 32
    assert request.get_header("X-user-id") == "12345"
    assert form["mode"] == ["add"] and form["type"] == ["user"]
    assert form["user_id"] == ["42"] and form["restrict"] == ["1"]
    assert gallery.pixiv_follow_artist("42")["already_following"]
    assert len(gallery.PIXIV_OPENER.requests) == 2

    server = ThreadingHTTPServer(("127.0.0.1", 0), gallery.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        with urllib.request.urlopen(base + "/api/artist-status?artist_id=42") as response:
            assert json.load(response)["following"]
        follow_request = urllib.request.Request(base + "/api/artist-follow",
            data=json.dumps({"artist_id": "42"}).encode(), method="POST",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(follow_request) as response:
            assert json.load(response)["already_following"]
        bulk_request = urllib.request.Request(base + "/api/artworks/bulk",
            data=json.dumps({"ids": ["two"], "tags": {"mode": "replace", "values": ["z"]}}).encode(),
            method="PATCH", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(bulk_request) as response:
            assert json.load(response)["updated"] == 1
        assert json.loads(records()["two"]["tags"]) == ["z"]
    finally:
        server.shutdown()
        server.server_close()

    followed = False
    class RejectedOpener(FakeOpener):
        def open(self, request, timeout=25):
            if request.full_url.endswith("bookmark_add.php"):
                raise urllib.error.HTTPError(request.full_url, 400, "Bad Request", {},
                                             io.BytesIO(b'{"message":"Captcha required"}'))
            return super().open(request, timeout)
    gallery.PIXIV_OPENER = RejectedOpener()
    try:
        gallery.pixiv_follow_artist("42")
        raise AssertionError("400 rejection accepted")
    except ValueError as error:
        assert isinstance(error, gallery.PixivFollowWebRequired)
        assert "Captcha required" in str(error)

    server = ThreadingHTTPServer(("127.0.0.1", 0), gallery.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        follow_request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/api/artist-follow",
            data=json.dumps({"artist_id": "42"}).encode(), method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(follow_request)
            raise AssertionError("Rejected follow returned success")
        except urllib.error.HTTPError as error:
            assert error.code == 409
            assert json.load(error)["follow_on_pixiv"] is True
    finally:
        server.shutdown()
        server.server_close()

print("Follow request and atomic bulk edits passed")
