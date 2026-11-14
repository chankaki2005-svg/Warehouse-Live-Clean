"""One-shot Hong Kong Observatory nine-day forecast sender.

Run this from a Replit Scheduled Deployment at 08:00 Asia/Hong_Kong.
It intentionally does not run a background loop or send catch-up messages.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

HKT = timezone(timedelta(hours=8))
HKO_WEATHER_API_URL = "https://data.weather.gov.hk/weatherAPI/opendata/weather.php?dataType=fnd&lang=tc"
HKO_WEATHER_PAGE_URL = "https://www.hko.gov.hk/tc/wxinfo/currwx/fnd.htm"


def parse_hko_nine_day_forecast(data: dict) -> tuple[list[dict[str, str]], str]:
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
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise ValueError("香港天文台預報資料欄位不完整或格式無效") from exc

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
    except ValueError as exc:
        raise ValueError("香港天文台預報更新時間格式無效") from exc
    return forecasts, updated_at


def normalize_hko_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 8:
        return f"852{digits}"
    if len(digits) == 11 and digits.startswith("852"):
        return digits
    raise ValueError("請填香港 8 位號碼或 852 開頭號碼")


def is_in_send_window(now: datetime) -> bool:
    local_time = now.astimezone(HKT)
    return local_time.hour == 8 and local_time.minute == 0


def format_forecast_message(forecasts: list[dict[str, str]], updated_at: str) -> str:
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
    return "\n".join(lines)


def load_config() -> tuple[str, str, str]:
    phone = os.environ.get("HKO_WEATHER_PHONE", "").strip()
    api_key = os.environ.get("SYNC_API_KEY", "").strip()
    api_base = os.environ.get("WHATSAPP_API_BASE", "").strip().rstrip("/")
    missing = [
        name for name, value in (
            ("HKO_WEATHER_PHONE", phone),
            ("SYNC_API_KEY", api_key),
            ("WHATSAPP_API_BASE", api_base),
        ) if not value
    ]
    if missing:
        raise ValueError(f"尚未設定必要環境變數：{', '.join(missing)}")

    parsed_base = urlsplit(api_base)
    if parsed_base.scheme != "https" or not parsed_base.netloc or parsed_base.path not in ("", "/"):
        raise ValueError("WHATSAPP_API_BASE 必須是正式 API 的 HTTPS 網址")
    return normalize_hko_phone(phone), api_key, api_base


def fetch_hko_nine_day_forecast() -> tuple[list[dict[str, str]], str]:
    request = urllib.request.Request(
        HKO_WEATHER_API_URL,
        headers={"User-Agent": "SinFaiWeatherForecast/1.0"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        data = json.loads(response.read())
    if not isinstance(data, dict):
        raise ValueError("香港天文台預報回應格式無效")
    return parse_hko_nine_day_forecast(data)


def send_forecast(api_base: str, api_key: str, target: str, message: str) -> None:
    request = urllib.request.Request(
        f"{api_base}/api/whatsapp/automation-send-to",
        data=json.dumps({"target": target, "text": message}, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-api-key": api_key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            result = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"WhatsApp API 發送失敗（HTTP {exc.code}）") from exc
    except Exception as exc:
        raise RuntimeError(f"WhatsApp API 發送失敗（{type(exc).__name__}）") from exc

    if not isinstance(result, dict) or result.get("ok") is not True:
        raise RuntimeError("WhatsApp API 未確認已發送")


def run_forecast(now: datetime | None = None) -> bool:
    current_time = now or datetime.now(HKT)
    if not is_in_send_window(current_time):
        print("不在香港時間 08:00 分鐘內，略過；不補發。")
        return False

    target, api_key, api_base = load_config()
    forecasts, updated_at = fetch_hko_nine_day_forecast()
    send_forecast(api_base, api_key, target, format_forecast_message(forecasts, updated_at))
    print(f"九天天氣預報已傳送（香港時間 {current_time.astimezone(HKT):%Y-%m-%d %H:%M:%S}）")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="發送香港天文台九天天氣預報")
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="只檢查排程環境設定，不讀取天氣或發送訊息",
    )
    args = parser.parse_args(argv)

    try:
        if args.validate_only:
            load_config()
            print("排程環境設定有效；沒有讀取天氣資料或發送訊息。")
            return 0
        run_forecast()
        return 0
    except Exception as exc:
        print(f"九天天氣預報工作失敗：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
