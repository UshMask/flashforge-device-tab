"""Self-checks for FlashForge Device Tab.

Run with:  python -m unittest discover -s tests -v
The fixture printer speaks the same TCP and HTTP dialects as an Adventurer 5M,
so the plugin is exercised end to end without a real machine.
"""
import base64
import json
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# The plugin imports orca; the tests only need the shapes it touches.
orca = SimpleNamespace(
    base=object,
    plugin=lambda cls: cls,
    script=SimpleNamespace(ScriptPluginCapabilityBase=object),
    pages=SimpleNamespace(PagesPluginCapabilityBase=object),
    PluginType=SimpleNamespace(Pages="pages"),
    host=SimpleNamespace(),
    LifecycleEvent=SimpleNamespace(UploadStarted="upload-started", UploadFinished="upload-finished",
                                   PresetSelected="preset-selected", ProjectOpened="project-opened",
                                   NewProject="new-project"),
    ExecutionResult=SimpleNamespace(success=lambda message: ("ok", message),
                                    failure=lambda kind, message: ("error", message)),
    PluginResult=SimpleNamespace(RecoverableError="recoverable"),
    request_permissions=lambda **kwargs: None,
    register_capability=lambda capability: None,
)
sys.modules.setdefault("orca", orca)

import flashforge_device_tab as plugin  # noqa: E402

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010804000000b51c0c02"
    "0000000b4944415478da63fcff1f0003030200efa3335b0000000049454e44ae426082"
)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def file_list(*paths):
    body = bytearray(b"D\xaa\xaaD" + len(paths).to_bytes(4, "big"))
    for path in paths:
        raw = path.encode("utf-8")
        body += b"::\xa3\xa3" + len(raw).to_bytes(4, "big") + raw
    return bytes(body)


def image_frame(image=PNG):
    return b"**\xa2\xa2" + len(image).to_bytes(4, "big") + image


class FakePrinter:
    """A TCP console plus the HTTP /detail API of an Adventurer 5M."""

    def __init__(self, tcp_port, api_port):
        self.files = ["/user/Fixture.3mf", "/user/Second.3mf"]
        self.state = "PRINTING"
        self.layer = (12, 200)
        self.detail = {
            "estimatedTime": 3600,
            "printDuration": 600,
            "estimatedRightWeight": 42.5,
            "coolingFanSpeed": 80,
            "printSpeedAdjust": 100,
            "printFileName": "/user/Fixture.3mf",
        }
        self.stop = threading.Event()
        self.down = threading.Event()
        self.tcp = socket.socket()
        self.tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.tcp.bind(("127.0.0.1", tcp_port))
        self.tcp.listen(8)
        self.http = ThreadingHTTPServer(("127.0.0.1", api_port), self.api_handler())
        threading.Thread(target=self.serve, daemon=True).start()
        threading.Thread(target=self.http.serve_forever, daemon=True).start()

    def api_handler(printer):
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                if self.path != "/detail":
                    self.send_error(404)
                    return
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                if body.get("checkCode") != "code":
                    payload = {"code": 1}
                else:
                    payload = {"code": 0, "detail": printer.detail}
                raw = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):
                pass

        return Handler

    def serve(self):
        while not self.stop.is_set():
            try:
                connection, _ = self.tcp.accept()
            except OSError:
                return
            threading.Thread(target=self.handle, args=(connection,), daemon=True).start()

    def handle(self, connection):
        with connection:
            if self.down.is_set():
                return
            stream = connection.makefile("rb")
            while True:
                line = stream.readline()
                if not line:
                    return
                text = line.decode("utf-8", "replace").strip()
                if not text.startswith("~"):
                    continue
                try:
                    connection.sendall(self.reply(text[1:]))
                except OSError:
                    return

    def reply(self, command):
        if command == "M105":
            return b"T0:205.3 /215.0 B:59.1 /60.0\r\nok\r\n"
        if command == "M27":
            return f"Layer: {self.layer[0]} / {self.layer[1]}\r\nok\r\n".encode()
        if command == "M119":
            return (f"MachineStatus: {self.state}\r\nCurrentFile: /user/Fixture.3mf\r\nok\r\n").encode()
        if command == "M115":
            return (b"Machine Type: Flashforge Adventurer 5M\r\n"
                    b"Machine Name: SN12345\r\n"
                    b"Firmware: v0.0.0-demo\r\n"
                    b"SN: SN12345\r\n"
                    b"X: 220 Y: 220 Z: 220\r\n"
                    b"Tool Count: 1\r\n"
                    b"Mac Address:00:11:22:33:44:55\r\nok\r\n")
        if command == "M661":
            return b"ok\r\n" + file_list(*self.files)
        if command.startswith("M662 "):
            return b"ok\r\n" + image_frame()
        return b"ok\r\n"

    def close(self):
        self.stop.set()
        self.tcp.close()
        self.http.shutdown()
        self.http.server_close()


def request(port, path, method="GET", headers=None, body=None):
    connection = HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, response.read(), dict(response.getheaders())
    finally:
        connection.close()


class ProtocolTests(unittest.TestCase):
    def test_ifs_slots_are_ordered_and_missing_slots_stay_unknown(self):
        detail = {"matlStationInfo": {"slotCnt": 4, "currentSlot": 2, "slotInfos": [
            {"slotId": 4, "hasFilament": False},
            {"slotId": 2, "materialName": "PLA", "hasFilament": True, "materialColor": "#28705F"},
            {"slotId": 1, "materialColor": "not a color"}, {"slotId": True}]}}
        slots = plugin.material_slots(detail)
        self.assertEqual([s["id"] for s in slots], [1, 2, 3, 4])
        self.assertEqual(slots[0]["color"], "")
        self.assertTrue(slots[1]["active"])
        self.assertEqual(slots[2], {"id": 3})
        self.assertFalse(slots[3]["present"])
        self.assertEqual(plugin.material_slots(dict(detail, hasMatlStation=0)), [])
        self.assertEqual(plugin.material_slots({}), [])

    def test_file_list_roundtrip(self):
        files = plugin.decode_file_list(file_list("/user/A.3mf", "/user/B.3mf"))
        self.assertEqual(len(files), 2)
        names = sorted(path.rsplit(b"/", 1)[-1] for path in files.values())
        self.assertEqual(names, [b"A.3mf", b"B.3mf"])

    def test_file_list_rejects_foreign_payload(self):
        with self.assertRaises(ValueError):
            plugin.decode_file_list(b"GET / HTTP/1.1")

    def test_file_list_waits_for_rest_of_entry(self):
        self.assertIsNone(plugin.decode_file_list(file_list("/user/A.3mf")[:-2]))

    def test_file_list_ignores_unsafe_paths(self):
        self.assertEqual(plugin.decode_file_list(file_list("user/A.3mf")), {})

    def test_payloads_idle_timeout(self):
        left, right = socket.socketpair()
        try:
            with self.assertRaises(TimeoutError):
                for _ in plugin.payloads(left, bytearray(b""), 1024, idle=0.1, total=5):
                    pass
        finally:
            left.close()
            right.close()

    def test_payloads_accepts_slow_but_steady_transfer(self):
        left, right = socket.socketpair()
        chunks = [b"a" * 4096] * 3
        try:
            def feed():
                for chunk in chunks:
                    right.sendall(chunk)
                    time.sleep(0.05)
                right.shutdown(socket.SHUT_WR)

            threading.Thread(target=feed, daemon=True).start()
            data = bytearray()
            seen = []
            for payload in plugin.payloads(left, data, 1 << 20, idle=0.2, total=5):
                seen.append(len(payload))
            self.assertEqual(seen[-1], 3 * 4096)
        finally:
            left.close()
            right.close()

    def test_local_address(self):
        self.assertEqual(plugin.local_address(" 192.168.1.37 "), "192.168.1.37")
        for bad in ("8.8.8.8", "printer.local", "", "192.168.1.37:8899"):
            with self.assertRaises(ValueError):
                plugin.local_address(bad)

    def test_monitor_identity(self):
        url = plugin.monitor_url("192.168.1.37")
        self.assertEqual(plugin.monitor_identity(url), "192.168.1.37")
        self.assertIsNone(plugin.monitor_identity("http://127.0.0.1:1/192.168.1.37/"))
        self.assertIsNone(plugin.monitor_identity("https://127.0.0.1:18771/192.168.1.37/"))
        self.assertEqual(plugin.monitor_identity(f"http://127.0.0.1:{plugin.WEB_PORT}/"), "")

    def test_printer_endpoint_accepts_host_and_url(self):
        self.assertEqual(plugin.printer_endpoint("192.168.1.37"), "192.168.1.37")
        self.assertEqual(plugin.printer_endpoint("http://192.168.1.37:80/"), "192.168.1.37")


class HistoryTests(unittest.TestCase):
    def status(self, state, name="", elapsed=None, layer=None):
        return {"state": state, "status": state, "file": name, "elapsed": elapsed,
                "layer": layer, "active": state in plugin.ACTIVE_JOB,
                "complete": state == "COMPLETED", "percent": None}

    def test_transitions(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "history.json"
            history = plugin.PrintHistory(path)
            history.observe(self.status("COMPLETED", "Old"), 1000)
            self.assertEqual(history.jobs, [])
            history.observe(self.status("HEATING", "Cube"), 1005)
            history.observe(self.status("PRINTING", "Cube", 24, 2), 1030)
            history = plugin.PrintHistory(path)
            history.observe(self.status("PAUSED", "Cube", 26, 2), 1035)
            history.observe(self.status("PRINTING", "Cube", 27, 2), 1045)
            history.observe(self.status("COMPLETED", "Cube", 52, 10), 1070)
            history.observe(self.status("COMPLETED", "Cube", 52, 10), 1075)
            self.assertEqual(len(history.jobs), 1)
            self.assertEqual(history.jobs[0]["result"], "complete")
            self.assertEqual(history.jobs[0]["elapsed"], 52)

    def test_gap_ends_job(self):
        with tempfile.TemporaryDirectory() as folder:
            history = plugin.PrintHistory(Path(folder) / "history.json")
            history.observe(self.status("PRINTING", "A", 100, 5), 1000)
            history.observe(self.status("PRINTING", "A", 400, 6), 1200)
            self.assertEqual([job["result"] for job in history.jobs], ["active", "unknown"])

    def test_corrupt_history_is_kept(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "history.json"
            path.write_text("broken", encoding="utf-8")
            history = plugin.PrintHistory(path)
            history.observe(self.status("PRINTING", "D"), 1300)
            self.assertTrue(history.error)
            self.assertEqual(path.read_text(encoding="utf-8"), "broken")

    def test_same_print_after_sleep_is_counted_once(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "history.json"
            history = plugin.PrintHistory(path)
            history.observe(self.status("PRINTING", "Cube", 100, 5), 1000)
            history = plugin.PrintHistory(path)
            history.observe(self.status("PRINTING", "Cube", 300, 10), 1200)
            history.observe(self.status("COMPLETED", "Cube", 400, 15), 1300)
            stats = plugin.history_stats(history.jobs, 1300)
            self.assertEqual((stats["jobs"], stats["complete"], stats["seconds"]), (1, 1, 400))


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.saved = (plugin.PORT, plugin.API_PORT, plugin.WEB_PORT, plugin.POLL_INTERVAL)
        plugin.PORT = free_port()
        plugin.API_PORT = free_port()
        plugin.WEB_PORT = free_port()
        plugin.POLL_INTERVAL = 0.1
        cls.printer = FakePrinter(plugin.PORT, plugin.API_PORT)

    @classmethod
    def tearDownClass(cls):
        plugin.PORT, plugin.API_PORT, plugin.WEB_PORT, plugin.POLL_INTERVAL = cls.saved
        cls.printer.close()

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.config = {"printers": [{"host": "127.0.0.1", "code": "code", "name": "Fixture"}],
                       "flashforge_host": "127.0.0.1", "check_code": "code"}
        self.applied = []
        self.server = plugin.LoopbackServer((plugin.WEB_HOST, plugin.WEB_PORT), plugin.MonitorHandler)
        self.server.configure(lambda: dict(self.config), Path(self.folder.name))

        def apply_settings(printers):
            if not isinstance(printers, list) or not printers:
                raise ValueError("no printers")
            clean = []
            for entry in printers:
                host = plugin.local_address(entry["host"])
                code = entry.get("code") or ""
                if len(code) > 32 or any(not 33 <= ord(c) <= 126 for c in code):
                    raise ValueError("bad code")
                clean.append({"host": host, "code": code,
                              "name": entry.get("name") or host})
            self.applied.append(clean)
            return clean[0]["host"]

        self.server.apply_settings = apply_settings
        self.acted = []

        def act(host, action):
            if action not in ("pause", "resume", "cancel"):
                raise ValueError("Unknown printer command.")
            self.acted.append((host, action))
            return "PAUSED"

        self.server.act = act
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05},
                                       daemon=True)
        self.thread.start()
        self.wait_for_status()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.folder.cleanup()

    def url(self, route, endpoint="127.0.0.1"):
        return f"/{endpoint}/{route}"

    def wait_for_status(self, expected=200):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                status, body, _ = request(plugin.WEB_PORT, self.url("status"))
            except OSError:
                time.sleep(0.05)
                continue
            if status == expected:
                return json.loads(body) if expected == 200 else body
            time.sleep(0.05)
        self.fail(f"status never became {expected}")

    def test_status_is_numeric(self):
        status, body, _ = request(plugin.WEB_PORT, self.url("status"))
        data = json.loads(body)
        self.assertEqual(status, 200)
        self.assertAlmostEqual(data["nozzle"], 205.3, places=1)
        self.assertAlmostEqual(data["bed"], 59.1, places=1)
        self.assertEqual(data["layer"], 12)
        self.assertEqual(data["layerTotal"], 200)
        self.assertEqual(data["file"], "Fixture.3mf")
        self.assertEqual(data["remaining"], 3600)
        self.assertEqual(data["percent"], 6)
        self.assertTrue(data["jobControls"])

    def test_documented_adventurer_3_tcp_replies(self):
        original = self.printer.reply
        responses = {
            "M105": b"CMD M105 Received.\r\nok T0:205/215 B:59/60\r\n",
            "M27": b"CMD M27 Received.\r\nSD printing byte 45/100\r\nok\r\n",
            "M119": b"MachineStatus: BUILDING_FROM_SD\r\nok\r\n",
            "M115": b"echo: Machine Type: FlashForge Adventurer III\r\nSerial Number: DEMO3\r\nok\r\n",
        }
        with patch.object(self.printer, "reply", side_effect=lambda cmd: responses[cmd] if cmd in responses else original(cmd)):
            status = plugin.read_status("127.0.0.1", "")
        self.assertEqual((status["percent"], status["layer"], status["nozzle"]), (45, None, 205))
        self.assertEqual(status["info"]["model"], "FlashForge Adventurer III")
        self.assertFalse(status["jobControls"])

    def test_adventurer_keeps_tcp_without_http_credentials(self):
        adapter = plugin.FlashForge("127.0.0.1", "", "unused-configured-serial")
        data = adapter.status(False)
        self.assertEqual(data["info"]["serial"], "SN12345")
        self.assertEqual(data["percent"], 6)
        self.assertIsNone(data["remaining"])
        file_paths = [path for _, path in adapter.files().values()]
        self.assertIn(b"/user/Fixture.3mf", file_paths)
        self.assertEqual(adapter.preview(b"/user/Fixture.3mf"), PNG)

    def test_page_served(self):
        status, body, headers = request(plugin.WEB_PORT, self.url(""))
        self.assertEqual(status, 200)
        self.assertIn(b"FlashForge", body)
        self.assertEqual(headers.get("X-Frame-Options"), "DENY")

    def test_root_redirects_to_configured_endpoint(self):
        status, _, headers = request(plugin.WEB_PORT, "/")
        self.assertEqual(status, 308)
        self.assertTrue(headers["Location"].endswith("/127.0.0.1/"))

    def test_host_guard(self):
        status, _, _ = request(plugin.WEB_PORT, self.url("status"), headers={"Host": "evil.example"})
        self.assertEqual(status, 403)

    def test_endpoint_mismatch(self):
        status, body, _ = request(plugin.WEB_PORT, self.url("status", "10.0.0.9"))
        self.assertEqual(status, 409)

    def test_files_and_preview(self):
        status, body, _ = request(plugin.WEB_PORT, self.url("files"))
        files = json.loads(body)["files"]
        self.assertEqual(status, 200)
        self.assertEqual(len(files), 2)
        status, image, headers = request(plugin.WEB_PORT, self.url("preview/" + files[0]["id"]))
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "image/png")
        self.assertEqual(image[:8], plugin.PNG_HEADER)

    def test_telemetry_has_chart_and_history(self):
        status, body, _ = request(plugin.WEB_PORT, self.url("telemetry"))
        data = json.loads(body)
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(data["temperatures"]), 1)
        self.assertEqual(data["temperatures"][0]["nozzle"], 205.3)

    def test_upload_pauses_monitoring(self):
        self.server.begin_upload()
        status, body, _ = request(plugin.WEB_PORT, self.url("status"))
        self.assertEqual(status, 503)
        self.assertIn("sending a print job", json.loads(body)["error"])
        self.server.uploading.clear()
        self.wait_for_status()

    def test_stale_upload_self_heals(self):
        self.server.begin_upload()
        self.server.uploaded_at -= plugin.UPLOAD_TIMEOUT + 1
        status, _, _ = request(plugin.WEB_PORT, self.url("status"))
        self.assertEqual(status, 200)

    def test_settings_get(self):
        status, body, _ = request(plugin.WEB_PORT, self.url("settings"))
        data = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(data["printers"][0]["host"], "127.0.0.1")
        self.assertEqual(data["printers"][0]["code"], "code")
        self.assertEqual(data["endpoint"], "127.0.0.1")
        self.assertEqual(data["placement"], "device")

    def test_separate_tab_uses_configured_printers_and_obeys_placement(self):
        def reject_preset(host):
            self.fail("The FlashForge tab must work without a matching OrcaSlicer profile")

        self.server.check_view = reject_preset
        path = "/flashforge/status"
        status, _, _ = request(plugin.WEB_PORT, path)
        self.assertEqual(status, 409)
        self.server.placement = "flashforge"
        status, body, _ = request(plugin.WEB_PORT, path + "?printer=127.0.0.1")
        self.assertEqual(status, 200)
        self.assertIn("observedAt", json.loads(body))
        status, _, _ = request(plugin.WEB_PORT, path + "?printer=127.0.0.2")
        self.assertEqual(status, 409)

    def test_old_native_request_cannot_change_the_profile(self):
        self.server.native_page = lambda host: "http://127.0.0.1:8080/"
        status, body, _ = request(plugin.WEB_PORT, self.url("native"))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["url"], "http://127.0.0.1:8080/")
        headers = {"Origin": f"http://{plugin.WEB_HOST}:{plugin.WEB_PORT}",
                   "X-FlashForge-Tab-Action": "restore"}
        status, _, _ = request(plugin.WEB_PORT, self.url("native"), method="POST", headers=headers)
        self.assertEqual(status, 403)

    def test_fleet_selection_keeps_profile_validation(self):
        self.server.check_view = lambda host: host if host == "profile" else self.fail("Wrong profile")
        code, body, _ = request(plugin.WEB_PORT, "/profile/status?printer=127.0.0.1")
        self.assertEqual(code, 200)
        self.assertIn("observedAt", json.loads(body))
        code, _, _ = request(plugin.WEB_PORT, "/profile/status?printer=127.0.0.2")
        self.assertEqual(code, 409)

    def test_connection_report_checks_saved_printer(self):
        headers = {"Origin": f"http://{plugin.WEB_HOST}:{plugin.WEB_PORT}",
                   "X-FlashForge-Tab-Action": "diagnostics"}
        code, body, _ = request(plugin.WEB_PORT, self.url("diagnostics"), method="POST", headers=headers)
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body), {"transport": "TCP + HTTP", "authenticated": True,
                                           "cameraReported": False, "telemetry": True})
        code, _, _ = request(plugin.WEB_PORT, "/127.0.0.2/diagnostics", method="POST", headers=headers)
        self.assertEqual(code, 409)

    def test_notifications_validate_and_persist(self):
        headers = {"Origin": f"http://{plugin.WEB_HOST}:{plugin.WEB_PORT}",
                   "X-FlashForge-Tab-Action": "notifications"}
        prefs = dict(plugin.NOTIFICATIONS, offline=True, finished=False)
        url = self.url("notifications")
        code, _, _ = request(plugin.WEB_PORT, url, method="POST", headers=headers, body=json.dumps(prefs))
        self.assertEqual(code, 200)
        code, _, _ = request(plugin.WEB_PORT, url, method="POST", headers=headers, body='{"offline":0}')
        self.assertEqual(code, 400)
        self.assertEqual(json.loads((Path(self.folder.name) / "notifications.json").read_text()), prefs)


    def test_settings_post_requires_action_header(self):
        status, _, _ = request(plugin.WEB_PORT, self.url("settings"), method="POST",
                               headers={"Origin": f"http://{plugin.WEB_HOST}:{plugin.WEB_PORT}",
                                        "Content-Type": "application/json"})
        self.assertEqual(status, 403)
        self.assertEqual(self.applied, [])

    def test_settings_post_rejects_public_address(self):
        status, body, _ = request(plugin.WEB_PORT, self.url("settings"), method="POST",
                                  headers={"Origin": f"http://{plugin.WEB_HOST}:{plugin.WEB_PORT}",
                                           "Content-Type": "application/json",
                                           "X-FlashForge-Tab-Action": "settings"},
                                  body=json.dumps({"printers": [{"host": "8.8.8.8", "code": "x"}]}))
        self.assertEqual(status, 400)
        self.assertEqual(self.applied, [])

    def test_settings_post_rejects_bad_json(self):
        status, _, _ = request(plugin.WEB_PORT, self.url("settings"), method="POST",
                               headers={"Origin": f"http://{plugin.WEB_HOST}:{plugin.WEB_PORT}",
                                        "Content-Type": "application/json",
                                        "X-FlashForge-Tab-Action": "settings"},
                               body="{not json")
        self.assertEqual(status, 400)

    def test_settings_post_saves(self):
        status, body, _ = request(plugin.WEB_PORT, self.url("settings"), method="POST",
                                  headers={"Origin": f"http://{plugin.WEB_HOST}:{plugin.WEB_PORT}",
                                           "Content-Type": "application/json",
                                           "X-FlashForge-Tab-Action": "settings"},
                                  body=json.dumps({"printers": [{"host": "127.0.0.1",
                                                                 "code": "newcode",
                                                                 "name": "Fixture"}]}))
        data = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(data["endpoint"], "127.0.0.1")
        self.assertFalse(data["moved"])
        self.assertEqual(self.applied,
                         [[{"host": "127.0.0.1", "code": "newcode", "name": "Fixture"}]])

    def test_action_requires_action_header(self):
        status, _, _ = request(plugin.WEB_PORT, self.url("action"), method="POST",
                               headers={"Origin": f"http://{plugin.WEB_HOST}:{plugin.WEB_PORT}",
                                        "Content-Type": "application/json"},
                               body=json.dumps({"action": "pause"}))
        self.assertEqual(status, 403)
        self.assertEqual(self.acted, [])

    def test_action_rejects_unknown(self):
        status, body, _ = request(plugin.WEB_PORT, self.url("action"), method="POST",
                                  headers={"Origin": f"http://{plugin.WEB_HOST}:{plugin.WEB_PORT}",
                                           "Content-Type": "application/json",
                                           "X-FlashForge-Tab-Action": "action"},
                                  body=json.dumps({"action": "boom"}))
        self.assertEqual(status, 400)
        self.assertEqual(self.acted, [])

    def test_action_pauses(self):
        status, body, _ = request(plugin.WEB_PORT, self.url("action"), method="POST",
                                  headers={"Origin": f"http://{plugin.WEB_HOST}:{plugin.WEB_PORT}",
                                           "Content-Type": "application/json",
                                           "X-FlashForge-Tab-Action": "action"},
                                  body=json.dumps({"action": "pause"}))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["state"], "PAUSED")
        self.assertEqual(self.acted, [("127.0.0.1", "pause")])

    def test_telemetry_has_stats(self):
        status, body, _ = request(plugin.WEB_PORT, self.url("telemetry"))
        data = json.loads(body)
        self.assertEqual(status, 200)
        self.assertIn("stats", data)
        self.assertIn("jobs", data["stats"])

    def test_snapshot_without_camera(self):
        status, body, _ = request(plugin.WEB_PORT, self.url("snapshot"))
        self.assertEqual(status, 404)

    def test_native_post_requires_action_header(self):
        status, _, _ = request(plugin.WEB_PORT, self.url("native"), method="POST",
                               headers={"Origin": f"http://{plugin.WEB_HOST}:{plugin.WEB_PORT}"})
        self.assertEqual(status, 403)

    def test_server_survives_printer_failure(self):
        self.printer.down.set()
        self.wait_for_status(503)
        status, _, _ = request(plugin.WEB_PORT, self.url("telemetry"))
        self.assertEqual(status, 200)
        self.printer.down.clear()
        self.wait_for_status(200)


class ManualTransportTests(unittest.TestCase):
    def setUp(self):
        ports = patch.multiple(plugin, PORT=free_port(), API_PORT=free_port())
        ports.start()
        self.addCleanup(ports.stop)
        self.printer = FakePrinter(plugin.PORT, plugin.API_PORT)
        self.addCleanup(self.printer.close)
        self.printer.state = "READY"
        self.printer.detail.update(status="ready", pid="0023")
        self.model = "FlashForge Adventurer 5M"
        self.commands = []
        self.grant = True
        self.control_state = "READY"
        original = self.printer.reply

        def reply(cmd):
            self.commands.append(cmd)
            if cmd == "M601 S1":
                return b"Control Success V2.1.\r\nok\r\n" if self.grant else b"Control Failed.\r\nok\r\n"
            if cmd == "M119" and self.commands[-2:] == ["M601 S1", "M119"]:
                return f"MachineStatus: {self.control_state}\r\nok\r\n".encode()
            if cmd == "M114":
                return b"X:100 Y:20 Z:5 A:0\r\nok\r\n"
            if cmd == "M115":
                return original(cmd).replace(b"Flashforge Adventurer 5M", self.model.encode())
            return original(cmd)
        self.printer.reply = reply
        storage = tempfile.TemporaryDirectory()
        self.addCleanup(storage.cleanup)
        self.server = plugin.LoopbackServer(("127.0.0.1", 0), plugin.MonitorHandler)
        self.addCleanup(self.server.server_close)
        self.entry = {"host": "127.0.0.1", "code": "code", "serial": ""}
        self.server.configure(lambda: {"printers": [self.entry]}, Path(storage.name))
        self.adapter = plugin.FlashForge("127.0.0.1", "code")
        self.server.monitors["127.0.0.1"] = plugin.PrinterMonitor(self.adapter, Path(storage.name) / "history.json")
        self.service = plugin.FlashForgeService()
        self.service._server = self.server

    def act(self, key):
        return self.service.act("127.0.0.1", "manual/" + key)

    def test_5m_jog_and_reject_out_of_bounds_pause_or_denied_control(self):
        self.act("x-10")
        self.assertEqual(self.commands[-6:], ["M601 S1", "M119", "M114", "G90", "G1 X90.000 F300", "M602"])
        for key, state, grant in (("z-10", "READY", True), ("home", "PAUSED", True), ("home", "READY", False)):
            self.commands.clear()
            self.control_state, self.grant = state, grant
            with self.assertRaises(ValueError):
                self.act(key)
            self.assertFalse(any(c.startswith(("G1 ", "G28")) for c in self.commands))

    def test_legacy_tcp_without_checkcode(self):
        self.entry["code"] = self.adapter.check_code = ""
        for self.model in ("FlashForge Adventurer III", "FlashForge Adventurer 4 Pro"):
            with self.subTest(model=self.model):
                self.act("home")
                self.assertEqual(self.commands[-2:], ["G28", "M602"])
                self.act("e-10")
                self.assertEqual(self.commands[-3:], ["M105", "G1 E-10 F120", "M602"])
                with self.assertRaises(ValueError):
                    self.act("shaper")

    def test_model_capability_matrix(self):
        for pid in ("0023", "0024", "0028", "0029", 35, 36, 40, 41):
            d = dict(pid=pid, status="ready", autoShutdown="close", moveCtrl=1, extrudeCtrl=1)
            controls = plugin.manual_controls(d, "READY")
            self.assertCountEqual(controls["groups"], ["calibration", "timer"])
            self.assertIn("shaper", controls["allowed"])
            self.assertNotIn("home", controls["allowed"])
            self.assertFalse(plugin.manual_controls(d, "PAUSED")["allowed"])
        self.assertIsNone(plugin.manual_controls({"pid":"9999", "model":"AD5X"}, "READY"))
        self.assertIsNone(plugin.manual_controls({"pid":"0028"}, "READY", "Adventurer 5M"))


class CreatorHTTPTests(unittest.TestCase):
    def setUp(self):
        self.detail = {
            "status": "printing", "model": "Creator 5 Pro", "pid": "29",
            "firmwareVersion": "1.9.2", "measure": "256X256X256", "nozzleCnt": 4,
            "nozzleTemps": [210, 28, 29, 30], "nozzleTargetTemps": [220, 0, 0, 0],
            "rightTemp": 99, "rightTargetTemp": 99, "platTemp": 57, "platTargetTemp": 60,
            "chamberTemp": 39, "chamberTargetTemp": 45, "lightStatus": "open",
            "doorStatus": "close", "cameraStreamUrl": "http://127.0.0.1:8080/?action=stream",
            "printFileName": "Тест 5.3mf", "printProgress": 0.42, "printLayer": 5,
            "targetPrintLayer": 20, "estimatedTime": 180.2, "printDuration": 60,
            "coolingFanSpeed": 80, "printSpeedAdjust": 100,
        }
        self.calls = []
        self.lan_only = True
        self.bad_detail = False
        self.control_readbacks = None
        self.pending_states = []
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fixture.calls.append((self.path, body))
                if not fixture.lan_only:
                    result = {"code": -2, "message": "Lan mode error"}
                elif body["serialNumber"] != "SN-C5P":
                    result = {"code": 1, "message": "SN is different"}
                elif body["checkCode"] != "abc01234":
                    result = {"code": 1, "message": "Access code is different"}
                elif self.path == "/detail":
                    if fixture.pending_states:
                        fixture.detail["status"] = fixture.pending_states.pop(0)
                    result = {"code": 0, "detail": fixture.detail}
                elif self.path == "/gcodeList":
                    result = {"code": 0, "gcodeList": ["Тест 5.3mf", "", 5, "../unexpected.gcode"]}
                elif self.path == "/gcodeThumb":
                    image = base64.b64encode(PNG).decode("ascii") if body["fileName"] == "Тест 5.3mf" else "%%"
                    result = {"code": 0, "imageData": image}
                elif self.path == "/control":
                    args = body["payload"].get("args", {})
                    if fixture.detail.get("pid") == "0026" or body["payload"]["cmd"] in ("calibration_cmd", "delayClose_cmd"):
                        cmd = body["payload"]["cmd"]
                        if cmd == "errorCodeCtrl_cmd" and args["errorCode"] == fixture.detail["errorCode"]:
                            fixture.detail["errorCode"] = ""
                        elif cmd == "delayClose_cmd":
                            fixture.detail.update(autoShutdown=args["automaticShutdown"], autoShutdownTime=args["shutdownAfterTime"])
                    elif body["payload"]["cmd"] == "streamCtrl":
                        fixture.camera_control(args["action"])
                    elif fixture.control_readbacks is None:
                        fixture.detail["status"] = {"pause": "pause", "continue": "printing",
                                                    "cancel": "canceling"}[args["action"]]
                    else:
                        fixture.pending_states = list(fixture.control_readbacks)
                    result = {"code": 0}
                else:
                    self.send_error(404)
                    return
                raw = b"<invalid>" if fixture.bad_detail and self.path == "/detail" else json.dumps(result).encode()
                self.send_response(200)
                self.send_header("Content-Type", "appliation/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):
                pass

        http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(http.server_close)
        threading.Thread(target=http.serve_forever, daemon=True).start()
        self.addCleanup(http.shutdown)
        tcp = socket.socket()
        self.addCleanup(tcp.close)
        tcp.bind(("127.0.0.1", 0))  # Reserve a port without accepting connections.
        ports = patch.multiple(plugin, API_PORT=http.server_port, PORT=tcp.getsockname()[1])
        ports.start()
        self.addCleanup(ports.stop)
        self.adapter = plugin.FlashForge("127.0.0.1", "abc01234", "SN-C5P")

    def act_with_readbacks(self, action, states):
        with tempfile.TemporaryDirectory() as folder:
            host = "127.0.0.1"
            entry = {"host": host, "code": "abc01234", "serial": "SN-C5P"}
            with plugin.LoopbackServer((host, 0), plugin.MonitorHandler) as server:
                server.configure(lambda: {"printers": [entry]}, Path(folder))
                monitor = plugin.PrinterMonitor(self.adapter, Path(folder) / "history.json")
                server.monitors[host] = monitor
                service = plugin.FlashForgeService()
                service._server = server
                monitor.sample(int(time.time()))
                self.control_readbacks = states
                return service.act(host, action)

    def test_ad5x_manual_controls_over_http(self):
        self.detail.update(pid="0026", model="AD5X", status="ready", rightTemp=195,
                           moveCtrl=1, extrudeCtrl=1, autoShutdown="close", errorCode="")
        for key, cmd, args in (
            ("home", "homingCtrl_cmd", None),
            ("x-10", "moveCtrl_cmd", {"axis": "x", "delta": -10}),
            ("y+1", "moveCtrl_cmd", {"axis": "y", "delta": 1}),
            ("z-1", "moveCtrl_cmd", {"axis": "z", "delta": -1}),
            ("z+10", "moveCtrl_cmd", {"axis": "z", "delta": 10}),
            ("e+10", "extrudeCtrl_cmd", {"axis": "e", "delta": 10}),
            ("e-10", "extrudeCtrl_cmd", {"axis": "e", "delta": -10}),
            ("level", "calibration_cmd", {"levelingDetection": "open", "vibrationCompensation": "close"}),
            ("shaper", "calibration_cmd", {"levelingDetection": "close", "vibrationCompensation": "open"}),
            ("timer60", "delayClose_cmd", {"automaticShutdown": "open", "shutdownAfterTime": 60}),
            ("clear", "errorCodeCtrl_cmd", {"action": "clearErrorCode", "errorCode": "E0048"}),
        ):
            with self.subTest(key=key):
                if key == "clear":
                    self.detail["errorCode"] = "E0048"
                self.calls.clear()
                result = self.act_with_readbacks("manual/" + key, [])
                commands = [body["payload"] for path, body in self.calls if path == "/control"]
                self.assertEqual(commands, [{"cmd": cmd, **({"args": args} if args else {})}])
                if key in ("timer60", "clear"):
                    self.assertNotIn("not confirmed", result)

    def test_ad5x_blocks_stale_idle_and_invalid_commands(self):
        self.detail.update(pid="0026", model="AD5X", status="ready", rightTemp=25, moveCtrl=1, extrudeCtrl=1)
        for state, key in (("pause","home"), ("printing","z-10"), ("unknown","level"),
                           ("ready","e+10"), ("ready","z+100"), ("ready","clear")):
            with self.subTest(state=state, key=key):
                self.pending_states = ["ready", state]
                with self.assertRaises(ValueError):
                    self.act_with_readbacks("manual/" + key, [])
        self.detail.update(pid="29", model="Creator 5 Pro", status="ready")
        with self.assertRaises(ValueError):
            self.act_with_readbacks("manual/home", [])
        self.assertFalse(any(path == "/control" for path, _ in self.calls))

    def test_creator_calibration_and_timer(self):
        for pid in ("0028", "0029"):
            self.detail.update(pid=pid, status="ready", autoShutdown="close")
            self.assertIn("accepted", self.act_with_readbacks("manual/level", []))
            self.assertIn("confirmed", self.act_with_readbacks("manual/timer30", []))
            with self.assertRaises(ValueError):
                self.act_with_readbacks("manual/e+10", [])

    def test_delayed_pause_checks_status_without_repeating_command(self):
        self.assertEqual(self.act_with_readbacks("pause", ["printing", "pause"]), "PAUSED")
        self.assertEqual(sum(path == "/control" for path, _ in self.calls), 1)

    def test_ignored_cancel_reports_observed_state_and_sends_once(self):
        with self.assertRaisesRegex(RuntimeError, "did not confirm cancel.*PRINTING"):
            self.act_with_readbacks("cancel", [])
        self.assertEqual(sum(path == "/control" for path, _ in self.calls), 1)

    def test_fallback_caches_http_and_decodes_creator_status(self):
        with patch.object(plugin, "read_status", wraps=plugin.read_status) as tcp:
            data = self.adapter.status(False)
            self.detail["status"] = "pause"
            paused = self.adapter.status(False)
            tcp.assert_called_once()
        self.assertEqual(data["info"]["model"], "Creator 5 Pro")
        self.assertEqual(data["info"]["volume"], [256, 256, 256])
        self.assertEqual(data["nozzle"], 210)
        self.assertEqual(data["tools"][3], {"actual": 30, "target": 0})
        self.assertEqual(data["chamber"], {"actual": 39, "target": 45})
        self.assertEqual(data["sensors"]["door"], "close")
        self.assertEqual(data["percent"], 42)
        self.assertEqual((data["layer"], data["layerTotal"]), (5, 20))
        self.assertEqual((data["remaining"], data["elapsed"]), (181, 60))
        self.assertEqual(data["cameraUrl"], self.detail["cameraStreamUrl"])
        self.assertEqual(paused["state"], "PAUSED")
        self.assertTrue(paused["active"])

    def test_missing_invalid_credentials_and_lan_mode(self):
        self.adapter.configure("abc01234", "")
        with self.assertRaisesRegex(plugin.PrinterAPIError, "set the serial number and CheckCode"):
            self.adapter.status(False)
        self.assertEqual(self.calls, [])
        self.adapter.configure("abc01234", "wrong")
        with self.assertRaisesRegex(plugin.PrinterAPIError, "Check the printer serial number"):
            self.adapter.status(False)
        self.adapter.configure("wrong", "SN-C5P")
        with self.assertRaisesRegex(plugin.PrinterAPIError, "Check the CheckCode"):
            self.adapter.status(False)
        self.lan_only = False
        with self.assertRaisesRegex(plugin.PrinterAPIError, "Enable LAN-only mode"):
            self.adapter.status(False)
        self.lan_only = True
        self.adapter.configure("abc01234", "SN-C5P")
        self.assertEqual(self.adapter.status(False)["state"], "PRINTING")

    def test_invalid_detail_recovers_and_preview_rejects_invalid_data(self):
        self.bad_detail = True
        with self.assertRaisesRegex(plugin.PrinterAPIError, "invalid HTTP data"):
            self.adapter.status(False)
        self.bad_detail = False
        self.detail.update({"status": "completed", "printProgress": 2, "nozzleTemps": None,
                            "nozzleTargetTemps": None, "cameraStreamUrl": "http://192.168.1.8:8080/"})
        data = self.adapter.status(False)
        self.assertEqual((data["complete"], data["percent"]), (True, 100))
        self.assertIsNone(data["nozzle"])
        self.assertEqual(data["cameraUrl"], "")
        self.assertIsNone(self.adapter.preview("absent.3mf"))

    def test_http_timeout_recovers_on_next_sample(self):
        self.adapter.status(False)
        with patch.object(plugin, "read_http_status", side_effect=TimeoutError):
            with self.assertRaises(TimeoutError):
                self.adapter.status(False)
        self.assertEqual(self.adapter.status(False)["state"], "PRINTING")

    def test_status_files_preview_and_controls_through_device_server(self):
        web_port = free_port()
        with patch.multiple(plugin, WEB_PORT=web_port, POLL_INTERVAL=0.1), tempfile.TemporaryDirectory() as folder:
            server = plugin.LoopbackServer(("127.0.0.1", web_port), plugin.MonitorHandler)
            preset = {"host": "127.0.0.1", "code": "abc01234", "serial": "SN-C5P"}
            server.configure(lambda: {"printers": [preset]}, Path(folder))
            service = plugin.FlashForgeService()
            service._server = server
            server.act = service.act
            worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
            worker.start()
            try:
                path = "/127.0.0.1/"
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    status, body, _ = request(web_port, path + "status")
                    if status == 200:
                        break
                    time.sleep(0.05)
                self.assertEqual(status, 200, body)
                self.assertEqual(json.loads(body)["file"], "Тест 5.3mf")
                status, body, _ = request(web_port, path + "telemetry")
                self.assertEqual(status, 200)
                newest = json.loads(body)["temperatures"][-1]
                self.assertEqual((newest["t1"], newest["t4"], newest["t4Target"]), (210, 30, 0))
                status, body, _ = request(web_port, path + "files")
                self.assertEqual(status, 200, body)
                file, = json.loads(body)["files"]
                self.assertEqual(file["name"], "Тест 5.3mf")
                status, body, headers = request(web_port, path + "preview/" + file["id"])
                self.assertEqual((status, body, headers["Content-Type"]), (200, PNG, "image/png"))
                for action, state in (("pause", "PAUSED"), ("resume", "PRINTING"), ("cancel", "CANCELING")):
                    status, body, _ = request(
                        web_port, path + "action", method="POST", body=json.dumps({"action": action}),
                        headers={"Origin": f"http://127.0.0.1:{web_port}", "Content-Type": "application/json",
                                 "X-FlashForge-Tab-Action": "action"})
                    self.assertEqual(status, 200, body)
                    self.assertEqual(json.loads(body)["state"], state)
                controls = [body["payload"] for path, body in self.calls if path == "/control"]
                self.assertEqual([payload["args"]["action"] for payload in controls], ["pause", "continue", "cancel"])
                self.assertTrue(all(payload["cmd"] == "jobCtl_cmd" for payload in controls))
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=2)


class SwitchViewTests(unittest.TestCase):
    def setUp(self):
        self.saved = (plugin.PORT, plugin.API_PORT, plugin.WEB_PORT)
        plugin.PORT = free_port()
        plugin.API_PORT = free_port()
        plugin.WEB_PORT = free_port()
        self.folder = tempfile.TemporaryDirectory()
        self.preset = SimpleNamespace(
            name="Adventurer 5M",
            values={"host_type": "flashforge", "print_host": "127.0.0.1", "print_host_webui": ""},
            config_value=lambda key: self.preset.values.get(key, ""),
        )
        self.transaction = SimpleNamespace(target_name=self.preset.name,
                                           values={},
                                           set=lambda key, value: self.transaction.values.__setitem__(key, value),
                                           commit=lambda: self.preset.values.update(self.transaction.values))
        presets = SimpleNamespace(edited_preset=lambda: self.preset, selected_preset=lambda: self.preset)
        orca.host = SimpleNamespace(
            plugin=SimpleNamespace(storage=lambda: self.folder.name),
            preset=SimpleNamespace(printer=SimpleNamespace(begin_update=lambda: self.transaction)),
            preset_bundle=lambda: SimpleNamespace(printers=presets, current_printer_preset=lambda: self.preset),
        )
        self.service = plugin.FlashForgeService()
        self.config = {"flashforge_host": "127.0.0.1", "check_code": "code"}
        self.service.get_config = lambda: json.dumps(self.config)
        self.service._page_store = Path(self.folder.name) / "device-pages.json"
        self.service._switch_lock = threading.Lock()
        self.service._stopping = threading.Event()
        self.service._view_refresh = threading.Event()
        self.service._server = SimpleNamespace(
            settings_for=lambda: ("127.0.0.1", "code"),
            printers=lambda: [{"host": "127.0.0.1", "code": "code", "name": "Adventurer 5M"}],
            monitors={})

    def tearDown(self):
        plugin.PORT, plugin.API_PORT, plugin.WEB_PORT = self.saved
        self.folder.cleanup()

    def test_native_browser_url_keeps_monitor_open(self):
        message = self.service.switch_view()
        self.assertIn("FlashForge Device Tab", message)
        monitor = self.preset.values["print_host_webui"]
        self.assertEqual(monitor, plugin.monitor_url("127.0.0.1"))
        self.assertEqual(json.loads(self.service._page_store.read_text()), {"Adventurer 5M": ""})

        self.transaction.values.clear()
        self.assertEqual(self.service.native_page("127.0.0.1"), "http://127.0.0.1")
        self.assertEqual(self.preset.values["print_host_webui"], monitor)
        self.assertEqual(self.transaction.values, {})
        self.assertIn("Printer tab", self.service.switch_view())

    def restart_service(self):
        # Orca discards the edited profile on exit: only plugin storage survives.
        previous = self.service
        self.service = plugin.FlashForgeService()
        for key in ("_page_store", "_server", "get_config"):
            setattr(self.service, key, getattr(previous, key))
        self.service._switch_lock = threading.Lock()
        self.service._stopping = threading.Event()
        self.service._view_refresh = threading.Event()
        self.transaction.values.clear()

    def test_panel_restored_after_unsaved_profile_is_discarded_and_service_restarts(self):
        self.preset.values["print_host_webui"] = "http://127.0.0.1:8080/"
        self.service.switch_view()
        self.preset.values["print_host_webui"] = "http://127.0.0.1:8080/"
        self.restart_service()
        self.service.restore_selected_view()
        self.assertEqual(self.preset.values["print_host_webui"], plugin.monitor_url("127.0.0.1"))
        self.assertEqual(self.service.native_page("127.0.0.1"), "http://127.0.0.1:8080/")

    def test_legacy_native_choice_is_recovered_on_startup(self):
        (Path(self.folder.name) / "device-views.json").write_text(json.dumps({
            self.preset.name: {"host": "127.0.0.1", "monitor": False}}), encoding="utf-8")
        self.preset.values["print_host_webui"] = "http://127.0.0.1"
        self.restart_service()
        self.service.restore_selected_view()
        self.assertEqual(self.preset.values["print_host_webui"], plugin.monitor_url("127.0.0.1"))

    def test_configured_profile_defaults_to_panel_without_prior_choice(self):
        self.service.restore_selected_view()
        self.assertEqual(self.preset.values["print_host_webui"], plugin.monitor_url("127.0.0.1"))
        with patch.object(self.transaction, "commit") as commit:
            self.service.restore_selected_view()
        commit.assert_not_called()

    def test_restore_ignores_unconfigured_or_foreign_printers(self):
        for changes in ({"host_type": "prusalink"},
                        {"host_type": "flashforge", "print_host": "192.168.1.9"}):
            self.preset.values.update(changes)
            self.service.restore_selected_view()
            self.assertEqual(self.preset.values["print_host_webui"], "")
        self.assertEqual(self.transaction.values, {})

    def test_restore_supports_second_configured_printer(self):
        self.service._server.printers = lambda: [{"host": "192.168.1.37", "code": "first"},
                                                  {"host": "127.0.0.1", "code": "code"}]
        self.service.restore_selected_view()
        self.assertEqual(self.preset.values["print_host_webui"], plugin.monitor_url("127.0.0.1"))

    def test_placement_restores_native_page_and_survives_restart(self):
        native = "http://127.0.0.1:8080/"
        self.preset.values["print_host_webui"] = native
        self.service.restore_selected_view()
        self.assertEqual(self.preset.values["print_host_webui"], plugin.monitor_url("127.0.0.1"))
        self.config["placement"] = "flashforge"
        self.service.restore_selected_view()
        self.assertEqual(self.preset.values["print_host_webui"], plugin.monitor_url("127.0.0.1"))
        self.restart_service()
        self.service.active_placement = "flashforge"
        self.service.restore_selected_view()
        self.assertEqual(self.preset.values["print_host_webui"], native)
        self.assertEqual(self.service.config()["placement"], "flashforge")
        self.config["placement"] = "device"
        self.service.active_placement = "device"
        self.service.restore_selected_view()
        self.assertEqual(self.preset.values["print_host_webui"], plugin.monitor_url("127.0.0.1"))

    def test_profile_change_between_read_and_transaction_does_not_write(self):
        def begin_update():
            self.preset.values["print_host"] = "192.168.1.9"
            return self.transaction
        orca.host.preset.printer.begin_update = begin_update
        with self.assertRaisesRegex(ValueError, "profile changed"):
            self.service.restore_selected_view()
        self.assertEqual(self.transaction.values, {})
        self.assertFalse(self.service._page_store.exists())

    def test_failed_commit_leaves_native_page_intact(self):
        with patch.object(self.transaction, "commit", side_effect=RuntimeError("stale")):
            with self.assertRaises(RuntimeError):
                self.service.switch_view()
        self.assertEqual(self.preset.values["print_host_webui"], "")

    def test_root_panel_url_does_not_replace_saved_native_url(self):
        self.service.switch_view()
        self.preset.values["print_host_webui"] = plugin.monitor_url()
        self.service.restore_selected_view()
        self.assertEqual(json.loads(self.service._page_store.read_text()), {"Adventurer 5M": ""})
        self.service.active_placement = "flashforge"
        self.service.restore_selected_view()
        self.assertEqual(self.preset.values["print_host_webui"], "")

    def test_plugin_settings_register_flashforge_page_only_after_reload(self):
        self.service._placement_lock = threading.Lock()
        self.service._next_placement = "device"
        path = Path(self.folder.name) / "monitor-placement.txt"
        with patch.object(plugin, "PLACEMENT_FILE", path), patch.object(orca, "register_capability") as register, \
                patch.object(plugin.FlashForgeService, "active_placement", "device"):
            package = plugin.FlashForgeDeviceTab()
            package.register_capabilities()
            register.assert_called_once_with(plugin.FlashForgeService)

            self.config["placement"] = "flashforge"
            self.service.sync_next_placement()
            self.assertEqual(plugin.FlashForgeService.active_placement, "device")
            register.reset_mock()
            package.register_capabilities()
            self.assertEqual([call.args[0] for call in register.call_args_list],
                             [plugin.FlashForgeService, plugin.FlashForgePage])

            self.config["placement"] = "device"
            self.service.sync_next_placement()
            register.reset_mock()
            package.register_capabilities()
            register.assert_called_once_with(plugin.FlashForgeService)

    def test_startup_worker_and_preset_events_restore_page_without_manual_execution(self):
        self.service._server = None
        self.service.save_config = lambda value: None
        self.service.on_load()
        try:
            for event in (None, orca.LifecycleEvent.PresetSelected, orca.LifecycleEvent.ProjectOpened):
                if event is not None:
                    self.preset.values["print_host_webui"] = ""
                    self.service.on_lifecycle_event(event, SimpleNamespace())
                deadline = time.monotonic() + 3
                while (self.preset.values["print_host_webui"] != plugin.monitor_url("127.0.0.1")
                       and time.monotonic() < deadline):
                    time.sleep(0.01)
                self.assertEqual(self.preset.values["print_host_webui"], plugin.monitor_url("127.0.0.1"))
        finally:
            self.service.on_unload()
            self.service._view_worker.join(timeout=2)
        self.assertFalse(self.service._view_worker.is_alive())

    def test_startup_worker_retries_until_profile_becomes_available(self):
        self.service._server = None
        self.service.save_config = lambda value: None
        original = orca.host.preset_bundle
        calls = []

        def bundle():
            calls.append(True)
            if len(calls) == 1:
                raise RuntimeError("Preset bundle is not available")
            return original()

        with patch.object(orca.host, "preset_bundle", side_effect=bundle):
            self.service.on_load()
            try:
                deadline = time.monotonic() + 3
                while (self.preset.values["print_host_webui"] != plugin.monitor_url("127.0.0.1")
                       and time.monotonic() < deadline):
                    time.sleep(0.01)
                self.assertEqual(self.preset.values["print_host_webui"], plugin.monitor_url("127.0.0.1"))
                self.assertGreater(len(calls), 1)
            finally:
                self.service.on_unload()
                self.service._view_worker.join(timeout=2)
        self.assertFalse(self.service._view_worker.is_alive())

    def test_apply_settings_updates_profile(self):
        self.saved_config = []
        self.service.save_config = lambda value: self.saved_config.append(json.loads(value))
        self.preset.values["print_host_webui"] = plugin.monitor_url("127.0.0.1")
        endpoint = self.service.apply_settings([{"host": "192.168.1.99", "code": "newcode",
                                                  "name": "Second"}])
        self.assertEqual(endpoint, "192.168.1.99")
        self.assertEqual(self.preset.values["print_host"], "192.168.1.99")
        self.assertEqual(self.preset.values["print_host_webui"], plugin.monitor_url("192.168.1.99"))
        saved = self.saved_config[0]
        self.assertEqual(saved["flashforge_host"], "192.168.1.99")
        self.assertEqual(saved["check_code"], "newcode")
        self.assertEqual(saved["printers"], [{"host": "192.168.1.99", "code": "newcode",
                                              "name": "Second"}])

    def test_apply_settings_rejects_public_address(self):
        with self.assertRaises(ValueError):
            self.service.apply_settings([{"host": "1.1.1.1", "code": "code"}])

    def test_apply_settings_preserves_creator_serial_and_older_entries(self):
        saved = []
        self.service.save_config = lambda value: saved.append(json.loads(value))
        self.service.apply_settings([{"host": "127.0.0.1", "serial": " SN-C5P ", "code": "abc01234"},
                                     {"host": "192.168.1.37"}])
        self.assertEqual(saved[0]["serial_number"], "SN-C5P")
        self.assertEqual(saved[0]["printers"][0]["serial"], "SN-C5P")
        self.assertNotIn("serial", saved[0]["printers"][1])
        with self.assertRaisesRegex(ValueError, "Enter a serial number"):
            self.service.apply_settings([{"host": "127.0.0.1", "serial": "bad | serial"}])

    def test_apply_settings_rejects_bad_check_code(self):
        with self.assertRaises(ValueError):
            self.service.apply_settings([{"host": "127.0.0.1", "code": "код"}])

    def test_switch_rejects_foreign_profile(self):
        self.preset.values["host_type"] = "prusalink"
        with self.assertRaises(ValueError):
            self.service.switch_view()

    def test_native_browser_url_rejects_stale_profile(self):
        self.service.switch_view()
        with self.assertRaises(ValueError):
            self.service.native_page("10.0.0.9")


class ControlTests(unittest.TestCase):
    """The /control request shape documented for the 5M series."""

    def setUp(self):
        self.saved = (plugin.API_PORT,)
        plugin.API_PORT = free_port()
        self.requests = []
        self.reply = {"code": 0, "message": "Success"}
        self.status = 200
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                outer.requests.append(json.loads(self.rfile.read(length) or b"{}"))
                body = json.dumps(outer.reply).encode()
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer((plugin.WEB_HOST, plugin.API_PORT), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        plugin.API_PORT = self.saved[0]

    def test_pause_uses_job_control(self):
        plugin.control_job("127.0.0.1", "SN123", "code", "pause")
        self.assertEqual(len(self.requests), 1)
        body = self.requests[0]
        self.assertEqual(body["serialNumber"], "SN123")
        self.assertEqual(body["checkCode"], "code")
        self.assertEqual(body["payload"]["cmd"], "jobCtl_cmd")
        self.assertEqual(body["payload"]["args"]["action"], "pause")
        self.assertEqual(body["payload"]["args"]["jobID"], "")

    def test_resume_maps_to_continue(self):
        plugin.control_job("127.0.0.1", "SN123", "code", "continue")
        self.assertEqual(self.requests[0]["payload"]["args"]["action"], "continue")

    def test_printer_error_is_reported(self):
        self.reply = {"code": -3, "message": "Printer is busy"}
        with self.assertRaises(ValueError) as raised:
            plugin.control_job("127.0.0.1", "SN123", "code", "cancel")
        self.assertIn("code -3", str(raised.exception))

    def test_http_error_is_reported(self):
        self.status = 500
        with self.assertRaises(ValueError):
            plugin.control_job("127.0.0.1", "SN123", "code", "pause")


class NotifyTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        levels = SimpleNamespace(RegularNotificationLevel="regular",
                                 WarningNotificationLevel="warning")
        self.saved_host = getattr(orca, "host", None)
        orca.host = SimpleNamespace(ui=SimpleNamespace(
            NotificationLevel=levels,
            push_notification=lambda level, text: self.sent.append((level, text))))
        self.service = plugin.FlashForgeService()

    def tearDown(self):
        orca.host = self.saved_host

    def monitor(self, state, file="bench.3mf", name="Adventurer"):
        return SimpleNamespace(state_lock=threading.Lock(),
                               latest={"state": state, "file": file}, name=name)

    def test_finished_print_notifies_once(self):
        monitor = self.monitor("COMPLETED")
        self.service.notify(monitor)
        self.service.notify(monitor)
        self.assertEqual(self.sent, [("regular", "Adventurer: Print finished — bench.3mf")])

    def test_error_uses_warning_level(self):
        self.service.notify(self.monitor("ERROR", file=""))
        self.assertEqual(self.sent, [("warning", "Adventurer: Printer error")])

    def test_running_state_is_silent(self):
        self.service.notify(self.monitor("PRINTING"))
        self.assertEqual(self.sent, [])

    def test_offline_alert_after_one_minute_and_no_duplicate_finish(self):
        monitor = self.monitor("COMPLETED")
        monitor.notifications = dict(plugin.NOTIFICATIONS, offline=True)
        self.service.notify(monitor)
        last = monitor.latest
        monitor.latest = None
        with patch.object(plugin.time, "monotonic", side_effect=[10, 69, 70, 80]):
            for _ in range(4):
                self.service.notify(monitor)
        monitor.latest = last
        self.service.notify(monitor)
        self.assertEqual(len(self.sent), 2)
        self.assertIn("offline", self.sent[1][1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
