"""A small, local-only illustration library with optional Pixiv import."""

from __future__ import annotations

import ctypes
import json
import os
import re
import socket
import sqlite3
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from ctypes import wintypes
from threading import Lock, Timer
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
IMAGES = DATA / "images"
DATABASE = DATA / "library.sqlite3"
SESSION_FILE = DATA / "pixiv_session.bin"
MAX_UPLOAD = 60 * 1024 * 1024
PIXIV_RE = re.compile(r"(?:pixiv\.net/(?:[^/]+/)?artworks/|^)(\d{5,12})(?:\D|$)")
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif"}


class DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


def windows_dpapi(data, decrypt=False):
    if os.name != "nt":
        raise ValueError("加密记住登录目前仅支持 Windows。")
    crypt32 = ctypes.WinDLL("Crypt32.dll", use_last_error=True)
    kernel32 = ctypes.WinDLL("Kernel32.dll", use_last_error=True)
    function = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    function.argtypes = [
        ctypes.POINTER(DataBlob), ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(DataBlob),
    ]
    function.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    input_buffer = (ctypes.c_byte * len(data)).from_buffer_copy(data)
    input_blob = DataBlob(len(data), input_buffer)
    output_blob = DataBlob()
    if not function(
        ctypes.byref(input_blob), None, None, None, None,
        0x01, ctypes.byref(output_blob),
    ):
        raise OSError(ctypes.get_last_error(), "Windows DPAPI 处理失败")
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(output_blob.pbData, ctypes.c_void_p))


def load_saved_session():
    if not SESSION_FILE.is_file():
        return ""
    try:
        return windows_dpapi(SESSION_FILE.read_bytes(), decrypt=True).decode("utf-8")
    except (OSError, UnicodeError, ValueError):
        return ""


def save_session(cookie):
    DATA.mkdir(exist_ok=True)
    try:
        encrypted = windows_dpapi(cookie.encode("utf-8"))
    except OSError as exc:
        raise ValueError("无法使用 Windows 加密保存登录状态。可取消“记住连接”后重试。") from exc
    temporary = SESSION_FILE.with_suffix(".tmp")
    temporary.write_bytes(encrypted)
    temporary.replace(SESSION_FILE)


SESSION_COOKIE = os.environ.get("PIXIV_PHPSESSID", "").strip() or load_saved_session()


class SameHostRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        original = urllib.parse.urlparse(request.full_url)
        redirected = urllib.parse.urlparse(newurl)
        if redirected.scheme != "https" or redirected.hostname != original.hostname:
            raise ValueError("Pixiv 请求发生了跨站跳转，已停止。")
        return super().redirect_request(request, fp, code, msg, headers, newurl)


PIXIV_OPENER = urllib.request.build_opener(SameHostRedirect)
DISCOVERY_LOCK = Lock()


def connection():
    DATA.mkdir(exist_ok=True)
    IMAGES.mkdir(exist_ok=True)
    db = sqlite3.connect(DATABASE)
    db.row_factory = sqlite3.Row
    db.execute(
        """CREATE TABLE IF NOT EXISTS artworks (
            id TEXT PRIMARY KEY,
            pixiv_id TEXT UNIQUE,
            title TEXT NOT NULL,
            artist TEXT NOT NULL DEFAULT '',
            artist_id TEXT NOT NULL DEFAULT '',
            tags TEXT NOT NULL DEFAULT '[]',
            source_url TEXT NOT NULL DEFAULT '',
            images TEXT NOT NULL DEFAULT '[]',
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS discovery_seen (
            user_id TEXT NOT NULL,
            pixiv_id TEXT NOT NULL,
            first_shown_at INTEGER NOT NULL,
            PRIMARY KEY (user_id, pixiv_id)
        )"""
    )
    db.commit()
    return db


def public_record(row):
    item = dict(row)
    item["tags"] = json.loads(item["tags"])
    item["images"] = json.loads(item["images"])
    return item


def artwork_id(value):
    match = PIXIV_RE.search(str(value).strip())
    if not match:
        raise ValueError("请输入 Pixiv 作品链接或作品数字 ID。")
    return match.group(1)


def pixiv_request(url, *, binary=False):
    parsed_url = urllib.parse.urlparse(url)
    if parsed_url.scheme != "https" or parsed_url.hostname not in {"www.pixiv.net", "i.pximg.net"}:
        raise ValueError("不支持的 Pixiv 地址。")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125 Safari/537.36",
        "Referer": "https://www.pixiv.net/",
        "Accept": "image/avif,image/webp,image/*,*/*" if binary else "application/json",
    }
    if SESSION_COOKIE and parsed_url.hostname == "www.pixiv.net":
        headers["Cookie"] = "PHPSESSID=" + SESSION_COOKIE
    request = urllib.request.Request(url, headers=headers)
    try:
        with PIXIV_OPENER.open(request, timeout=25) as response:
            if binary:
                length = response.headers.get("Content-Length")
                if length and int(length) > MAX_UPLOAD:
                    raise ValueError("原图超过 60 MB，已跳过。")
                data = response.read(MAX_UPLOAD + 1)
                if len(data) > MAX_UPLOAD:
                    raise ValueError("原图超过 60 MB，已跳过。")
                return data
            return json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise ValueError("Pixiv 拒绝访问。请检查登录 Cookie、作品权限和网络连接。") from exc
        if exc.code == 404:
            raise ValueError("未找到该作品，或当前账号无法查看。") from exc
        raise ValueError(f"Pixiv 请求失败：HTTP {exc.code}。") from exc
    except urllib.error.URLError as exc:
        raise ValueError(f"无法连接 Pixiv：{exc.reason}") from exc


def pixiv_json(url):
    result = pixiv_request(url)
    if result.get("error"):
        raise ValueError(result.get("message") or "Pixiv 返回错误。")
    return result.get("body")


def collect_pixiv(pid):
    base = f"https://www.pixiv.net/ajax/illust/{pid}"
    details = pixiv_json(base)
    if not isinstance(details, dict):
        raise ValueError("作品信息为空，可能需要登录或作品不可见。")
    pages = pixiv_json(base + "/pages")
    if not isinstance(pages, list) or not pages:
        raise ValueError("没有找到可下载的图片页。动图暂不支持。")
    urls = []
    for page in pages:
        original = page.get("urls", {}).get("original")
        parsed = urllib.parse.urlparse(original or "")
        if parsed.scheme != "https" or parsed.hostname != "i.pximg.net":
            raise ValueError("Pixiv 返回了不受支持的原图地址。")
        urls.append(original)
    preview_url = pages[0].get("urls", {}).get("regular") or pages[0].get("urls", {}).get("small") or ""
    preview_parsed = urllib.parse.urlparse(preview_url)
    if preview_parsed.scheme != "https" or preview_parsed.hostname != "i.pximg.net":
        preview_url = ""
    tags = [
        tag["tag"]
        for tag in details.get("tags", {}).get("tags", [])
        if isinstance(tag, dict) and isinstance(tag.get("tag"), str)
    ]
    return {
        "pixiv_id": pid,
        "title": str(details.get("title") or f"Pixiv {pid}"),
        "artist": str(details.get("userName") or ""),
        "artist_id": str(details.get("userId") or ""),
        "tags": tags,
        "source_url": f"https://www.pixiv.net/artworks/{pid}",
        "preview_url": preview_url,
        "urls": urls,
    }


def normalize_feed_item(raw):
    if not isinstance(raw, dict):
        return None
    pid = str(raw.get("id") or "")
    if not pid.isdigit():
        return None
    thumbnail = raw.get("url") or raw.get("urls", {}).get("small") or ""
    parsed = urllib.parse.urlparse(thumbnail)
    if parsed.scheme != "https" or parsed.hostname != "i.pximg.net":
        thumbnail = ""
    tags = raw.get("tags") or []
    if not isinstance(tags, list):
        tags = []
    return {
        "id": pid,
        "title": str(raw.get("title") or f"Pixiv {pid}"),
        "artist": str(raw.get("userName") or ""),
        "artist_id": str(raw.get("userId") or ""),
        "tags": [str(tag) for tag in tags if isinstance(tag, str)][:10],
        "thumbnail": thumbnail,
        "page_count": int(raw.get("pageCount") or 1),
        "source_url": f"https://www.pixiv.net/artworks/{pid}",
    }


def pixiv_feed(kind, page=1):
    if not SESSION_COOKIE:
        raise ValueError("请先连接 Pixiv 账号。")
    if kind == "discover":
        return pixiv_discover_fresh()
    if kind == "following":
        url = f"https://www.pixiv.net/ajax/follow_latest/illust?mode=all&p={page}"
    else:
        raise ValueError("未知的动态类型。")
    body = pixiv_json(url)
    if not isinstance(body, dict):
        raise ValueError("Pixiv 动态内容为空。")
    thumbnails = body.get("thumbnails") or {}
    source = thumbnails.get("illust") if isinstance(thumbnails, dict) else []
    if not isinstance(source, list):
        raise ValueError("Pixiv 动态格式已变化。")
    items = [item for raw in source if (item := normalize_feed_item(raw))]
    page_info = body.get("page") or {}
    return {
        "artworks": items,
        "page": page,
        "is_last_page": bool(page_info.get("isLastPage", True)),
    }


def pixiv_discover_fresh():
    uid = pixiv_current_user_id()
    selected = []
    selected_ids = set()
    url = "https://www.pixiv.net/ajax/discovery/artworks?mode=all&limit=60"
    with DISCOVERY_LOCK:
        db = connection()
        try:
            for _ in range(3):
                try:
                    body = pixiv_json(url)
                except ValueError:
                    if selected:
                        break
                    raise
                if not isinstance(body, dict):
                    raise ValueError("Pixiv 推荐内容为空。")
                thumbnails = body.get("thumbnails") or {}
                source = thumbnails.get("illust") if isinstance(thumbnails, dict) else None
                if not isinstance(source, list):
                    raise ValueError("Pixiv 推荐格式已变化。")
                items = [item for raw in source if (item := normalize_feed_item(raw))]
                ids = [item["id"] for item in items]
                if not ids:
                    continue
                placeholders = ",".join("?" for _ in ids)
                seen = {
                    row["pixiv_id"] for row in db.execute(
                        f"SELECT pixiv_id FROM discovery_seen WHERE user_id=? AND pixiv_id IN ({placeholders})",
                        [uid, *ids],
                    )
                }
                seen.update(
                    row["pixiv_id"] for row in db.execute(
                        f"SELECT pixiv_id FROM artworks WHERE pixiv_id IN ({placeholders})",
                        ids,
                    )
                )
                for item in items:
                    if item["id"] in seen or item["id"] in selected_ids:
                        continue
                    selected.append(item)
                    selected_ids.add(item["id"])
                    if len(selected) >= 30:
                        break
                if len(selected) >= 30:
                    break
            if selected:
                now = int(time.time())
                with db:
                    db.executemany(
                        "INSERT OR IGNORE INTO discovery_seen VALUES (?,?,?)",
                        [(uid, item["id"], now) for item in selected],
                    )
        finally:
            db.close()
    return {"artworks": selected, "page": 1, "is_last_page": False}


def pixiv_ranking(mode, page):
    if mode not in {"daily", "weekly", "monthly", "rookie"}:
        raise ValueError("未知的排行榜类型。")
    if not 1 <= page <= 10:
        raise ValueError("排行榜页码超出范围。")
    parameters = urllib.parse.urlencode({
        "mode": mode, "content": "illust", "format": "json", "p": page,
    })
    result = pixiv_request(f"https://www.pixiv.net/ranking.php?{parameters}")
    if not isinstance(result, dict) or not isinstance(result.get("contents"), list):
        raise ValueError("Pixiv 排行榜格式已变化。")
    items = []
    for raw in result["contents"]:
        if not isinstance(raw, dict) or raw.get("is_masked"):
            continue
        normalized = normalize_feed_item({
            "id": raw.get("illust_id"), "title": raw.get("title"),
            "userName": raw.get("user_name"), "userId": raw.get("user_id"),
            "url": raw.get("url"), "tags": raw.get("tags"),
            "pageCount": raw.get("illust_page_count"),
        })
        if normalized:
            normalized["rank"] = int(raw.get("rank") or 0)
            items.append(normalized)
    return {
        "artworks": items, "page": page,
        "is_last_page": not bool(result.get("next")) or page >= 10,
        "date": str(result.get("date") or ""),
    }


def local_image_path(uid, page):
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", uid):
        raise ValueError("作品 ID 无效。")
    if not isinstance(page, int) or page < 0:
        raise ValueError("图片页码无效。")
    with connection() as db:
        row = db.execute("SELECT images FROM artworks WHERE id=?", (uid,)).fetchone()
    if row is None:
        raise ValueError("没有找到该作品。")
    images = json.loads(row["images"])
    if page >= len(images):
        raise ValueError("图片页码超出范围。")
    name = images[page]
    if not isinstance(name, str) or not re.fullmatch(
        r"[a-zA-Z0-9_-]+\.(jpg|png|gif|webp|avif)", name
    ):
        raise ValueError("图片文件名无效。")
    path = IMAGES / name
    if not path.is_file():
        raise ValueError("本地图片文件不存在。")
    return path


def pixiv_current_user_id():
    if not SESSION_COOKIE:
        raise ValueError("请先连接 Pixiv 账号。")
    uid = SESSION_COOKIE.split("_", 1)[0]
    if not re.fullmatch(r"\d{1,12}", uid):
        raise ValueError("无法从当前 Pixiv 会话识别用户 ID，请重新连接账号。")
    return uid


def validated_artist_id(value):
    uid = str(value).strip()
    if not re.fullmatch(r"\d{1,12}", uid):
        raise ValueError("请输入有效的 Pixiv 画师 ID。")
    return uid


class PixivNextDataParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_next_data = False
        self.next_data = ""

    def handle_starttag(self, tag, attrs):
        if tag == "script" and dict(attrs).get("id") == "__NEXT_DATA__":
            self.in_next_data = True

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_next_data = False

    def handle_data(self, data):
        if self.in_next_data:
            self.next_data += data


def pixiv_csrf_token():
    uid = pixiv_current_user_id()
    request = urllib.request.Request(f"https://www.pixiv.net/users/{uid}", headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125 Safari/537.36",
        "Referer": "https://www.pixiv.net/",
        "Cookie": "PHPSESSID=" + SESSION_COOKIE,
    })
    try:
        with PIXIV_OPENER.open(request, timeout=25) as response:
            html = response.read(2_000_001)
    except urllib.error.HTTPError as exc:
        raise ValueError(f"无法读取 Pixiv 登录页面：HTTP {exc.code}。") from exc
    except urllib.error.URLError as exc:
        raise ValueError(f"无法连接 Pixiv：{exc.reason}") from exc
    if len(html) > 2_000_000:
        raise ValueError("Pixiv 登录页面过大，无法读取认证令牌。")
    parser = PixivNextDataParser()
    parser.feed(html.decode("utf-8", "replace"))
    try:
        page = json.loads(parser.next_data)
        state = json.loads(page["props"]["pageProps"]["serverSerializedPreloadedState"])
        token = state["api"]["token"]
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError("无法从 Pixiv 登录页面读取认证令牌，请重新连接账号。") from exc
    if not isinstance(token, str) or not re.fullmatch(r"[A-Fa-f0-9]{32,64}", token):
        raise ValueError("Pixiv 认证令牌格式无效，请重新连接账号。")
    return token


def pixiv_artist_status(value):
    current_uid = pixiv_current_user_id()
    uid = validated_artist_id(value)
    body = pixiv_json(f"https://www.pixiv.net/ajax/user/{uid}")
    if not isinstance(body, dict) or not isinstance(body.get("isFollowed"), bool):
        raise ValueError("Pixiv 画师信息格式已变化，无法确认关注状态。")
    return {"artist_id": uid, "artist": str(body.get("name") or ""),
            "following": body["isFollowed"], "is_self": uid == current_uid}


def pixiv_follow_artist(value, privacy="public"):
    uid = validated_artist_id(value)
    if privacy not in {"public", "private"}:
        raise ValueError("关注可见范围无效。")
    status = pixiv_artist_status(uid)
    if status["is_self"]:
        raise ValueError("不能关注自己的 Pixiv 账号。")
    if status["following"]:
        return {**status, "already_following": True}
    csrf_token = pixiv_csrf_token()
    form = urllib.parse.urlencode({"mode": "add", "type": "user", "user_id": uid,
                                   "tag": "", "restrict": 0 if privacy == "public" else 1,
                                   "format": "json"}).encode("utf-8")
    request = urllib.request.Request(
        "https://www.pixiv.net/bookmark_add.php", data=form, method="POST",
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125 Safari/537.36",
            "Referer": f"https://www.pixiv.net/users/{uid}",
            "Origin": "https://www.pixiv.net",
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
            "Cookie": "PHPSESSID=" + SESSION_COOKIE,
            "x-csrf-token": csrf_token,
            "x-user-id": pixiv_current_user_id(),
        },
    )
    try:
        with PIXIV_OPENER.open(request, timeout=25) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            raise ValueError("Pixiv 拒绝关注请求，请检查登录状态或在 Pixiv 页面完成验证。") from exc
        if exc.code == 400:
            try:
                error_body = json.loads(exc.read(8192))
                message = error_body.get("message") if isinstance(error_body, dict) else None
                if isinstance(message, str):
                    message = re.sub(r"[A-Za-z0-9_-]{24,}", "[已隐藏]", message).strip()
                    if message and len(message) <= 200 and "<" not in message:
                        raise ValueError(f"Pixiv 拒绝关注请求（HTTP 400）：{message}") from exc
            except (json.JSONDecodeError, UnicodeError):
                pass
            raise ValueError("Pixiv 拒绝关注请求（HTTP 400）。可能需要在 Pixiv 网页完成验证。") from exc
        raise ValueError(f"Pixiv 关注请求失败：HTTP {exc.code}。") from exc
    except urllib.error.URLError as exc:
        raise ValueError(f"无法连接 Pixiv：{exc.reason}") from exc
    except (ValueError, UnicodeError) as exc:
        raise ValueError("Pixiv 关注响应格式无效，请到画师主页确认状态。") from exc
    if result != []:
        message = result.get("message") if isinstance(result, dict) else None
        raise ValueError(str(message or "Pixiv 未接受关注请求，请到画师主页确认状态。"))
    verified = pixiv_artist_status(uid)
    if not verified["following"]:
        raise ValueError("Pixiv 尚未确认关注成功，请到画师主页确认状态。")
    return {**verified, "already_following": False}


def pixiv_followed_artists(page):
    uid = pixiv_current_user_id()
    base = f"https://www.pixiv.net/ajax/user/{uid}/following"

    def get_following(rest, offset, limit):
        parameters = urllib.parse.urlencode({"offset": offset, "limit": limit, "rest": rest})
        body = pixiv_json(f"{base}?{parameters}")
        if not isinstance(body, dict) or not isinstance(body.get("users"), list):
            raise ValueError("Pixiv 关注画师列表格式已变化。")
        return body

    public_total = int(get_following("show", 0, 1).get("total") or 0)
    private_total = int(get_following("hide", 0, 1).get("total") or 0)
    total = public_total + private_total
    offset = (page - 1) * 24
    raw_users = []
    if offset < public_total:
        amount = min(24, public_total - offset)
        raw_users.extend(get_following("show", offset, amount)["users"])
    if len(raw_users) < 24 and offset + len(raw_users) < total:
        private_offset = max(0, offset + len(raw_users) - public_total)
        raw_users.extend(get_following("hide", private_offset, 24 - len(raw_users))["users"])
    artists = [item for raw in raw_users if (item := normalize_artist(raw))]
    return {"artists": artists, "page": page, "total": total,
            "is_last_page": page * 24 >= total}


def pixiv_search_artworks(keyword, page, order="date_d"):
    keyword = keyword.strip()
    if not keyword or len(keyword) > 100:
        raise ValueError("请输入 1 到 100 字的 Pixiv 搜索词。")
    if order not in {"date_d", "popular_d", "hot"}:
        raise ValueError("未知的搜索排序方式。")
    if order == "popular_d":
        uid = pixiv_current_user_id()
        profile = pixiv_json(f"https://www.pixiv.net/ajax/user/{uid}")
        if not isinstance(profile, dict) or not profile.get("premium"):
            raise ValueError("Pixiv 的完整热门排序需要 Premium 账号。当前账号可选“热门精选”，或继续按最新搜索。")
    encoded = urllib.parse.quote(keyword, safe="")
    parameters = urllib.parse.urlencode({
        "word": keyword, "order": "date_d" if order == "hot" else order,
        "mode": "all" if SESSION_COOKIE else "safe",
        "p": page, "s_mode": "s_tag", "type": "illust",
    })
    body = pixiv_json(f"https://www.pixiv.net/ajax/search/illustrations/{encoded}?{parameters}")
    if order == "hot":
        popular = body.get("popular") if isinstance(body, dict) else None
        if not isinstance(popular, dict):
            raise ValueError("Pixiv 未返回该标签的热门精选。")
        raw_items = (popular.get("permanent") or []) + (popular.get("recent") or [])
        items = list({item["id"]: item for raw in raw_items
                      if (item := normalize_feed_item(raw))}.values())
        return {"artworks": items, "page": 1, "is_last_page": True,
                "total": len(items), "limited_selection": True}
    section = body.get("illust") if isinstance(body, dict) else None
    if not isinstance(section, dict) or not isinstance(section.get("data"), list):
        raise ValueError("Pixiv 搜索结果格式已变化。")
    items = [item for raw in section["data"] if (item := normalize_feed_item(raw))]
    last_page = int(section.get("lastPage") or page)
    return {"artworks": items, "page": page, "is_last_page": page >= last_page,
            "total": int(section.get("total") or 0)}


def normalize_artist(raw):
    if not isinstance(raw, dict):
        return None
    uid = str(raw.get("userId") or raw.get("id") or "")
    if not uid.isdigit():
        return None
    avatar = str(raw.get("image") or raw.get("profileImageUrl") or raw.get("imageBig") or "")
    parsed = urllib.parse.urlparse(avatar)
    if parsed.scheme != "https" or parsed.hostname != "i.pximg.net":
        avatar = ""
    return {"id": uid, "name": str(raw.get("name") or raw.get("userName") or f"画师 {uid}"),
            "avatar": avatar, "source_url": f"https://www.pixiv.net/users/{uid}"}


def pixiv_search_artists(keyword, page):
    keyword = keyword.strip()
    if not keyword or len(keyword) > 100:
        raise ValueError("请输入画师名称、用户 ID 或画师主页链接。")
    match = re.fullmatch(r"(?:https?://(?:www\.)?pixiv\.net/(?:[a-z-]+/)?users/)?(\d{1,12})/?", keyword)
    if match:
        uid = match.group(1)
        artist = normalize_artist(pixiv_json(f"https://www.pixiv.net/ajax/user/{uid}"))
        return {"artists": [artist] if artist else [], "page": 1, "is_last_page": True}
    if not SESSION_COOKIE:
        raise ValueError("按画师名称搜索需要先连接 Pixiv 账号；也可以输入画师主页链接或用户 ID。")
    parameters = urllib.parse.urlencode({"nick": keyword, "s_mode": "s_usr", "i": 1, "p": page})
    body = pixiv_json(f"https://www.pixiv.net/ajax/search/users?{parameters}")
    if not isinstance(body, dict):
        raise ValueError("Pixiv 画师搜索结果格式已变化。")
    thumbnails = body.get("thumbnails") or {}
    candidates = body.get("users") or (thumbnails.get("user") if isinstance(thumbnails, dict) else None) or []
    if isinstance(candidates, dict):
        candidates = list(candidates.values())
    artists = [item for raw in candidates if (item := normalize_artist(raw))] if isinstance(candidates, list) else []
    page_info = body.get("page") or {}
    if not artists and isinstance(page_info.get("userIds"), list):
        for uid in page_info["userIds"][:12]:
            if str(uid).isdigit():
                try:
                    artist = normalize_artist(pixiv_json(f"https://www.pixiv.net/ajax/user/{uid}"))
                    if artist:
                        artists.append(artist)
                except ValueError:
                    continue
    artists = list({artist["id"]: artist for artist in artists}.values())
    return {"artists": artists, "page": page,
            "is_last_page": bool(page_info.get("isLastPage", len(artists) < 12))}


def pixiv_artist_works(uid, page):
    if not re.fullmatch(r"\d{1,12}", uid):
        raise ValueError("请输入有效的画师用户 ID。")
    body = pixiv_json(f"https://www.pixiv.net/ajax/user/{uid}/profile/all")
    illusts = body.get("illusts") if isinstance(body, dict) else None
    if not isinstance(illusts, dict):
        raise ValueError("无法读取该画师的作品列表。")
    ids = sorted((pid for pid in illusts if pid.isdigit()), key=int, reverse=True)
    page_ids = ids[(page - 1) * 24:page * 24]
    if not page_ids:
        return {"artworks": [], "page": page, "is_last_page": True, "total": len(ids)}
    parameters = urllib.parse.urlencode(
        [("ids[]", pid) for pid in page_ids] +
        [("work_category", "illust"), ("is_first_page", 1 if page == 1 else 0)]
    )
    detail = pixiv_json(f"https://www.pixiv.net/ajax/user/{uid}/profile/illusts?{parameters}")
    works = detail.get("works") if isinstance(detail, dict) else None
    if not isinstance(works, dict):
        raise ValueError("无法读取该画师的作品详情。")
    items = [item for pid in page_ids if (item := normalize_feed_item(works.get(pid)))]
    return {"artworks": items, "page": page,
            "is_last_page": page * 24 >= len(ids), "total": len(ids)}


def clean_tags(value):
    if isinstance(value, str):
        value = re.split(r"[,，\n]+", value)
    if not isinstance(value, list):
        raise ValueError("标签格式无效。")
    return list(dict.fromkeys(str(tag).strip() for tag in value if str(tag).strip()))[:100]


def bulk_update_artworks(payload):
    if not isinstance(payload, dict):
        raise ValueError("批量修改内容无效。")
    ids = payload.get("ids")
    if not isinstance(ids, list) or not 1 <= len(ids) <= 500:
        raise ValueError("请选择 1 到 500 件作品。")
    if any(not isinstance(uid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", uid) for uid in ids) or len(set(ids)) != len(ids):
        raise ValueError("作品 ID 无效或重复。")
    artist_update = payload.get("artist")
    tag_update = payload.get("tags")
    if artist_update is None and tag_update is None:
        raise ValueError("请选择要修改的画师或标签。")
    if artist_update is not None:
        if not isinstance(artist_update, dict):
            raise ValueError("画师信息格式无效。")
        artist = str(artist_update.get("name", "")).strip()
        artist_id = str(artist_update.get("id", "")).strip()
        if len(artist) > 200 or (artist_id and not re.fullmatch(r"\d{1,12}", artist_id)):
            raise ValueError("画师名称或 Pixiv 画师 ID 无效。")
        if artist_id and not artist:
            raise ValueError("填写画师 ID 时也请填写画师名称。")
    if tag_update is not None:
        if not isinstance(tag_update, dict) or tag_update.get("mode") not in {"add", "remove", "replace"}:
            raise ValueError("标签修改方式无效。")
        tags = clean_tags(tag_update.get("values", []))
        if tag_update["mode"] != "replace" and not tags:
            raise ValueError("请填写要添加或移除的标签。")
    db = connection()
    try:
        with db:
            db.execute("BEGIN IMMEDIATE")
            placeholders = ",".join("?" for _ in ids)
            rows = db.execute(f"SELECT id,artist,artist_id,tags FROM artworks WHERE id IN ({placeholders})", ids).fetchall()
            if len(rows) != len(ids):
                raise ValueError("部分作品已不存在；请刷新图库后重试。")
            now = int(time.time())
            for row in rows:
                current_tags = json.loads(row["tags"])
                if tag_update is None:
                    updated_tags = current_tags
                elif tag_update["mode"] == "replace":
                    updated_tags = tags
                elif tag_update["mode"] == "add":
                    updated_tags = list(dict.fromkeys(current_tags + tags))[:100]
                else:
                    removed = set(tags)
                    updated_tags = [tag for tag in current_tags if tag not in removed]
                db.execute("UPDATE artworks SET artist=?,artist_id=?,tags=?,updated_at=? WHERE id=?",
                           (artist if artist_update is not None else row["artist"],
                            artist_id if artist_update is not None else row["artist_id"],
                            json.dumps(updated_tags, ensure_ascii=False), now, row["id"]))
    finally:
        db.close()
    return {"ok": True, "updated": len(ids)}


def image_extension(name, data):
    ext = Path(name).suffix.lower()
    if ext not in IMAGE_EXTENSIONS:
        raise ValueError("仅支持 JPG、PNG、GIF、WebP 和 AVIF 图片。")
    signatures = {
        ".jpg": data.startswith(b"\xff\xd8\xff"),
        ".jpeg": data.startswith(b"\xff\xd8\xff"),
        ".png": data.startswith(b"\x89PNG\r\n\x1a\n"),
        ".gif": data.startswith((b"GIF87a", b"GIF89a")),
        ".webp": data.startswith(b"RIFF") and data[8:12] == b"WEBP",
        ".avif": data[4:12] in (b"ftypavif", b"ftypavis"),
    }
    if not signatures[ext]:
        raise ValueError("文件内容与图片格式不符。")
    return ".jpg" if ext == ".jpeg" else ext


class Handler(BaseHTTPRequestHandler):
    def local_origin_ok(self):
        origin = self.headers.get("Origin")
        if not origin:
            return True
        parsed = urllib.parse.urlparse(origin)
        return (
            parsed.scheme == "http"
            and parsed.hostname in {"127.0.0.1", "localhost"}
            and parsed.port == self.server.server_port
        )

    def send_json(self, status, value):
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        size = int(self.headers.get("Content-Length", "0"))
        if size > 100_000 or size < 1:
            raise ValueError("请求内容大小无效。")
        return json.loads(self.rfile.read(size))

    def read_image(self):
        size = int(self.headers.get("Content-Length", "0"))
        if size < 1 or size > MAX_UPLOAD:
            raise ValueError("图片为空或超过 60 MB。")
        return self.rfile.read(size)

    def serve_file(self, path, content_type):
        if not path.is_file():
            self.send_error(404)
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def send_image(self, data, content_type):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=3600")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/api/artworks":
            with connection() as db:
                rows = db.execute("SELECT * FROM artworks ORDER BY created_at DESC").fetchall()
            self.send_json(200, {"artworks": [public_record(row) for row in rows]})
        elif path == "/api/status":
            self.send_json(200, {
                "pixiv_cookie_configured": bool(SESSION_COOKIE),
                "session_saved": SESSION_FILE.is_file() and bool(load_saved_session()),
            })
        elif path == "/api/feed":
            query = urllib.parse.parse_qs(parsed.query)
            kind = query.get("kind", [""])[0]
            try:
                page = int(query.get("page", ["1"])[0])
                if page < 1 or page > 100:
                    raise ValueError("页码超出范围。")
                self.send_json(200, pixiv_feed(kind, page))
            except (ValueError, TypeError) as exc:
                self.send_json(400, {"error": str(exc)})
        elif path == "/api/search":
            query = urllib.parse.parse_qs(parsed.query)
            kind = query.get("kind", [""])[0]
            try:
                page = int(query.get("page", ["1"])[0])
                if not 1 <= page <= 100:
                    raise ValueError("页码超出范围。")
                if kind == "artworks":
                    result = pixiv_search_artworks(query.get("q", [""])[0], page,
                                                  query.get("order", ["date_d"])[0])
                elif kind == "artists":
                    result = pixiv_search_artists(query.get("q", [""])[0], page)
                elif kind == "artist_works":
                    result = pixiv_artist_works(query.get("artist_id", [""])[0], page)
                else:
                    raise ValueError("未知的搜索类型。")
                self.send_json(200, result)
            except (ValueError, TypeError) as exc:
                self.send_json(400, {"error": str(exc)})
        elif path == "/api/followed-artists":
            query = urllib.parse.parse_qs(parsed.query)
            try:
                page = int(query.get("page", ["1"])[0])
                if not 1 <= page <= 1000:
                    raise ValueError("页码超出范围。")
                self.send_json(200, pixiv_followed_artists(page))
            except (ValueError, TypeError) as exc:
                self.send_json(400, {"error": str(exc)})
        elif path == "/api/artist-status":
            query = urllib.parse.parse_qs(parsed.query)
            try:
                self.send_json(200, pixiv_artist_status(query.get("artist_id", [""])[0]))
            except (ValueError, TypeError) as exc:
                self.send_json(400, {"error": str(exc)})
        elif path == "/api/ranking":
            query = urllib.parse.parse_qs(parsed.query)
            try:
                page = int(query.get("page", ["1"])[0])
                self.send_json(200, pixiv_ranking(query.get("mode", ["daily"])[0], page))
            except (ValueError, TypeError) as exc:
                self.send_json(400, {"error": str(exc)})
        elif path == "/api/pixiv/detail":
            query = urllib.parse.parse_qs(parsed.query)
            try:
                pid = artwork_id(query.get("id", [""])[0])
                item = collect_pixiv(pid)
                item["page_count"] = len(item.pop("urls"))
                self.send_json(200, item)
            except ValueError as exc:
                self.send_json(400, {"error": str(exc)})
        elif path == "/api/preview":
            query = urllib.parse.parse_qs(parsed.query)
            url = query.get("url", [""])[0]
            remote = urllib.parse.urlparse(url)
            if remote.scheme != "https" or remote.hostname != "i.pximg.net":
                self.send_error(400, "Invalid thumbnail URL")
                return
            try:
                data = pixiv_request(url, binary=True)
                ext = Path(remote.path).suffix.lower()
                mime = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif"}.get(ext, "image/jpeg")
                self.send_image(data, mime)
            except ValueError:
                self.send_error(404, "Thumbnail unavailable")
        elif path.startswith("/images/"):
            name = path.removeprefix("/images/")
            if not re.fullmatch(r"[a-zA-Z0-9_-]+\.(jpg|png|gif|webp|avif)", name):
                self.send_error(404)
                return
            ext = Path(name).suffix
            mime = {".jpg": "image/jpeg", ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp", ".avif": "image/avif"}[ext]
            self.serve_file(IMAGES / name, mime)
        elif path in ("/", "/index.html"):
            self.serve_file(ROOT / "index.html", "text/html; charset=utf-8")
        else:
            self.send_error(404)

    def do_POST(self):
        if not self.local_origin_ok():
            self.send_error(403, "Cross-site request blocked")
            return
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/api/session":
                global SESSION_COOKIE
                payload = self.read_json()
                cookie = str(payload.get("cookie", "")).strip()
                remember = bool(payload.get("remember", False))
                if not re.fullmatch(r"[A-Za-z0-9_-]{16,512}", cookie):
                    raise ValueError("Cookie 格式不正确，请只填写 PHPSESSID 的值。")
                old_cookie = SESSION_COOKIE
                SESSION_COOKIE = cookie
                try:
                    pixiv_feed("following", 1)
                    if remember:
                        save_session(cookie)
                    else:
                        SESSION_FILE.unlink(missing_ok=True)
                except Exception:
                    SESSION_COOKIE = old_cookie
                    raise
                self.send_json(200, {"ok": True, "session_saved": remember})
            elif path == "/api/upload":
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                filename = query.get("filename", [""])[0]
                image = self.read_image()
                ext = image_extension(filename, image)
                uid = uuid.uuid4().hex
                name = uid + ext
                (IMAGES / name).write_bytes(image)
                now = int(time.time())
                record = (uid, None, Path(filename).stem[:200], "", "", "[]", "", json.dumps([name]), now, now)
                with connection() as db:
                    db.execute("INSERT INTO artworks VALUES (?,?,?,?,?,?,?,?,?,?)", record)
                self.send_json(201, {"id": uid})
            elif path == "/api/pixiv/import":
                pid = artwork_id(self.read_json().get("url", ""))
                with connection() as db:
                    if db.execute("SELECT 1 FROM artworks WHERE pixiv_id=?", (pid,)).fetchone():
                        raise ValueError("这件作品已经在图库中。")
                item = collect_pixiv(pid)
                saved = []
                try:
                    for index, url in enumerate(item["urls"]):
                        data = pixiv_request(url, binary=True)
                        ext = image_extension(urllib.parse.urlparse(url).path, data)
                        name = f"pixiv_{pid}_p{index}{ext}"
                        (IMAGES / name).write_bytes(data)
                        saved.append(name)
                        if index + 1 < len(item["urls"]):
                            time.sleep(0.8)
                    now = int(time.time())
                    with connection() as db:
                        db.execute(
                            "INSERT INTO artworks VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (
                                "pixiv_" + pid, pid, item["title"], item["artist"],
                                item["artist_id"], json.dumps(item["tags"], ensure_ascii=False),
                                item["source_url"], json.dumps(saved), now, now,
                            ),
                        )
                except Exception:
                    for name in saved:
                        (IMAGES / name).unlink(missing_ok=True)
                    raise
                self.send_json(201, {"id": "pixiv_" + pid, "pages": len(saved)})
            elif path == "/api/pixiv/metadata":
                pid = artwork_id(self.read_json().get("url", ""))
                item = collect_pixiv(pid)
                item.pop("urls")
                self.send_json(200, item)
            elif path == "/api/artist-follow":
                payload = self.read_json()
                if not isinstance(payload, dict):
                    raise ValueError("关注请求格式无效。")
                self.send_json(200, pixiv_follow_artist(payload.get("artist_id", ""),
                                                       payload.get("privacy", "public")))
            elif path.startswith("/api/artworks/") and path.endswith("/open"):
                uid = path.removeprefix("/api/artworks/").removesuffix("/open")
                payload = self.read_json()
                page = payload.get("page", 0)
                action = payload.get("action", "open")
                if action not in {"open", "reveal", "both"}:
                    raise ValueError("未知的打开方式。")
                image = local_image_path(uid, page)
                if os.name != "nt":
                    raise ValueError("打开本地图片目前仅支持 Windows。")
                if action in {"open", "both"}:
                    os.startfile(str(image))
                if action in {"reveal", "both"}:
                    subprocess.Popen(["explorer.exe", "/select,", str(image)])
                self.send_json(200, {"ok": True, "path": str(image)})
            else:
                self.send_error(404)
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json(400, {"error": str(exc)})
        except Exception as exc:
            print("Request failed:", repr(exc))
            self.send_json(500, {"error": "处理失败，请查看终端中的错误信息。"})

    def do_PATCH(self):
        if not self.local_origin_ok():
            self.send_error(403, "Cross-site request blocked")
            return
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/artworks/bulk":
            try:
                self.send_json(200, bulk_update_artworks(self.read_json()))
            except (ValueError, json.JSONDecodeError) as exc:
                self.send_json(400, {"error": str(exc)})
            return
        if not path.startswith("/api/artworks/"):
            self.send_error(404)
            return
        uid = path.removeprefix("/api/artworks/")
        try:
            values = self.read_json()
            title = str(values.get("title", "")).strip()[:200]
            if not title:
                raise ValueError("作品标题不能为空。")
            artist = str(values.get("artist", "")).strip()[:200]
            artist_id = str(values.get("artist_id", "")).strip()[:30]
            source_url = str(values.get("source_url", "")).strip()
            pixiv_id = artwork_id(source_url) if source_url else None
            if source_url:
                source_url = f"https://www.pixiv.net/artworks/{pixiv_id}"
            tags = clean_tags(values.get("tags", []))
            with connection() as db:
                if pixiv_id and db.execute(
                    "SELECT id FROM artworks WHERE pixiv_id=? AND id<>?", (pixiv_id, uid)
                ).fetchone():
                    raise ValueError("这件 Pixiv 作品已关联图库中的另一项。")
                result = db.execute(
                    "UPDATE artworks SET title=?,artist=?,artist_id=?,tags=?,source_url=?,pixiv_id=?,updated_at=? WHERE id=?",
                    (title, artist, artist_id, json.dumps(tags, ensure_ascii=False), source_url, pixiv_id, int(time.time()), uid),
                )
                if not result.rowcount:
                    raise ValueError("没有找到该作品。")
            self.send_json(200, {"ok": True})
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json(400, {"error": str(exc)})

    def do_DELETE(self):
        if not self.local_origin_ok():
            self.send_error(403, "Cross-site request blocked")
            return
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/session":
            global SESSION_COOKIE
            SESSION_COOKIE = ""
            SESSION_FILE.unlink(missing_ok=True)
            self.send_json(200, {"ok": True})
            return
        if not path.startswith("/api/artworks/"):
            self.send_error(404)
            return
        uid = path.removeprefix("/api/artworks/")
        with connection() as db:
            row = db.execute("SELECT images FROM artworks WHERE id=?", (uid,)).fetchone()
            if row is None:
                self.send_json(404, {"error": "没有找到该作品。"})
                return
            db.execute("DELETE FROM artworks WHERE id=?", (uid,))
        for name in json.loads(row["images"]):
            if re.fullmatch(r"[a-zA-Z0-9_-]+\.(jpg|png|gif|webp|avif)", name):
                (IMAGES / name).unlink(missing_ok=True)
        self.send_json(200, {"ok": True})


class LocalHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = False

    def server_bind(self):
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


if __name__ == "__main__":
    connection().close()
    try:
        server = LocalHTTPServer(("127.0.0.1", 8765), Handler)
    except OSError as exc:
        raise SystemExit(f"Gallery could not start: port 8765 is in use. Close all earlier gallery command windows and try again. Details: {exc}") from exc
    print("插画图库已启动：http://127.0.0.1:8765")
    print("按 Ctrl+C 停止。图片与数据库保存在 data 文件夹。")
    if os.environ.get("GALLERY_OPEN_BROWSER") == "1":
        Timer(0.7, lambda: webbrowser.open("http://127.0.0.1:8765")).start()
    server.serve_forever()
