import json
import re
import threading
import unittest
from collections import Counter
from types import SimpleNamespace
from unittest.mock import patch

from test_plugin import plugin


class LocalizationTests(unittest.TestCase):
    def test_host_language_uses_direct_api_when_available(self):
        host = SimpleNamespace(app_language=lambda: "zh_Hant_HK",
                               app_info=lambda: {"language": "zh_CN"})
        with patch.object(plugin.orca, "host", host):
            self.assertEqual(plugin.orca_language(), "zh-TW")

    def test_legacy_host_language_and_fallback(self):
        for code, expected in [("pt_BR", "pt-BR"), ("ca_ES", "ca"), ("en_GB", "en"), ("xx_XX", "en")]:
            with self.subTest(code=code):
                host = SimpleNamespace(app_info=lambda: {"language": code})
                with patch.object(plugin.orca, "host", host):
                    self.assertEqual(plugin.orca_language(), expected)

    def test_all_locales_render_both_pages_and_preserve_template_fields(self):
        self.assertEqual(set(plugin.TRANSLATIONS), set(plugin.LANGUAGES) - {"en"})
        required = set(re.findall(r"\{\{t:([^{}]+)\}\}", plugin.PAGE + plugin.CONFIG_PAGE))
        required.update(re.findall(r'\bt\("([^"]+)"', plugin.PAGE + plugin.CONFIG_PAGE))
        required.update(plugin.STATES.values())
        required.update(plugin.ERROR_TEMPLATES)
        required.add(plugin.MONITOR_BIND_ERROR)
        self.assertFalse(plugin.CORE_MESSAGES.keys() & plugin.EXTENDED_MESSAGES.keys())
        self.assertFalse(required.difference(plugin.CORE_MESSAGES, plugin.EXTENDED_MESSAGES))
        self.assertEqual(len(plugin.FULL_TRANSLATION_LOCALES), 12)
        for messages, locales in ((plugin.CORE_MESSAGES, plugin.TRANSLATION_LOCALES),
                                  (plugin.EXTENDED_MESSAGES, plugin.FULL_TRANSLATION_LOCALES)):
            self.assertTrue(all(len(values) == len(locales) for values in messages.values()))
        for language in plugin.LANGUAGES:
            with self.subTest(language=language):
                config = plugin.render_page(plugin.CONFIG_PAGE, language, placement="flashforge", languageOptions="")
                monitor = plugin.render_page(plugin.PAGE, language, printer="FlashForge")
                self.assertIn(f'<html lang="{language}">', config)
                self.assertIn(f'<html lang="{language}">', monitor)
                self.assertNotRegex(config + monitor, r"\{\{(?:t:|translations|placement|language)")
                if language != "en":
                    catalog = plugin.TRANSLATIONS[language]
                    expected = plugin.CORE_MESSAGES.keys()
                    if language in plugin.FULL_TRANSLATION_LOCALES:
                        expected = expected | plugin.EXTENDED_MESSAGES.keys()
                    self.assertEqual(catalog.keys(), expected)
                    self.assertNotEqual(catalog["All printers"], "All printers")
                    for key, value in catalog.items():
                        self.assertTrue(value.strip(), key)
                        fields = r"\{[a-z]+\}"
                        self.assertEqual(Counter(re.findall(fields, key)), Counter(re.findall(fields, value)), key)

    def test_reduced_locale_keeps_controls_and_falls_back_for_extended_text(self):
        language = "ca"
        help_text = "The new language appears when you reopen the monitor."
        config = plugin.render_page(plugin.CONFIG_PAGE, language, placement="flashforge", languageOptions="")
        self.assertIn(f'id="languageHelp" hidden>{help_text}</p>', config)
        monitor = plugin.render_page(plugin.PAGE, language, printer="FlashForge")
        raw = re.search(r'<script id="plugin-translations" type="application/json">(.*?)</script>', monitor)
        messages = json.loads(raw[1])["messages"]
        self.assertNotEqual(messages["Cancel print?"], "Cancel print?")
        self.assertIn("Press Home before moving the axes.", messages)
        template = "The printer HTTP API returned status {code}."
        self.assertNotIn(template, messages)
        message = template.format(code=503)
        self.assertEqual(plugin.translate_error(language, message), message)

    def test_rendered_axis_heading_uses_motion_terminology(self):
        for language, heading in [("ru", "Оси"), ("fr", "Axes"), ("uk", "Осі")]:
            with self.subTest(language=language):
                page = plugin.render_page(plugin.PAGE, language, printer="FlashForge")
                self.assertIn(f'data-group="motion" aria-label="{heading}"', page)

    def test_override_applies_to_http_pages_without_changing_host_locale(self):
        config = {"language": "zh_TW"}
        server = object.__new__(plugin.LoopbackServer)
        server.get_config = config.copy
        server.host_language = "ja"
        self.assertEqual(server.language_for(), "zh-TW")
        config["language"] = ""
        self.assertEqual(server.language_for(), "ja")

    def test_webview_reports_locales_that_the_legacy_api_omits(self):
        server = object.__new__(plugin.LoopbackServer)
        config = {"language": ""}
        server.get_config = config.copy
        self.assertEqual(server.language_for("BBL-Slicer/v2.5.1 (light) BBL-Language/eu"), "eu")
        self.assertEqual(server.language_for(), "eu")
        config["language"] = "zh-TW"
        self.assertEqual(server.language_for("BBL-Slicer/v2.5.1 (light) BBL-Language/zh"), "zh-TW")

    def test_failed_background_read_preserves_detected_host_language(self):
        service = plugin.FlashForgeService()
        service._server = SimpleNamespace(host_language="en")
        with patch.object(plugin, "orca_language", return_value="ru"):
            self.assertEqual(service.refresh_host_language(), "ru")
        with patch.object(plugin, "orca_language", return_value=""):
            self.assertEqual(service.refresh_host_language(), "ru")
        self.assertEqual(service._server.host_language, "ru")

    def test_localized_errors_preserve_dynamic_values(self):
        message = "The printer HTTP API returned status 503."
        self.assertEqual(plugin.translate_error("en", message), message)
        german = plugin.translate_error("de", message)
        self.assertIn("503", german)
        self.assertNotEqual(german, message)
        message = "Filament 2 requires PLA-CF."
        russian = plugin.translate_error("ru", message)
        self.assertIn("2", russian)
        self.assertIn("PLA-CF", russian)
        self.assertNotEqual(russian, message)
        self.assertEqual(plugin.translate_error("de", "firmware diagnostic E0123"), "firmware diagnostic E0123")

    def test_notification_uses_override_and_keeps_the_filename(self):
        service = plugin.FlashForgeService()
        server = object.__new__(plugin.LoopbackServer)
        server.host_language = "de"
        server.get_config = lambda: {"language": "ru"}
        service._server = server
        notifications = []
        ui = SimpleNamespace(NotificationLevel=SimpleNamespace(RegularNotificationLevel="regular"),
                             push_notification=lambda level, text: notifications.append((level, text)))
        monitor = SimpleNamespace(state_lock=threading.Lock(), name="Adventurer",
                                  latest={"state": "COMPLETED", "file": "Bench & parts.3mf"})
        with patch.object(plugin.orca, "host", SimpleNamespace(ui=ui)):
            service.notify(monitor)
        self.assertEqual(notifications, [("regular", "Adventurer: Печать завершена — Bench & parts.3mf")])
