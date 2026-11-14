"""Regression tests for the morning WhatsApp API URL configuration."""

import importlib.util
import os
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch


MAIN_PATH = Path(__file__).resolve().parents[1] / "main.py"


def load_main():
    spec = importlib.util.spec_from_file_location("test_main_whatsapp_config", MAIN_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WhatsAppApiBaseTests(TestCase):
    def test_uses_known_api_server_when_no_override_is_configured(self):
        with patch.dict(os.environ, {"WHATSAPP_API_BASE": ""}, clear=False):
            main = load_main()

        expected_base = "https://warehouse-live--chankaki2005.replit.app"
        self.assertEqual(main.WHATSAPP_API_BASE, expected_base)
        self.assertEqual(
            main._whatsapp_status_url(),
            f"{expected_base}/api/whatsapp/status",
        )
        self.assertEqual(
            main._whatsapp_setup_url(),
            f"{expected_base}/api/whatsapp/setup",
        )


    def test_single_override_controls_status_and_setup_urls(self):
        with patch.dict(
            os.environ,
            {
                "WHATSAPP_API_BASE": "https://new-api.example.test///",
                # This legacy setting must no longer be able to split the URLs.
                "WHATSAPP_SETUP_URL": "https://wrong-host.example.test/setup",
            },
            clear=False,
        ):
            main = load_main()

        expected_base = "https://new-api.example.test"
        self.assertEqual(main.WHATSAPP_API_BASE, expected_base)
        self.assertEqual(
            main._whatsapp_status_url(),
            f"{expected_base}/api/whatsapp/status",
        )
        self.assertEqual(
            main._whatsapp_setup_url(),
            f"{expected_base}/api/whatsapp/setup",
        )