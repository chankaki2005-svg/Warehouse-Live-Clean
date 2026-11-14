"""
WeChat Clipboard Bridge
========================
Run on Windows. Watches the clipboard for images copied from WeChat
and uploads them to Google Drive via the API server.

Install:
    pip install requests Pillow pywin32

Usage:
    Set environment variables (done by the .bat launcher), then:
        python wechat_bridge.py
"""

import os
import sys
import time
import hashlib
import threading
import io
import requests
from datetime import datetime
from urllib.parse import quote

# ══════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════
API_BASE         = os.environ.get("WECHAT_API_BASE", "https://warehouse-live--chankaki2005.replit.app")
API_KEY          = os.environ.get("SYNC_API_KEY", "")
DRIVE_FOLDER_NAME = os.environ.get("WECHAT_FOLDER", "test")
POLL_INTERVAL    = 1   # seconds between clipboard checks

# ══════════════════════════════════════════════════════════════

_seen_hashes: set = set()
_lock = threading.Lock()


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def image_hash(data: bytes) -> str:
    return hashlib.md5(data[:65536]).hexdigest()


def get_clipboard_image() -> bytes | None:
    """Return PNG bytes if the clipboard contains an image, else None."""
    try:
        import win32clipboard  # type: ignore[import]
        from PIL import Image  # type: ignore[import]

        win32clipboard.OpenClipboard()
        try:
            # CF_DIB = Device Independent Bitmap (most common)
            CF_DIB  = 8
            CF_HDROP = 15   # Files dragged from Explorer
            if win32clipboard.IsClipboardFormatAvailable(CF_DIB):
                data = win32clipboard.GetClipboardData(CF_DIB)
                # Parse DIB header and convert to PNG via Pillow
                img = Image.open(io.BytesIO(
                    b'BM' + len(data).to_bytes(4, 'little') + b'\x00\x00\x00\x00' +
                    b'\x36\x00\x00\x00' + data
                ))
                buf = io.BytesIO()
                img.convert("RGB").save(buf, format="JPEG", quality=90)
                return buf.getvalue()
        except Exception:
            pass
        finally:
            win32clipboard.CloseClipboard()
    except ImportError:
        pass
    return None


def get_clipboard_image_pil() -> bytes | None:
    """Read image from clipboard. Handles both bitmap and file-path clipboard formats."""
    try:
        from PIL import ImageGrab, Image  # type: ignore[import]
        raw = ImageGrab.grabclipboard()
        if raw is None:
            return None
        # WeChat may copy the image as a file path list instead of raw bitmap
        if isinstance(raw, list):
            if not raw:
                return None
            try:
                img = Image.open(raw[0])
            except Exception:
                return None
        else:
            img = raw
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=90)
        return buf.getvalue()
    except Exception:
        return None


def upload_photo(data: bytes) -> bool:
    url = f"{API_BASE.rstrip('/')}/api/wechat/photo"
    try:
        resp = requests.post(
            url,
            data=data,
            headers={
                "Content-Type": "image/jpeg",
                "x-api-key": API_KEY,
                "x-group-name": quote(DRIVE_FOLDER_NAME, safe=""),
            },
            timeout=60,
        )
        if resp.ok:
            result = resp.json()
            log(f"  Upload OK -> {result.get('driveLink', '(pending)')}")
            return True
        else:
            log(f"  Upload FAIL HTTP {resp.status_code}: {resp.text[:150]}")
            return False
    except Exception as e:
        log(f"  Upload error: {e}")
        return False


def watch_clipboard() -> None:
    log("Clipboard watcher started.")
    log(f"  API:          {API_BASE}")
    log(f"  Drive folder: {DRIVE_FOLDER_NAME}")
    log("  Copy any WeChat photo (right-click -> Copy) to upload it to Drive.")
    log("  Press Ctrl+C to stop.")
    print(flush=True)

    while True:
        try:
            data = get_clipboard_image_pil()
            if data and len(data) > 1000:
                h = image_hash(data)
                with _lock:
                    if h not in _seen_hashes:
                        _seen_hashes.add(h)
                        log(f"Image detected in clipboard ({len(data)//1024} KB) — uploading...")
                        upload_photo(data)
        except Exception as e:
            log(f"Clipboard error: {e}")
        time.sleep(POLL_INTERVAL)


def check_deps() -> bool:
    ok = True
    try:
        from PIL import ImageGrab  # noqa: F401
    except ImportError:
        print("ERROR: Pillow not installed.")
        print("  Run: pip install Pillow")
        ok = False
    if not API_KEY:
        print("ERROR: SYNC_API_KEY not set")
        ok = False
    return ok


def main() -> None:
    if not check_deps():
        sys.exit(1)
    watch_clipboard()


if __name__ == "__main__":
    main()
