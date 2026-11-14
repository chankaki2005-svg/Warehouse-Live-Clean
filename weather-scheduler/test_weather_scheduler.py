import json
import os
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

import main


def sample_hko_data():
    weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五",
                "星期六", "星期日", "星期一", "星期二"]
    return {
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
                "PSR": "低",
            }
            for day in range(1, 10)
        ],
    }


class WeatherSchedulerTests(unittest.TestCase):
    def test_accepts_only_nine_forecast_days(self):
        forecasts, updated = main.parse_hko_nine_day_forecast(sample_hko_data())
        self.assertEqual(len(forecasts), 9)
        self.assertEqual(forecasts[0]["date"], "11月1日（一）")
        self.assertEqual(updated, "2026-11-05 07:50")

        incomplete = sample_hko_data()
        incomplete["weatherForecast"].pop()
        with self.assertRaisesRegex(ValueError, "不是 9 日"):
            main.parse_hko_nine_day_forecast(incomplete)

    def test_hong_kong_phone_normalization(self):
        self.assertEqual(main.normalize_hko_phone("6224 9469"), "85262249469")
        self.assertEqual(main.normalize_hko_phone("+852 6224 9469"), "85262249469")
        with self.assertRaises(ValueError):
            main.normalize_hko_phone("12345")

    def test_send_window_uses_hong_kong_time_and_never_catches_up_later(self):
        self.assertTrue(main.is_in_send_window(datetime.fromisoformat("2026-11-13T08:00:59+08:00")))
        self.assertFalse(main.is_in_send_window(datetime.fromisoformat("2026-11-13T08:01:00+08:00")))
        self.assertFalse(main.is_in_send_window(datetime.fromisoformat("2026-11-13T10:19:00+08:00")))

    def test_late_invocation_skips_without_fetch_or_send(self):
        with (
            patch.object(main, "fetch_hko_nine_day_forecast") as fetch,
            patch.object(main, "send_forecast") as send,
        ):
            result = main.run_forecast(datetime.fromisoformat("2026-11-13T10:19:00+08:00"))

        self.assertFalse(result)
        fetch.assert_not_called()
        send.assert_not_called()

    def test_sends_to_the_authenticated_existing_api(self):
        with patch.dict(os.environ, {
            "HKO_WEATHER_PHONE": "6224 9469",
            "SYNC_API_KEY": "test-key",
            "WHATSAPP_API_BASE": "https://api.example.test",
        }):
            target, key, base = main.load_config()

        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"ok":true}'
        with patch.object(main.urllib.request, "urlopen", return_value=response) as urlopen:
            main.send_forecast(base, key, target, "測試天氣")

        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.example.test/api/whatsapp/automation-send-to")
        self.assertEqual(request.get_header("X-api-key"), "test-key")
        self.assertEqual(json.loads(request.data), {"target": "85262249469", "text": "測試天氣"})


if __name__ == "__main__":
    unittest.main()
