"""
新輝雲端自動化後台
===================
功能：
  - 凌晨 01:50  從 Dropbox /新輝DWG 下載最新 DWG 圖紙
  - 凌晨 02:00  自動分析 DWG 圖紙（透過 API 引擎，支援 TG-PIPE DN* 圖層）並發送 WhatsApp 結果
  - 早上  07:00  自動發送倉庫日報摘要到 WhatsApp 群組

設定需要的環境變數（Replit Secrets）：
  ULTRAMSG_INSTANCE_ID  — UltraMsg 實例 ID
  ULTRAMSG_TOKEN        — UltraMsg API Token
  WA_INVENTORY_GROUP    — WhatsApp 目標（群組 JID 或電話號碼，如 85298765432@c.us）
                          （若未設，自動嘗試 WA_ANOMALY_GROUP → WA_TARGET）
  HKO_WEATHER_PHONE     — 每日九天天氣預報的個人 WhatsApp 收件號碼（香港號碼可只填 8 位）
  SYNC_API_KEY          — 自動化後台呼叫 Baileys WhatsApp API 的共用密鑰
  DROPBOX_DWG_FOLDER    — Dropbox DWG 資料夾路徑（預設 /新輝DWG）
  WHATSAPP_API_BASE     — WhatsApp API 網址（天氣發送、狀態檢查及重連頁共用）
"""

import os
import re
import schedule
import time
import urllib.error
import urllib.request
import urllib.parse
import json as _json
from datetime import datetime, date, timedelta, timezone
from pathlib import Path

# ── 資料夾設定 ────────────────────────────────────────────────
DAILY_REPORTS_DIR = Path("daily_reports")
HKO_WEATHER_API_URL = "https://data.weather.gov.hk/weatherAPI/opendata/weather.php?dataType=fnd&lang=tc"
HKO_WEATHER_PAGE_URL = "https://www.hko.gov.hk/tc/wxinfo/currwx/fnd.htm"
HKT = timezone(timedelta(hours=8))

# ── Dropbox 同步設定 ──────────────────────────────────────────
DROPBOX_DWG_FOLDER = os.environ.get("DROPBOX_DWG_FOLDER", "/新輝DWG")
INTERNAL_API_BASE  = "http://localhost:80"
# WhatsApp 的 Baileys session 在正式 API server 上，不能用自動化後台
# 所在環境的 localhost 查詢，否則開發環境斷線會誤報為正式環境斷線。
# WHATSAPP_API_BASE 是唯一的可調整來源。自動化後台和 API server 是
# 分開部署，不能使用後台本身的 REPLIT_DOMAINS 作為 API URL。未設定時
# 保留目前可用的正式 API 後備網址；重連頁 URL 由同一個 base 生成。
DEFAULT_WHATSAPP_API_BASE = "https://warehouse-live--chankaki2005.replit.app"


def _normalise_public_base(raw: str) -> str:
    value = raw.strip()
    if not value:
        return ""
    if "://" not in value:
        value = f"https://{value}"
    return value.rstrip("/")


def _get_public_api_base() -> str:
    configured = os.environ.get("WHATSAPP_API_BASE", "")
    if configured.strip():
        return _normalise_public_base(configured)
    return DEFAULT_WHATSAPP_API_BASE


WHATSAPP_API_BASE = _get_public_api_base()


def _whatsapp_setup_url() -> str:
    return f"{WHATSAPP_API_BASE}/api/whatsapp/setup" if WHATSAPP_API_BASE else ""


def _whatsapp_status_url() -> str:
    return f"{WHATSAPP_API_BASE}/api/whatsapp/status" if WHATSAPP_API_BASE else ""


DAILY_REPORTS_DIR.mkdir(exist_ok=True)


# ══════════════════════════════════════════════════════════════
#  WhatsApp 發送（UltraMsg）
# ══════════════════════════════════════════════════════════════

def send_whatsapp_message(
    message: str,
    target_override: str | None = None,
    log_target: bool = True,
) -> bool:
    """
    透過 UltraMsg 發送 WhatsApp 文字訊息。
    目標從環境變數讀取：WA_INVENTORY_GROUP > WA_ANOMALY_GROUP > WA_TARGET
    """
    instance_id = os.environ.get("ULTRAMSG_INSTANCE_ID", "")
    token       = os.environ.get("ULTRAMSG_TOKEN", "")
    target = target_override if target_override is not None else (
        os.environ.get("WA_INVENTORY_GROUP") or
        os.environ.get("WA_ANOMALY_GROUP")   or
        os.environ.get("WA_TARGET")          or ""
    )

    if not instance_id or not token:
        print("  ⚠️  ULTRAMSG_INSTANCE_ID / ULTRAMSG_TOKEN 未設定，跳過 WhatsApp 通知")
        return False
    if not target:
        print("  ⚠️  WA_INVENTORY_GROUP / WA_ANOMALY_GROUP / WA_TARGET 均未設定，跳過 WhatsApp 通知")
        return False

    segment = instance_id if instance_id.startswith("instance") else f"instance{instance_id}"
    url     = f"https://api.ultramsg.com/{segment}/messages/chat"

    payload = urllib.parse.urlencode({
        "token": token,
        "to":    target,
        "body":  message,
    }).encode()

    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = _json.loads(resp.read())
            if result.get("sent") == "true" or result.get("id"):
                if log_target:
                    print(f"  ✅ WhatsApp 訊息已發送至：{target}")
                else:
                    print("  ✅ WhatsApp 訊息已發送")
                return True
            if log_target:
                print(f"  ⚠️  UltraMsg 回應異常：{result}")
            else:
                print("  ⚠️  UltraMsg 未確認訊息已發送")
            return False
    except Exception as e:
        if log_target:
            print(f"  ❌ WhatsApp 發送失敗：{e}")
        else:
            print("  ❌ WhatsApp 發送失敗；收件資料未寫入日誌")
        return False


# ══════════════════════════════════════════════════════════════
#  任務三：香港天文台九天天氣預報 WhatsApp
# ══════════════════════════════════════════════════════════════

def parse_hko_nine_day_forecast(data: dict) -> tuple[list[dict[str, str]], str]:
    """Validate HKO's official forecast API response and format its nine days."""
    entries = data.get("weatherForecast")
    if not isinstance(entries, list) or len(entries) != 9:
        count = len(entries) if isinstance(entries, list) else 0
        raise ValueError(f"香港天文台預報日數不是 9 日（讀到 {count} 日）")

    forecasts: list[dict[str, str]] = []
    valid_rain_levels = {"高", "中高", "中", "中低", "低"}
    for entry in entries:
        try:
            forecast_date = datetime.strptime(entry["forecastDate"], "%Y%m%d")
            weekday = entry["week"].replace("星期", "").replace("天", "日")
            minimum = entry["forecastMintemp"]["value"]
            maximum = entry["forecastMaxtemp"]["value"]
            min_humidity = entry["forecastMinrh"]["value"]
            max_humidity = entry["forecastMaxrh"]["value"]
            wind = entry["forecastWind"].strip()
            weather = entry["forecastWeather"].strip()
            rain_probability = entry["PSR"].strip()
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            raise ValueError("香港天文台預報資料欄位不完整或格式無效") from e

        numeric_values = (minimum, maximum, min_humidity, max_humidity)
        if (
            weekday not in {"一", "二", "三", "四", "五", "六", "日"}
            or not all(isinstance(value, (int, float)) for value in numeric_values)
            or not wind
            or not weather
            or rain_probability not in valid_rain_levels
        ):
            raise ValueError("香港天文台預報資料欄位不完整或格式無效")

        forecasts.append({
            "date": f"{forecast_date.month}月{forecast_date.day}日（{weekday}）",
            "wind": wind,
            "weather": weather,
            "temperature": f"{minimum}–{maximum}°C",
            "humidity": f"{min_humidity}–{max_humidity}%",
            "rainProbability": rain_probability,
        })

    update_time = data.get("updateTime")
    if not isinstance(update_time, str) or not update_time.strip():
        raise ValueError("香港天文台預報沒有更新時間")
    try:
        updated_at = datetime.fromisoformat(update_time).astimezone(HKT).strftime("%Y-%m-%d %H:%M")
    except ValueError as e:
        raise ValueError("香港天文台預報更新時間格式無效") from e
    return forecasts, updated_at


def fetch_hko_nine_day_forecast() -> tuple[list[dict[str, str]], str]:
    """Fetch and validate the latest nine-day forecast from the official HKO API."""
    request = urllib.request.Request(
        HKO_WEATHER_API_URL,
        headers={"User-Agent": "SinFaiWeatherForecast/1.0"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        data = _json.loads(response.read())
    if not isinstance(data, dict):
        raise ValueError("香港天文台預報回應格式無效")
    return parse_hko_nine_day_forecast(data)


def _normalise_hko_phone(phone: str) -> str:
    """Return a Hong Kong WhatsApp target in international digit format."""
    phone_digits = re.sub(r"\D", "", phone)
    if len(phone_digits) == 8:
        return f"852{phone_digits}"
    if len(phone_digits) == 11 and phone_digits.startswith("852"):
        return phone_digits
    raise ValueError("請填香港 8 位號碼或 852 開頭號碼")


def task_hko_nine_day_forecast():
    """Fetch the current HKO nine-day forecast and send it to the configured phone."""
    phone = os.environ.get("HKO_WEATHER_PHONE", "").strip()
    if not phone:
        print("  ⚠️  HKO_WEATHER_PHONE 未設定，跳過九天天氣預報")
        return

    try:
        phone_digits = _normalise_hko_phone(phone)
    except ValueError as e:
        print(f"  ❌ HKO_WEATHER_PHONE 格式無效；{e}")
        return

    try:
        forecasts, updated_at = fetch_hko_nine_day_forecast()
        lines = [
            "🌦 香港天文台｜九天天氣預報",
            f"更新時間：{updated_at}（香港時間）",
            "",
        ]
        for forecast in forecasts:
            lines.append(
                f"📅 {forecast['date']}  {forecast['temperature']}｜濕度 {forecast['humidity']}"
            )
            lines.append(f"　{forecast['weather']}｜{forecast['wind']}")
            lines.append(f"　顯著降雨概率：{forecast['rainProbability']}")
        lines.extend(["", f"來源：{HKO_WEATHER_PAGE_URL}"])
        message = "\n".join(lines)
    except Exception as e:
        print(f"  ❌ 香港天文台九天天氣預報讀取失敗：{e}")
        return

    if send_hko_forecast_via_whatsapp_api(phone_digits, message):
        print(f"  ✅ 九天天氣預報已傳送（香港時間 {datetime.now(HKT):%Y-%m-%d %H:%M}）")


def send_hko_forecast_via_whatsapp_api(target: str, message: str) -> bool:
    """Send the forecast through the authenticated Baileys API, never UltraMsg."""
    api_key = os.environ.get("SYNC_API_KEY", "").strip()
    api_base = _get_public_api_base()
    if not api_key:
        print("  ❌ SYNC_API_KEY 未設定，無法透過 WhatsApp API 發送天氣預報")
        return False
    if not api_base:
        print("  ❌ WhatsApp API 網址未設定，無法發送天氣預報")
        return False

    request = urllib.request.Request(
        f"{api_base}/api/whatsapp/automation-send-to",
        data=_json.dumps({"target": target, "text": message}).encode(),
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            result = _json.loads(response.read())
        if isinstance(result, dict) and result.get("ok") is True:
            return True
        print("  ⚠️ WhatsApp API 未確認天氣預報已發送")
        return False
    except urllib.error.HTTPError as e:
        print(f"  ❌ WhatsApp API 發送失敗（HTTP {e.code}）")
    except Exception as e:
        # Do not log request URLs, response bodies, or the private recipient.
        print(f"  ❌ WhatsApp API 發送失敗（{type(e).__name__}）")
    return False


# ══════════════════════════════════════════════════════════════
#  任務零：從 Dropbox 同步 DWG 圖紙
# ══════════════════════════════════════════════════════════════

def task_sync_from_dropbox():
    """凌晨 1:50 執行：從 Dropbox 下載最新 DWG 圖紙到 pending_drawings/。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'='*60}")
    print(f"[任務零] Dropbox DWG 同步 — {now}")
    print(f"{'='*60}")
    print(f"  Dropbox 資料夾：{DROPBOX_DWG_FOLDER}")

    try:
        folder_enc = urllib.parse.quote(DROPBOX_DWG_FOLDER)
        url = f"{INTERNAL_API_BASE}/api/dxf/sync-from-dropbox?folder={folder_enc}"
        req = urllib.request.Request(
            url, method="POST",
            headers={
                "Content-Type": "application/json",
                "x-api-key": os.environ.get("SYNC_API_KEY", ""),
            },
            data=b"{}",
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = _json.loads(resp.read())

        downloaded = data.get("downloaded", 0)
        files      = data.get("files", [])
        print(f"  ✅ 同步完成：下載 {downloaded} 個 DWG")
        for f in files:
            print(f"     - {f}")

    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="ignore")[:300]
        print(f"  ⚠️  API 回應錯誤 {e.code}：{body}")
        print("  （繼續使用現有 pending_drawings/ 內的檔案）")
    except Exception as e:
        print(f"  ❌ Dropbox 同步失敗：{e}")
        print("  （繼續使用現有 pending_drawings/ 內的檔案）")


# ══════════════════════════════════════════════════════════════
#  任務一：自動分析 DWG 圖紙（透過 API 引擎）
# ══════════════════════════════════════════════════════════════

def task_analyze_drawings():
    """
    凌晨 2:00 執行：觸發 /api/dxf/batch-analyze-pending。
    API server 立即回傳 202（不阻塞），在背景完成 DWG 轉換 + 分析，
    完成後自動透過 Baileys WhatsApp 發送報告。
    本函數只負責觸發，不等待結果（避免代理超時）。
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'='*60}")
    print(f"[任務一] DWG 圖紙自動分析 — {now}")
    print(f"{'='*60}")

    try:
        req = urllib.request.Request(
            f"{INTERNAL_API_BASE}/api/dxf/batch-analyze-pending",
            data=_json.dumps({
                "keywords": "GI,PE,喉,鍍鋅,銅,不鏽鋼,閘,曲,叉,梳杰",
            }).encode(),
            headers={
                "Content-Type": "application/json",
                "x-api-key":    os.environ.get("SYNC_API_KEY", ""),
            },
            method="POST",
        )
        # Short timeout — server responds 202 immediately, then works in background
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = _json.loads(resp.read())

        queued = data.get("queued", 0)
        if queued == 0:
            print("  ⚠️  佇列為空，跳過分析。")
            print("  提示：請先透過 Dropbox 同步或 App 上傳圖紙到佇列。")
        else:
            print(f"  ✅ 已觸發分析 {queued} 個 DWG 檔案（背景處理中）")
            print(f"  📱 分析完成後將自動發送 WhatsApp 報告。")

    except urllib.error.HTTPError as e:
        err_body = e.read().decode(errors="ignore")[:300]
        print(f"  ❌ batch-analyze API 錯誤 {e.code}：{err_body}")
    except Exception as e:
        print(f"  ❌ batch-analyze 觸發失敗：{e}")


# ══════════════════════════════════════════════════════════════
#  任務二：早上 7:00 倉庫日報 WhatsApp 通知
# ══════════════════════════════════════════════════════════════

def _get_warehouse_data(report_date: str) -> dict:
    """從 API 取得昨日倉庫數據（呼叫內部 warehouse daily-report 端點）。"""
    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        print("  ⚠️  DATABASE_URL 未設定，使用空數據")
        return {"date": report_date, "total_in": 0, "total_out": 0,
                "transactions": [], "low_stock_warnings": []}
    try:
        import psycopg2
        from datetime import timezone
        HKT = timezone(timedelta(hours=8))
        conn = psycopg2.connect(db_url)
        cur  = conn.cursor()

        day_start = datetime.strptime(report_date, "%Y-%m-%d").replace(tzinfo=HKT)
        day_end   = day_start + timedelta(days=1)
        ts_start  = int(day_start.timestamp() * 1000)
        ts_end    = int(day_end.timestamp()   * 1000)

        cur.execute(
            "SELECT item_name, type, quantity, note, timestamp "
            "FROM transactions WHERE timestamp >= %s AND timestamp < %s "
            "ORDER BY timestamp ASC",
            (ts_start, ts_end),
        )
        rows = cur.fetchall()
        total_in  = sum(r[2] for r in rows if r[1] == "in")
        total_out = sum(r[2] for r in rows if r[1] == "out")
        transactions = []
        for item_name, t_type, qty, note, ts in rows:
            dt = datetime.fromtimestamp(ts / 1000, tz=HKT)
            transactions.append({
                "time": dt.strftime("%H:%M"), "type": "IN" if t_type == "in" else "OUT",
                "item": item_name, "qty": qty, "note": note or "",
            })

        cur.execute(
            "SELECT name, quantity, min_stock, unit FROM items "
            "WHERE min_stock > 0 AND quantity <= min_stock ORDER BY quantity ASC LIMIT 20"
        )
        low_stock = [
            {"item": r[0], "balance": r[1], "min_level": r[2], "unit": r[3]}
            for r in cur.fetchall()
        ]
        cur.close(); conn.close()
        return {"date": report_date, "total_in": total_in, "total_out": total_out,
                "transactions": transactions, "low_stock_warnings": low_stock}

    except Exception as e:
        print(f"  ❌ 資料庫查詢失敗：{e}")
        return {"date": report_date, "total_in": 0, "total_out": 0,
                "transactions": [], "low_stock_warnings": []}


def _get_whatsapp_status() -> str | None:
    """從正式 API server 取得 WhatsApp 連線狀態。"""
    if not WHATSAPP_API_BASE:
        print("  ⚠️  未設定正式 WhatsApp API 網址（請設定 WHATSAPP_API_BASE）")
        return None

    status_url = _whatsapp_status_url()
    try:
        req = urllib.request.Request(status_url, method="GET")
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = _json.loads(resp.read())

        status = payload.get("status")
        if isinstance(status, str) and status:
            print(f"  WhatsApp 正式 API 狀態：{status}")
            return status

        print("  ⚠️  WhatsApp 狀態 API 回應缺少有效 status")
    except Exception as e:
        print(f"  ⚠️  無法取得 WhatsApp 正式 API 狀態：{e}")
    return None


def task_morning_whatsapp_report():
    """
    早上 7:00 執行：
      1. 檢查正式 WhatsApp 連線狀態
      2. 讀取昨日倉庫數據
      3. 組成 WhatsApp 文字摘要並發送
      4. 嘗試透過 Baileys 發送 PDF 日報（如已連接 WhatsApp）
    """
    now_str     = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    report_date = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")

    print(f"\n{'='*60}")
    print(f"[任務二] WhatsApp 倉庫日報通知 — {now_str}")
    print(f"{'='*60}")

    wa_status = _get_whatsapp_status()
    data      = _get_warehouse_data(report_date)
    warnings = data.get("low_stock_warnings", [])
    total_in  = data["total_in"]
    total_out = data["total_out"]
    net       = total_in - total_out
    net_str   = f"+{net}" if net >= 0 else str(net)
    tx_count  = len(data["transactions"])

    # ── 文字摘要 ──────────────────────────────────────────────
    lines = [
        f"📦 *新輝倉庫日報*  {report_date}",
        "━━━━━━━━━━━━━━━━━━━━",
        f"🟢 入庫：*{total_in}* 件",
        f"🔴 出庫：*{total_out}* 件",
        f"📊 淨變動：*{net_str}* 件",
        f"📋 交易記錄：{tx_count} 筆",
    ]

    if wa_status != "connected":
        status_label = wa_status or "無法取得狀態"
        setup_url = _whatsapp_setup_url()
        connection_warning = (
            "⚠️ 無法連線至正式 API，未能確認 WhatsApp 狀態。"
            if wa_status is None
            else "⚠️ 正式 API 回報 WhatsApp 未連線。"
        )
        lines = [
            "🚨 *WhatsApp 連線警告*",
            f"目前狀態：*{status_label}*",
            connection_warning,
            "相片接收可能已停止，請立即掃描 QR 碼重連：",
        ] + lines
        if setup_url:
            lines.insert(4, setup_url)
        else:
            lines.insert(4, "⚠️ 未設定重連頁網址，請設定 WHATSAPP_API_BASE。")
        lines.insert(5, "")

    if warnings:
        lines.append("")
        lines.append(f"⚠️ *低庫存警告（{len(warnings)} 項）*")
        for w in warnings[:8]:
            unit = w.get("unit", "個")
            lines.append(f"  • {w['item']} — 剩 {w['balance']} {unit}（最低 {w['min_level']}）")
        if len(warnings) > 8:
            lines.append(f"  … 另外 {len(warnings) - 8} 項，請查看 App")
    else:
        lines.append("")
        lines.append("✅ 所有物料庫存正常")

    lines.append("")
    lines.append("_由 新輝雲端系統 自動發送_")

    message = "\n".join(lines)
    print(f"  發送文字摘要...")
    ok = send_whatsapp_message(message)
    if not ok:
        print("  ⚠️  文字摘要未能發送（請檢查環境變數設定）")

    # ── 嘗試發送 PDF 日報（透過 Baileys WhatsApp 客戶端）──────
    wa_target = (
        os.environ.get("WA_INVENTORY_GROUP") or
        os.environ.get("WA_ANOMALY_GROUP")   or
        os.environ.get("WA_TARGET")          or ""
    )
    if wa_target:
        try:
            print(f"  嘗試發送 PDF 日報至：{wa_target}")
            pdf_url = f"{INTERNAL_API_BASE}/api/warehouse/daily-report?date={report_date}"
            pdf_req = urllib.request.Request(pdf_url, method="GET")
            with urllib.request.urlopen(pdf_req, timeout=60) as pdf_resp:
                if pdf_resp.status == 200:
                    pdf_bytes = pdf_resp.read()
                    filename  = f"warehouse_report_{report_date}.pdf"

                    # ── Multipart form-data for /api/whatsapp/send-report ──
                    boundary = b"----PythonBoundary"
                    body_parts = []

                    def field(name: str, value: str) -> bytes:
                        return (
                            b"--" + boundary + b"\r\n"
                            b"Content-Disposition: form-data; name=\"" + name.encode() + b"\"\r\n\r\n"
                            + value.encode() + b"\r\n"
                        )

                    body_parts.append(field("target",  wa_target))
                    body_parts.append(field("caption", f"新輝倉庫日報 {report_date}"))
                    body_parts.append(
                        b"--" + boundary + b"\r\n"
                        b"Content-Disposition: form-data; name=\"file\"; filename=\"" + filename.encode() + b"\"\r\n"
                        b"Content-Type: application/pdf\r\n\r\n"
                        + pdf_bytes + b"\r\n"
                    )
                    body_parts.append(b"--" + boundary + b"--\r\n")
                    body = b"".join(body_parts)

                    wa_req = urllib.request.Request(
                        f"{INTERNAL_API_BASE}/api/whatsapp/send-report",
                        data=body,
                        method="POST",
                    )
                    wa_req.add_header("Content-Type", f"multipart/form-data; boundary={boundary.decode()}")

                    with urllib.request.urlopen(wa_req, timeout=30) as wa_resp:
                        result = _json.loads(wa_resp.read())
                        if result.get("ok"):
                            print(f"  ✅ PDF 日報已透過 WhatsApp 發送")
                        else:
                            print(f"  ⚠️  PDF 發送回應：{result}")
                else:
                    print(f"  ⚠️  PDF 生成失敗，HTTP {pdf_resp.status}")
        except Exception as e:
            print(f"  ⚠️  PDF 發送失敗（WhatsApp 可能未連接）：{e}")
            print("  （文字摘要已透過 UltraMsg 發送）")


# ══════════════════════════════════════════════════════════════
#  任務三：下午 18:00 合併當日現場報告 PDF
# ══════════════════════════════════════════════════════════════

def task_merge_site_reports():
    """
    下午 18:00 執行：
      1. 呼叫 /api/site-reports/merge-daily
         → 將同一群組當日所有 PDF 合併成一份，並刪除原件
      2. 若合併成功，透過 WhatsApp 發送通知及 Drive 連結
    """
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    today   = date.today().strftime("%Y-%m-%d")

    print(f"\n{'='*60}")
    print(f"[任務三] 現場報告合併 — {now_str}")
    print(f"{'='*60}")

    try:
        req = urllib.request.Request(
            f"{INTERNAL_API_BASE}/api/site-reports/merge-daily",
            data=_json.dumps({"date": today, "deleteOriginals": True}).encode(),
            headers={
                "Content-Type": "application/json",
                "x-api-key": os.environ.get("SYNC_API_KEY", ""),
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=300) as resp:
            data = _json.loads(resp.read())
    except Exception as e:
        print(f"  ❌ merge-daily API 失敗：{e}")
        return

    merged = data.get("mergedFiles", [])
    if not merged:
        print("  ℹ️  今日沒有現場報告需要合併。")
        return

    print(f"  ✅ 處理了 {len(merged)} 個群組：")
    lines = [f"📋 *現場報告合輯*  {today}", "━━━━━━━━━━━━━━━━━━━━"]
    any_merged = False
    for m in merged:
        group   = m.get("groupName", "")
        count   = m.get("fileCount", 0)
        link    = m.get("driveLink", "")
        skipped = m.get("skipped", "")
        if skipped:
            print(f"     - {group}：{count} 份（{skipped}）")
            lines.append(f"• {group}：{count} 份報告")
            if link:
                lines.append(f"  {link}")
        else:
            print(f"     - {group}：已合併 {count} 份 → {link}")
            lines.append(f"• {group}：共 {count} 份已合併")
            if link:
                lines.append(f"  {link}")
            any_merged = True

    if not any_merged:
        print("  ℹ️  無需合併（每組僅一份）。")

    lines.append("")
    lines.append("_由 新輝雲端系統 自動發送_")
    message = "\n".join(lines)

    # ── 嘗試透過 Baileys 發送到現場報告群組 ──────────────────
    site_jid = ""
    try:
        jid_path = "./data/site-report-jids.json"
        if os.path.exists(jid_path):
            with open(jid_path, "r") as f:
                jids = _json.load(f)
            if jids:
                site_jid = jids[0]
    except Exception:
        pass

    if site_jid:
        try:
            wa_req = urllib.request.Request(
                f"{INTERNAL_API_BASE}/api/whatsapp/send-text",
                data=_json.dumps({"target": site_jid, "body": message}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(wa_req, timeout=30) as wa_resp:
                result = _json.loads(wa_resp.read())
                if result.get("ok") or result.get("sent"):
                    print(f"  ✅ 合併通知已發送至現場報告群組：{site_jid}")
                else:
                    print(f"  ⚠️  Baileys 回應異常：{result}")
        except Exception as e:
            print(f"  ⚠️  Baileys 發送失敗，改用 UltraMsg：{e}")
            send_whatsapp_message(message)
    else:
        send_whatsapp_message(message)


# ══════════════════════════════════════════════════════════════
#  定時排程設定
# ══════════════════════════════════════════════════════════════

def setup_schedule():
    """設定每日定時任務（香港時間）。"""
    schedule.every().day.at("01:50", "Asia/Hong_Kong").do(task_sync_from_dropbox)
    schedule.every().day.at("02:00", "Asia/Hong_Kong").do(task_analyze_drawings)
    schedule.every().day.at("07:00", "Asia/Hong_Kong").do(task_morning_whatsapp_report)
    schedule.every().day.at("18:00", "Asia/Hong_Kong").do(task_merge_site_reports)
    schedule.every().day.at("08:00", "Asia/Hong_Kong").do(task_hko_nine_day_forecast)

    print("定時任務已設定（香港時間）：")
    print(f"  凌晨 01:50 — Dropbox DWG 同步（{DROPBOX_DWG_FOLDER}）")
    print("  凌晨 02:00 — DWG 圖紙自動分析 + WhatsApp 結果")
    print("  早上  07:00 — 倉庫日報 WhatsApp 通知（文字摘要 + PDF）")
    print("  早上  08:00 — 香港天文台九天天氣預報 WhatsApp")
    print("  下午  18:00 — 現場報告合輯（合併各工地群組當日 PDF）")


# ══════════════════════════════════════════════════════════════
#  主程式入口
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("╔══════════════════════════════════════════╗")
    print("║   新輝雲端自動化後台  v2.0               ║")
    print("║   Sin Fai Cloud Automation Backend       ║")
    print("╚══════════════════════════════════════════╝")
    print(f"啟動時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

    # ── 環境變數檢查 ──────────────────────────────────────────
    wa_ok  = bool(os.environ.get("ULTRAMSG_INSTANCE_ID") and os.environ.get("ULTRAMSG_TOKEN"))
    tgt_ok = bool(
        os.environ.get("WA_INVENTORY_GROUP") or
        os.environ.get("WA_ANOMALY_GROUP")   or
        os.environ.get("WA_TARGET")
    )
    db_ok  = bool(os.environ.get("DATABASE_URL"))

    print("環境變數狀態：")
    print(f"  UltraMsg（其他通知）：{'✅ 已設定' if wa_ok  else '❌ 未設定（ULTRAMSG_INSTANCE_ID / ULTRAMSG_TOKEN）'}")
    print(f"  九天天氣傳送方式   ：{'Baileys API ✅' if os.environ.get('SYNC_API_KEY') else '❌ 未設定 SYNC_API_KEY'}")
    print(f"  WhatsApp 目標   ：{'✅ 已設定' if tgt_ok else '❌ 未設定（請設定 WA_INVENTORY_GROUP）'}")
    print(f"  九天天氣收件人  ：{'✅ 已設定' if os.environ.get('HKO_WEATHER_PHONE') else '❌ 未設定'}")
    print(f"  資料庫連接      ：{'✅ 已設定' if db_ok  else '❌ 未設定（DATABASE_URL）'}")
    print(f"  Dropbox 資料夾  ：{DROPBOX_DWG_FOLDER}\n")

    # ── 設定定時排程 ──────────────────────────────────────────
    setup_schedule()
    print(f"{'─'*60}")
    print("系統進入待機狀態，等待定時觸發...")
    print("（按 Ctrl+C 停止）\n")

    # ── 主循環 ────────────────────────────────────────────────
    while True:
        schedule.run_pending()
        time.sleep(30)
