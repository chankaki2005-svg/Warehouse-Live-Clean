"""
WeChat Clipboard Bridge - Diagnostic Tool
"""

import os
import sys
import io
import time
import requests
from datetime import datetime

API_BASE  = os.environ.get("WECHAT_API_BASE", "https://warehouse-live--chankaki2005.replit.app")
API_KEY   = os.environ.get("SYNC_API_KEY", "")

DIVIDER = "-" * 60


def ok(msg):   print(f"  [OK]   {msg}", flush=True)
def fail(msg): print(f"  [FAIL] {msg}", flush=True)
def info(msg): print(f"  [INFO] {msg}", flush=True)


def test_api_connection():
    print(f"\n{DIVIDER}")
    print("Step 1: Test API connection")
    print(DIVIDER)
    info(f"Target: {API_BASE}/api/healthz")
    try:
        r = requests.get(f"{API_BASE.rstrip('/')}/api/healthz", timeout=10)
        if r.status_code == 200:
            ok("Server OK (HTTP 200)")
            return True
        else:
            fail(f"HTTP {r.status_code}: {r.text[:100]}")
            return False
    except Exception as e:
        fail(f"Connection error: {e}")
        return False


def test_api_key():
    print(f"\n{DIVIDER}")
    print("Step 2: Test API Key")
    print(DIVIDER)
    tiny = bytes([
        0xFF,0xD8,0xFF,0xE0,0x00,0x10,0x4A,0x46,0x49,0x46,0x00,0x01,
        0x01,0x00,0x00,0x01,0x00,0x01,0x00,0x00,0xFF,0xDB,0x00,0x43,
        0x00,0x08,0x06,0x06,0x07,0x06,0x05,0x08,0x07,0x07,0x07,0x09,
        0x09,0x08,0x0A,0x0C,0x14,0x0D,0x0C,0x0B,0x0B,0x0C,0x19,0x12,
        0x13,0x0F,0x14,0x1D,0x1A,0x1F,0x1E,0x1D,0x1A,0x1C,0x1C,0x20,
        0x24,0x2E,0x27,0x20,0x22,0x2C,0x23,0x1C,0x1C,0x28,0x37,0x29,
        0x2C,0x30,0x31,0x34,0x34,0x34,0x1F,0x27,0x39,0x3D,0x38,0x32,
        0x3C,0x2E,0x33,0x34,0x32,0xFF,0xC0,0x00,0x0B,0x08,0x00,0x01,
        0x00,0x01,0x01,0x01,0x11,0x00,0xFF,0xC4,0x00,0x1F,0x00,0x00,
        0x01,0x05,0x01,0x01,0x01,0x01,0x01,0x01,0x00,0x00,0x00,0x00,
        0x00,0x00,0x00,0x00,0x01,0x02,0x03,0x04,0x05,0x06,0x07,0x08,
        0x09,0x0A,0x0B,0xFF,0xDA,0x00,0x08,0x01,0x01,0x00,0x00,0x3F,
        0x00,0xFB,0xD5,0xFF,0xD9,
    ])
    from urllib.parse import quote
    try:
        r = requests.post(
            f"{API_BASE.rstrip('/')}/api/wechat/photo",
            data=tiny,
            headers={"Content-Type": "image/jpeg", "x-api-key": API_KEY,
                     "x-group-name": quote("test", safe="")},
            timeout=30,
        )
        if r.ok:
            ok(f"API Key OK: {r.json()}")
            return True
        elif r.status_code == 401:
            fail("API Key rejected (401)")
            return False
        else:
            fail(f"HTTP {r.status_code}: {r.text[:150]}")
            return False
    except Exception as e:
        fail(f"Error: {e}")
        return False


def test_pillow():
    print(f"\n{DIVIDER}")
    print("Step 3: Check Pillow library")
    print(DIVIDER)
    try:
        from PIL import ImageGrab, Image  # noqa: F401
        ok("Pillow installed OK")
        return True
    except ImportError:
        fail("Pillow not installed")
        info("Run this command to install:")
        info("  pip install Pillow")
        return False


def test_clipboard_live():
    print(f"\n{DIVIDER}")
    print("Step 4: Live clipboard test")
    print(DIVIDER)
    info("In WeChat, RIGHT-CLICK any photo -> Copy")
    info("Waiting 30 seconds for clipboard image...")

    from PIL import ImageGrab
    from urllib.parse import quote

    for i in range(30, 0, -1):
        print(f"\r  Waiting {i:3d}s... ", end="", flush=True)
        time.sleep(1)
        try:
            raw = ImageGrab.grabclipboard()
            img = None
            if isinstance(raw, list) and raw:
                # WeChat copies image as a file path list — open it directly
                from PIL import Image
                try:
                    img = Image.open(raw[0])
                    ok(f"Image file in clipboard: {raw[0]}")
                except Exception:
                    pass
            elif raw is not None:
                img = raw
            if img is not None:
                print(flush=True)
                ok(f"Image found in clipboard! Size: {img.size}, Mode: {img.mode}")
                info("Uploading to Drive...")
                buf = io.BytesIO()
                img.convert("RGB").save(buf, format="JPEG", quality=90)
                data = buf.getvalue()
                r = requests.post(
                    f"{API_BASE.rstrip('/')}/api/wechat/photo",
                    data=data,
                    headers={"Content-Type": "image/jpeg", "x-api-key": API_KEY,
                             "x-group-name": quote("test-clipboard", safe="")},
                    timeout=60,
                )
                if r.ok:
                    ok(f"Upload success! {r.json()}")
                    ok("Check Google Drive -> WeChat工地相片 -> test-clipboard")
                    return True
                else:
                    fail(f"Upload failed: {r.status_code} {r.text[:100]}")
                    return False
        except Exception as e:
            print(flush=True)
            fail(f"Clipboard error: {e}")
            return False

    print(flush=True)
    fail("No image detected in clipboard after 30 seconds.")
    info("Make sure you right-clicked a WeChat photo and chose 'Copy'")
    return False


def main():
    print("=" * 60)
    print("  WeChat Clipboard Bridge - Diagnostic Tool")
    print("=" * 60)
    print(f"  Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  API:  {API_BASE}")

    if not test_api_connection():
        print("\n[STOP] Fix connection first.")
        input("\nPress any key to close...")
        sys.exit(1)

    if not test_api_key():
        print("\n[STOP] Fix API key first.")
        input("\nPress any key to close...")
        sys.exit(1)

    if not test_pillow():
        print("\n[STOP] Install Pillow first: pip install Pillow")
        input("\nPress any key to close...")
        sys.exit(1)

    test_clipboard_live()

    print(f"\n{DIVIDER}")
    print("Diagnosis complete!")
    print("Run wechat_bridge_start.bat to start continuous monitoring.")
    print(DIVIDER)
    input("\nPress any key to close...")


if __name__ == "__main__":
    main()
