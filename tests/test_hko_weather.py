"""Tests for the daily Hong Kong Observatory forecast message."""

import importlib.util
import json
import os
from pathlib import Path
from unittest import TestCase
from unittest.mock import MagicMock, call, patch


MAIN_PATH = Path(__file__).resolve().parents[1] / "main.py"


def load_main():
    spec = importlib.util.spec_from_file_location("test_hko_weather", MAIN_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class HkoWeatherTests(TestCase):
    def test_all_daily_automation_jobs_use_hong_kong_timezone(self):
        main = load_main()
        with patch.object(main.schedule, "every") as every:
            main.setup_schedule()

        self.assertEqual(
            every.return_value.day.at.call_args_list,
            [
                call("01:50", "Asia/Hong_Kong"),
                call("02:00", "Asia/Hong_Kong"),
                call("07:00", "Asia/Hong_Kong"),
                call("18:00", "Asia/Hong_Kong"),
                call("08:00", "Asia/Hong_Kong"),
            ],
        )

    def test_parses_nine_forecast_days_including_significant_rain_probability(self):
        main = load_main()
        weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五",
                    "星期六", "星期日", "星期一", "星期二"]
        api_data = {
            "updateTime": "2026-11-05T07:50:00+08:00",
            "weatherForecast": [
                {
                    "forecastDate": f"202611{day:02d}",
                    "week": weekdays[day - 1],
                    "forecastWind": "東風3級。",
                    "forecastWeather": "部分時間有陽光。",
                    "forecastMintemp": {"value": 22, "unit": "C"},
                    "forecastMaxtemp": {"value": 28, "unit": "C"},
                    "forecastMinrh": {"value": 60, "unit": "percent"},
                    "forecastMaxrh": {"value": 80, "unit": "percent"},
                    "PSR": "低" if day < 9 else "中",
                }
                for day in range(1, 10)
            ],
        }

        forecasts, updated_at = main.parse_hko_nine_day_forecast(api_data)

        self.assertEqual(len(forecasts), 9)
        self.assertEqual(forecasts[0]["date"], "11月1日（一）")
        self.assertEqual(forecasts[-1]["date"], "11月9日（二）")
        self.assertEqual(forecasts[0]["temperature"], "22–28°C")
        self.assertEqual(forecasts[0]["humidity"], "60–80%")
        self.assertEqual(forecasts[0]["weather"], "部分時間有陽光。")
        self.assertEqual(forecasts[0]["wind"], "東風3級。")
        self.assertEqual(forecasts[0]["rainProbability"], "低")
        self.assertEqual(forecasts[-1]["rainProbability"], "中")
        self.assertEqual(updated_at, "2026-11-05 07:50")

    def test_rejects_incomplete_forecasts_instead_of_sending_partial_data(self):
        main = load_main()
        with self.assertRaisesRegex(ValueError, "不是 9 日"):
            main.parse_hko_nine_day_forecast({"weatherForecast": []})

    def test_normalises_hong_kong_phone_numbers(self):
        main = load_main()
        self.assertEqual(main._normalise_hko_phone("6224 9469"), "85262249469")
        self.assertEqual(main._normalise_hko_phone("+852 6224 9469"), "85262249469")
        with self.assertRaises(ValueError):
            main._normalise_hko_phone("12345")

    def test_daily_task_formats_and_sends_through_the_authenticated_whatsapp_api(self):
        main = load_main()
        forecasts = [
            {
                "date": f"11月{day}日（一）",
                "temperature": "22–28°C",
                "humidity": "60–80%",
                "weather": "部分時間有陽光。",
                "wind": "東風3級。",
                "rainProbability": "低",
            }
            for day in range(1, 10)
        ]
        with (
            patch.dict(os.environ, {
                "HKO_WEATHER_PHONE": "62249469",
                "SYNC_API_KEY": "test-sync-key",
                "WHATSAPP_API_BASE": "https://api.example.test",
            }, clear=False),
            patch.object(main, "fetch_hko_nine_day_forecast", return_value=(forecasts, "2026-11-05 07:50")),
            patch.object(main.urllib.request, "urlopen") as urlopen,
        ):
            response = MagicMock()
            response.__enter__.return_value = response
            response.read.return_value = b'{"ok":true}'
            urlopen.return_value = response
            main.task_hko_nine_day_forecast()

        urlopen.assert_called_once()
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.example.test/api/whatsapp/automation-send-to")
        self.assertEqual(request.get_header("X-api-key"), "test-sync-key")
        payload = json.loads(request.data)
        message = payload["text"]
        self.assertIn("九天天氣預報", message)
        self.assertIn("11月9日", message)
        self.assertIn("顯著降雨概率：低", message)
        self.assertIn(main.HKO_WEATHER_PAGE_URL, message)
        self.assertEqual(payload["target"], "85262249469")
