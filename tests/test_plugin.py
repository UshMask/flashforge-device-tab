"""Self-checks for FlashForge Device Tab.

Run with:  python -m unittest discover -s tests -v
The fixture printer speaks the same TCP and HTTP dialects as an Adventurer 5M,
so the plugin is exercised end to end without a real machine.
"""
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# The plugin imports orca; the tests only need the shapes it touches.
orca = SimpleNamespace(
    base=object,
    plugin=lambda cls: cls,
    script=SimpleNamespace(ScriptPluginCapabilityBase=object),
    host=SimpleNamespace(),
    LifecycleEvent=SimpleNamespace(UploadStarted="upload-started", UploadFinished="upload-finished"),
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
            return b"SN: SN12345\r\nok\r\n"
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


def request(port, path, method="GET", headers=None):
    connection = HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(method, path, headers=headers or {})
        response = connection.getresponse()
        return response.status, response.read(), dict(response.getheaders())
    finally:
        connection.close()


class ProtocolTests(unittest.TestCase):
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
        self.server = plugin.LoopbackServer((plugin.WEB_HOST, plugin.WEB_PORT), plugin.MonitorHandler)
        self.server.configure(lambda: {"flashforge_host": "127.0.0.1", "check_code": "code"},
                              Path(self.folder.name))
        self.server.restore_page = None
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
        self.service.get_config = lambda: json.dumps({"flashforge_host": "127.0.0.1", "check_code": "code"})
        self.service._page_store = Path(self.folder.name) / "device-pages.json"
        self.service._switch_lock = threading.Lock()
        self.service._stopping = threading.Event()
        self.service._server = SimpleNamespace(settings_for=lambda: ("127.0.0.1", "code"))
        self.service.restore_page = self.service.restore_page

    def tearDown(self):
        plugin.PORT, plugin.API_PORT, plugin.WEB_PORT = self.saved
        self.folder.cleanup()

    def test_switch_to_monitor_and_back(self):
        message = self.service.switch_view()
        self.assertIn("FlashForge Device Tab", message)
        monitor = self.preset.values["print_host_webui"]
        self.assertEqual(monitor, plugin.monitor_url("127.0.0.1"))
        self.assertEqual(json.loads(self.service._page_store.read_text()), {"Adventurer 5M": ""})

        message = self.service.switch_view(restore_view="127.0.0.1")
        self.assertIn("printer web interface", message)
        self.assertEqual(self.preset.values["print_host_webui"], "")

    def test_switch_rejects_foreign_profile(self):
        self.preset.values["host_type"] = "prusalink"
        with self.assertRaises(ValueError):
            self.service.switch_view()

    def test_switch_rejects_foreign_restore(self):
        self.service.switch_view()
        with self.assertRaises(ValueError):
            self.service.switch_view(restore_view="10.0.0.9")


if __name__ == "__main__":
    unittest.main(verbosity=2)
