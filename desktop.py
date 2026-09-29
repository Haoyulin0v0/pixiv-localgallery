"""Native Windows window around the gallery's local HTTP service."""

import ctypes
import hashlib
import json
import sys
import time
import traceback
from threading import Thread

import server as gallery


TITLE = "画屿 · 本地插画图库"
SMOKE = "--smoke-test" in sys.argv


def instance_mutex():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    name = hashlib.sha256(str(gallery.ROOT).casefold().encode()).hexdigest()[:24]
    handle = kernel32.CreateMutexW(None, False, f"Local\\PixivGallery-{name}")
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == 183:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
        user32.FindWindowW.restype = ctypes.c_void_p
        user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
        window = user32.FindWindowW(None, TITLE)
        if window:
            user32.ShowWindow(window, 9)
            user32.SetForegroundWindow(window)
        kernel32.CloseHandle(handle)
        return None, kernel32
    return handle, kernel32


def run():
    handle, kernel32 = instance_mutex()
    if handle is None:
        return
    service = None
    thread = None
    try:
        import webview

        gallery.connection().close()
        service = gallery.LocalHTTPServer(("127.0.0.1", 0), gallery.Handler)
        service.daemon_threads = True
        thread = Thread(target=service.serve_forever, daemon=True)
        thread.start()
        webview.settings["ALLOW_DOWNLOADS"] = True
        webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
        window = webview.create_window(
            TITLE, f"http://127.0.0.1:{service.server_port}",
            width=1320, height=900, min_size=(850, 620),
            background_color="#17191e", hidden=SMOKE,
        )
        if SMOKE:
            def verify_page():
                try:
                    result = window.evaluate_js("""(() => {
                      detailTagEditor.setTags([]);
                      const input = document.querySelector('#detail-tags');
                      input.value = '#one #two';
                      input.dispatchEvent(new Event('input', {bubbles:true}));
                      return {title: document.title, tags: detailTagEditor.getTags(),
                        gallery: !!document.querySelector('#show-discover'),
                        appearance: !!document.querySelector('#appearance-button'),
                        icon: document.querySelector('.brand-icon').naturalWidth > 0};
                    })()""")
                    if (not result or result.get("tags") != ["one", "two"]
                            or not all(result.get(key) for key in ("gallery", "appearance", "icon"))):
                        raise RuntimeError(f"WebView page verification failed: {result}")
                    window.evaluate_js("switchView('search'); switchView('library'); history.back();")
                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline:
                        if window.evaluate_js("state.view === 'search' && !navigationPending"):
                            break
                        time.sleep(0.05)
                    else:
                        raise RuntimeError("Native browser history did not restore the search view")
                    window.evaluate_js("document.activeElement?.blur(); document.dispatchEvent(new KeyboardEvent('keydown', {key:'u',bubbles:true,cancelable:true}));")
                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline:
                        if window.evaluate_js("state.view === 'library' && !navigationPending"):
                            break
                        time.sleep(0.05)
                    else:
                        raise RuntimeError("The U shortcut did not restore the library view")
                    (gallery.ROOT / "webview-smoke.json").write_text(
                        json.dumps({"ok": True, "port": service.server_port}), encoding="utf-8")
                except Exception as exc:
                    (gallery.ROOT / "webview-smoke.json").write_text(
                        json.dumps({"ok": False, "error": str(exc)}), encoding="utf-8")
                finally:
                    window.destroy()
            window.events.loaded += verify_page
        webview.start(gui="edgechromium", private_mode=False,
                      icon=str(gallery.RESOURCE_ROOT / "assets" / "app.ico"),
                      storage_path=str(gallery.DATA / "webview"))
    finally:
        if service is not None:
            service.shutdown()
            service.server_close()
        if thread is not None:
            thread.join(timeout=3)
        kernel32.CloseHandle(handle)


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:
        try:
            gallery.DATA.mkdir(exist_ok=True)
            (gallery.DATA / "desktop-error.log").write_text(traceback.format_exc(), encoding="utf-8")
            if SMOKE:
                (gallery.ROOT / "webview-smoke.json").write_text(
                    json.dumps({"ok": False, "error": str(exc)}), encoding="utf-8")
        except OSError:
            pass
        if not SMOKE:
            ctypes.windll.user32.MessageBoxW(
                None, f"画屿无法启动：{exc}\n\n请确认已安装 Microsoft Edge WebView2 Runtime。"
                "\n详细错误保存在 data/desktop-error.log。", TITLE, 0x10)
        raise SystemExit(1)
