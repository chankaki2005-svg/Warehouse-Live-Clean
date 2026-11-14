"""
新輝 DWG 自動同步腳本
=====================
放在公司 SERVER / Windows 電腦上，定時把指定資料夾的新 DWG 上傳到 Replit。
凌晨 2:00 Replit 的 main.py 會自動批次分析所有已上傳的圖紙。

設定方法
--------
1. 安裝 Python 3.8+ (https://python.org)
2. 安裝依賴：  pip install requests schedule
3. 修改下方「USER CONFIG」區塊的三個設定值
4. 雙擊執行此腳本，或加入 Windows 工作排程器

Windows 工作排程器設定（選做，讓腳本隨 Windows 啟動）
------------------------------------------------------
1. 開啟「工作排程器」
2. 建立基本工作 → 觸發程序選「電腦啟動時」
3. 動作選「啟動程式」→ 程式填 python.exe 路徑，引數填此腳本完整路徑
"""

import os
import time
import logging
import requests
import schedule
from pathlib import Path
from datetime import datetime

# ─────────────────────────────────────────────────────────────────────────────
# USER CONFIG — 只需修改這三行
# ─────────────────────────────────────────────────────────────────────────────

# 1. 公司 SERVER 上存放 DWG 的資料夾（支援子資料夾）
DWG_FOLDER = r"C:\DWG圖紙"          # 例：r"D:\Projects\DWG" 或 r"\\NAS\drawings"

# 2. Replit 伺服器網址（你的 .replit.app 或 .replit.dev 域名）
REPLIT_URL = "https://f167636e-dfa6-4ccf-ad07-f0494e07584e-00-17s7pti3p1y22.sisko.replit.dev"

# 3. API 金鑰（在 Replit Secrets 設定的 SYNC_API_KEY）
API_KEY = "sk-sync-yz56cid6frpmdzer54lq5p8j2ten4xqv"

# ─────────────────────────────────────────────────────────────────────────────
# 進階設定（一般不需要修改）
# ─────────────────────────────────────────────────────────────────────────────

# 每次同步的間隔（分鐘）—— 預設每 30 分鐘掃描一次
SYNC_INTERVAL_MINUTES = 30

# 只同步最近幾小時內修改過的檔案（避免重複上傳舊圖）
ONLY_MODIFIED_WITHIN_HOURS = 24

# 記錄已上傳檔案的狀態檔（避免重複上傳）
STATE_FILE = Path(__file__).parent / ".dwg_sync_state.txt"

# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(Path(__file__).parent / "sync_log.txt", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


def load_uploaded_set() -> set:
    """載入已上傳檔案的紀錄（路徑 + 修改時間）"""
    if not STATE_FILE.exists():
        return set()
    return set(STATE_FILE.read_text(encoding="utf-8").splitlines())


def save_uploaded_set(uploaded: set):
    STATE_FILE.write_text("\n".join(sorted(uploaded)), encoding="utf-8")


def upload_dwg(filepath: Path) -> bool:
    """上傳單個 DWG 到 Replit pending_drawings/"""
    url = f"{REPLIT_URL.rstrip('/')}/api/dxf/upload-pending"
    try:
        with open(filepath, "rb") as f:
            resp = requests.post(
                url,
                headers={"x-api-key": API_KEY},
                files={"file": (filepath.name, f, "application/octet-stream")},
                timeout=60,
            )
        if resp.status_code == 200:
            data = resp.json()
            log.info(f"✅ 上傳成功：{filepath.name} → {data.get('fileName')}")
            return True
        else:
            log.error(f"❌ 上傳失敗：{filepath.name} — HTTP {resp.status_code}: {resp.text[:200]}")
            return False
    except Exception as e:
        log.error(f"❌ 上傳出錯：{filepath.name} — {e}")
        return False


def sync_once():
    """掃描資料夾，上傳新的或修改過的 DWG"""
    folder = Path(DWG_FOLDER)
    if not folder.exists():
        log.warning(f"資料夾不存在：{DWG_FOLDER}")
        return

    uploaded = load_uploaded_set()
    now = time.time()
    cutoff = now - (ONLY_MODIFIED_WITHIN_HOURS * 3600)

    dwg_files = list(folder.rglob("*.dwg")) + list(folder.rglob("*.DWG"))
    new_files = []

    for f in dwg_files:
        try:
            mtime = f.stat().st_mtime
        except OSError:
            continue
        key = f"{f}|{mtime}"
        if key not in uploaded and mtime >= cutoff:
            new_files.append((f, mtime, key))

    if not new_files:
        log.info(f"掃描完成，沒有新圖紙需要上傳（共掃描 {len(dwg_files)} 個 DWG）")
        return

    log.info(f"發現 {len(new_files)} 個新／修改圖紙，開始上傳...")
    ok_count = 0
    for f, _, key in new_files:
        if upload_dwg(f):
            uploaded.add(key)
            ok_count += 1
        time.sleep(0.5)  # 避免過快

    save_uploaded_set(uploaded)
    log.info(f"同步完成：{ok_count}/{len(new_files)} 個成功上傳")


def main():
    log.info("=" * 60)
    log.info("新輝 DWG 同步服務啟動")
    log.info(f"監控資料夾：{DWG_FOLDER}")
    log.info(f"Replit 伺服器：{REPLIT_URL}")
    log.info(f"同步間隔：每 {SYNC_INTERVAL_MINUTES} 分鐘")
    log.info("=" * 60)

    # 啟動時立即執行一次
    sync_once()

    # 排程定時執行
    schedule.every(SYNC_INTERVAL_MINUTES).minutes.do(sync_once)

    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
