"""Self-checks for FlashForge Device Tab.

Run with:  python -m unittest discover -s tests -v
The fixture printer speaks the same TCP and HTTP dialects as an Adventurer 5M,
so the plugin is exercised end to end without a real machine.
"""
import base64
import io
import json
import socket
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlencode

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


def sliced_source():
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as bundle:
        bundle.writestr("Metadata/plate_1.gcode", "; fixture plate")
        bundle.writestr("Metadata/slice_info.config", '<config><plate>'
                        '<filament id="1" type="PLA" color="#112233"/>'
                        '<filament id="3" type="PETG" color="#445566"/>'
                        '</plate></config>')
    return stream.getvalue()


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
    def test_upload_multipart_has_storage_only_flag_and_exact_length(self):
        content = b"; Storage-only upload fixture\n"
        with patch.object(plugin.http.client, "HTTPConnection") as factory:
            connection = factory.return_value
            response = connection.getresponse.return_value
            response.status = 200
            response.read.return_value = b'{"code":0}'
            transmitted = []
            connection.request.side_effect = lambda method, path, chunks, headers: transmitted.append(
                (method, path, b"".join(chunks), headers))
            plugin.upload_flashforge_file("127.0.0.1", "serial", "code", "Test.gcode",
                                         io.BytesIO(content), len(content), lambda: None)
        method, path, body, headers = transmitted[0]
        self.assertEqual((method, path), ("POST", "/uploadGcode"))
        self.assertEqual(headers["printNow"], "false")
        self.assertEqual(headers["fileSize"], str(len(content)))
        self.assertEqual(int(headers["Content-Length"]), len(body))
        self.assertIn(b'name="gcodeFile"; filename="Test.gcode"', body)
        self.assertIn(content, body)

    def test_discovery_decodes_flashforge_big_endian_and_deduplicates(self):
        packet = bytearray(276)
        packet[0x86:0x8a] = bytes.fromhex("2b710029")
        packet[0x92:0x98] = b"SN-C5P"
        with patch.object(plugin.socket, "socket") as socket_factory:
            sock = socket_factory.return_value.__enter__.return_value
            sock.recvfrom.side_effect = [(packet, ("192.168.1.12", 48899)), (packet, ("192.168.1.12", 48899)),
                                         (packet, ("8.8.8.8", 48899)), (b"short", ("192.168.1.13", 48899)),
                                         socket.timeout()]
            self.assertEqual(plugin.discover_printers(), [{"host": "192.168.1.12", "name": "Creator 5 Pro", "serial": "SN-C5P"}])
            sock.sendto.assert_called_once_with(b"\0", ("255.255.255.255", 48899))

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

    def test_job_activity_persists_and_records_each_connection_change_once(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "history.json"
            history = plugin.PrintHistory(path)
            history.observe(self.status("PRINTING", "Cube", 20, 2), 1000)
            self.assertTrue(history.connection_lost(1005))
            self.assertFalse(history.connection_lost(1006))
            history = plugin.PrintHistory(path)
            history.observe(self.status("PAUSED", "Cube", 26, 2), 1010)
            history.observe(self.status("PRINTING", "Cube", 27, 2), 1020)
            history.observe(self.status("COMPLETED", "Cube", 40, 5), 1035)
            job = plugin.PrintHistory(path).jobs[0]
            self.assertEqual(job["events"], [
                [980, "Printing started"], [1005, "Connection lost"], [1010, "Connection restored"],
                [1010, "Print paused"], [1020, "Print resumed"], [1035, "Print completed"],
            ])

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


class FleetHistoryTests(unittest.TestCase):
    def test_combines_live_jobs_and_archives_without_counting_stale_prints(self):
        with tempfile.TemporaryDirectory() as folder:
            storage = Path(folder)
            config = {"printers": [{"host": "192.168.1.4", "name": "Studio", "code": "fixture"}]}
            server = object.__new__(plugin.LoopbackServer)
            server.configure(lambda: config, storage)
            server.remember_history_names([{"host": "192.168.1.7", "name": "Workshop"}])
            server.configure(lambda: config, storage)
            completed = {"started": 200, "ended": 230, "elapsed": 30, "result": "complete", "file": "Part.3mf"}
            (storage / "flashforge-192.168.1.4-history.json").write_text(json.dumps([completed]), encoding="utf-8")
            (storage / "flashforge-192.168.1.7-history.json").write_text(json.dumps([
                {**completed, "result": "active", "seen": 230},
            ]), encoding="utf-8")
            (storage / "flashforge-192.168.1.8-history.json").write_text("damaged", encoding="utf-8")
            (storage / "flashforge-192.168.1.9-history.json").write_text(json.dumps([completed]), encoding="utf-8")
            server.monitors["192.168.1.4"] = SimpleNamespace(
                state_lock=threading.Lock(), history_error="",
                history_snapshot=[{**completed, "file": "Live.3mf"}],
            )
            data = server.history_payload()
            printers = {printer["host"]: printer for printer in data["printers"]}
            self.assertEqual(printers["192.168.1.4"]["jobs"][0]["file"], "Live.3mf")
            self.assertEqual(printers["192.168.1.7"]["name"], "Workshop")
            self.assertEqual(printers["192.168.1.7"]["jobs"][0]["result"], "unknown")
            self.assertEqual(plugin.PrintHistory(storage / "flashforge-192.168.1.7-history.json").jobs[0]["result"], "active")
            self.assertTrue(printers["192.168.1.8"]["error"])
            self.assertEqual(printers["192.168.1.9"]["name"], "192.168.1.9")
            self.assertEqual((data["stats"]["jobs"], data["stats"]["complete"], data["stats"]["seconds"]), (3, 2, 90))
            self.assertNotIn("code", printers["192.168.1.4"])


class BedConfirmationTests(unittest.TestCase):
    def test_clearance_persists_and_is_specific_to_the_completed_job(self):
        with tempfile.TemporaryDirectory() as folder:
            config = {"printers": [{"host": "192.168.1.4", "name": "Studio"}]}
            storage = Path(folder)
            host = config["printers"][0]["host"]
            server = object.__new__(plugin.LoopbackServer)
            completed = {"started": 100, "ended": 200, "result": "complete", "file": "Part.3mf"}
            monitor = SimpleNamespace(state_lock=threading.Lock(), latest={"state": "READY", "active": False},
                                      error="", camera_url="", history_snapshot=[completed])
            server.configure(lambda: config, storage)
            server.monitors[host] = monitor
            token = server.fleet_payload()[0]["bedClearToken"]
            self.assertTrue(server.confirm_bed_clear(host, token))
            self.assertTrue(server.confirm_bed_clear(host, token))
            server.configure(lambda: config, storage)
            server.monitors[host] = monitor
            self.assertNotIn("bedClearToken", server.fleet_payload()[0])
            monitor.history_snapshot = [{**completed, "started": 210, "ended": 300}, completed]
            self.assertNotEqual(server.fleet_payload()[0]["bedClearToken"], token)
            self.assertFalse(server.confirm_bed_clear(host, token))
            new_token = server.fleet_payload()[0]["bedClearToken"]
            monitor.latest = {"state": "PRINTING", "active": True}
            self.assertFalse(server.confirm_bed_clear(host, new_token))
            self.assertNotIn("bedClearToken", server.fleet_payload()[0])
            monitor.latest = {"state": "READY", "active": False}
            monitor.history_snapshot.insert(0, {**completed, "result": "cancelled"})
            self.assertNotIn("bedClearToken", server.fleet_payload()[0])


class LoopbackStartupTests(unittest.TestCase):
    def test_unload_after_cloud_storage_startup_failure(self):
        def unavailable_storage():
            raise RuntimeError("Cloud storage unavailable")

        service = plugin.FlashForgeService()
        host = SimpleNamespace(plugin=SimpleNamespace(storage=unavailable_storage))
        with patch.object(plugin.orca, "host", host):
            with self.assertRaisesRegex(RuntimeError, "Cloud storage unavailable"):
                service.on_load()
        service.on_unload()
        self.assertIsNone(service._server)

    def test_start_reports_unavailable_monitor_address(self):
        service = plugin.FlashForgeService()
        with socket.socket() as occupied:
            occupied.bind((plugin.WEB_HOST, 0))
            occupied.listen()
            port = occupied.getsockname()[1]
            host = SimpleNamespace(plugin=SimpleNamespace(storage=tempfile.gettempdir))
            with patch.object(plugin.orca, "host", host), patch.object(plugin, "WEB_PORT", port):
                message = rf"127\.0\.0\.1:{port}\) is in use or reserved\. Close all OrcaSlicer"
                with self.assertRaisesRegex(RuntimeError, message) as raised:
                    service.on_load()
        self.assertIsInstance(raised.exception.__cause__, OSError)
        self.assertIsNone(service._server)

    @unittest.skipUnless(hasattr(socket, "SO_EXCLUSIVEADDRUSE"), "Windows socket sharing")
    def test_old_listener_and_new_listener_cannot_share_the_address(self):
        with HTTPServer((plugin.WEB_HOST, 0), BaseHTTPRequestHandler) as old:
            address = old.server_address
            with self.assertRaises(OSError):
                plugin.LoopbackServer(address, plugin.MonitorHandler)
        with plugin.LoopbackServer(address, plugin.MonitorHandler):
            with self.assertRaises(OSError):
                HTTPServer(address, BaseHTTPRequestHandler)


class FleetPollingTests(unittest.TestCase):
    def test_stalled_poll_and_failure_do_not_block_the_other_printer(self):
        slow, fast = "127.0.0.1", "127.0.0.2"
        entered, unblock, fast_sampled = (threading.Event() for _ in range(3))

        def sample(monitor, now):
            if monitor.endpoint == slow:
                entered.set()
                unblock.wait(5)
                raise RuntimeError("failed slow poll")
            monitor.latest = {"state": "READY"}
            fast_sampled.set()

        with tempfile.TemporaryDirectory() as folder:
            with plugin.LoopbackServer(("127.0.0.1", 0), plugin.MonitorHandler) as server:
                config = {"printers": [{"host": slow}, {"host": fast}]}
                server.configure(lambda: config, Path(folder))
                with patch.object(plugin.PrinterMonitor, "sample", sample):
                    dispatcher = threading.Thread(target=server.poll_printer)
                    dispatcher.start()
                    try:
                        self.assertTrue(entered.wait(2))
                        self.assertTrue(fast_sampled.wait(2))
                        with server.printer_io(fast):
                            self.assertEqual(server.monitors[fast].latest["state"], "READY")
                        unblock.set()
                        server.monitors[slow].poll_worker.join(2)
                        self.assertEqual(server.monitors[slow].error, "Could not update printer data.")
                        self.assertEqual(server.monitors[fast].latest["state"], "READY")
                    finally:
                        unblock.set()
                        server.stopping.set()
                        dispatcher.join(3)


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

        def act(host, action, *, expected_job=None):
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

    def test_upload_uses_storage_only_and_verifies_the_new_file_and_ready_state(self):
        self.addCleanup(setattr, self.printer, "files", self.printer.files)
        self.addCleanup(setattr, self.printer, "state", self.printer.state)
        old_detail = self.printer.detail.copy()
        self.addCleanup(setattr, self.printer, "detail", old_detail)
        self.printer.state = "READY"
        self.printer.detail["status"] = "ready"
        data = b"; Storage-only upload fixture\n"
        headers = {"Origin": f"http://127.0.0.1:{plugin.WEB_PORT}", "X-FlashForge-Tab-Action": "upload-file"}
        calls = []

        def upload(host, serial, check_code, name, stream, length, available):
            available()
            calls.append((name, stream.read(), length))
            self.printer.files = [*self.printer.files, "/user/" + name]

        with patch.object(plugin, "upload_flashforge_file", side_effect=upload):
            status, body, _ = request(plugin.WEB_PORT, self.url("upload-file?name=Upload.gcode"), "POST", headers, data)
            self.assertEqual(status, 200, body)
            self.assertEqual(json.loads(body), {"file": "Upload.gcode", "state": "READY"})
            status, body, _ = request(plugin.WEB_PORT, self.url("upload-file?name=Upload.gcode"), "POST", headers, data)
            self.assertEqual(status, 400)
            self.assertIn(b"already exists", body)
            self.printer.state = "BUILDING_FROM_SD"
            self.printer.detail["status"] = "printing"
            status, body, _ = request(plugin.WEB_PORT, self.url("upload-file?name=Busy.gcode"), "POST", headers, data)
            self.assertEqual(status, 400)
            self.assertIn(b"must be idle", body)
        self.assertEqual(calls, [("Upload.gcode", data, len(data))])
        _, body, _ = request(plugin.WEB_PORT, self.url("files"))
        self.assertEqual(json.loads(body)["files"][0]["name"], "Upload.gcode")
        monitor = self.server.monitors["127.0.0.1"]
        restored = plugin.PrinterMonitor(monitor.adapter, monitor.history.path)
        self.assertEqual(next(iter(restored.update_file_catalog(restored.adapter.files()).values()))[0], "Upload.gcode")

    def test_file_refresh_promotes_new_entries_without_reordering_known_files(self):
        self.addCleanup(setattr, self.printer, "files", self.printer.files)

        def file_names():
            status, body, _ = request(plugin.WEB_PORT, self.url("files"))
            self.assertEqual(status, 200)
            return [entry["name"] for entry in json.loads(body)["files"]]

        self.assertEqual(file_names(), ["Fixture.3mf", "Second.3mf"])
        self.printer.files = ["/user/Second.3mf", "/user/Fixture.3mf", "/user/New.3mf"]
        self.assertEqual(file_names(), ["New.3mf", "Fixture.3mf", "Second.3mf"])
        self.assertEqual(file_names(), ["New.3mf", "Fixture.3mf", "Second.3mf"])
        self.printer.files = ["/user/Second.3mf", "/user/New.3mf", "/user/Later.3mf"]
        self.assertEqual(file_names(), ["Later.3mf", "New.3mf", "Second.3mf"])

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
        self.server.finish_upload()
        self.wait_for_status()

    def test_stale_upload_self_heals(self):
        with patch.object(plugin.time, "monotonic", return_value=time.monotonic() - plugin.UPLOAD_TIMEOUT - 1):
            self.server.begin_upload()
        status, _, _ = request(plugin.WEB_PORT, self.url("status"))
        self.assertEqual(status, 200)

    def test_late_finish_cannot_release_another_upload(self):
        with patch.object(plugin.time, "monotonic", return_value=time.monotonic() - plugin.UPLOAD_TIMEOUT - 1):
            self.server.begin_upload("earlier.3mf")
        self.server.begin_upload("current.3mf")
        self.server.finish_upload("earlier.3mf")
        self.assertTrue(self.server.upload_active())
        with self.assertRaises(plugin.UploadInProgress):
            with self.server.printer_io("127.0.0.2"):
                self.fail("printer I/O started during upload")
        self.server.finish_upload("current.3mf")
        self.assertFalse(self.server.upload_active())

    def test_upload_start_blocks_new_io_while_draining(self):
        starting = threading.Event()
        original = self.server.upload_gate.wait_for

        def wait_for_idle(*args, **kwargs):
            starting.set()
            return original(*args, **kwargs)

        with patch.object(self.server.upload_gate, "wait_for", side_effect=wait_for_idle):
            with self.server.printer_io("127.0.0.2"):
                worker = threading.Thread(target=self.server.begin_upload, args=("new.3mf",))
                worker.start()
                self.assertTrue(starting.wait(2))
                self.assertTrue(self.server.upload_active())
                with self.assertRaises(plugin.UploadInProgress):
                    with self.server.printer_io("127.0.0.3"):
                        self.fail("unrelated network operation started during upload")
            worker.join(2)
            self.assertFalse(worker.is_alive())
        self.server.finish_upload("new.3mf")

    def test_discover_and_test_unsaved_printer_are_loopback_actions(self):
        origin = f"http://127.0.0.1:{plugin.WEB_PORT}"
        device = {"host": "192.168.1.12", "serial": "SN-C5P", "name": "Creator 5 Pro"}
        with patch.object(plugin, "discover_printers", return_value=[device]) as discover:
            denied, _, _ = request(plugin.WEB_PORT, self.url("discover"), "POST",
                                   {"Origin": "https://example.com", "X-FlashForge-Tab-Action": "discover"})
            self.assertEqual(denied, 403)
            discover.assert_not_called()
            status, body, _ = request(plugin.WEB_PORT, self.url("discover"), "POST",
                                      {"Origin": origin, "X-FlashForge-Tab-Action": "discover"})
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["printers"], [device])
        with patch.object(plugin.FlashForge, "status", return_value={"info": {"model": device["name"]}, "transport": "HTTP"}):
            status, body, _ = request(plugin.WEB_PORT, self.url("test-printer"), "POST",
                                      {"Origin": origin, "X-FlashForge-Tab-Action": "test-printer"},
                                      json.dumps({**device, "code": "demo"}))
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body), {"model": device["name"], "transport": "HTTP"})
            self.assertFalse(self.applied)

    def test_settings_get(self):
        status, body, _ = request(plugin.WEB_PORT, self.url("settings"))
        data = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(data["printers"][0]["host"], "127.0.0.1")
        self.assertEqual(data["printers"][0]["code"], "code")
        self.assertEqual(data["endpoint"], "127.0.0.1")
        self.assertEqual(data["placement"], "device")
        self.assertEqual(data["savedPlacement"], "device")

    def test_history_endpoint_keeps_archived_jobs_without_a_configured_printer(self):
        archive = Path(self.folder.name) / "flashforge-192.168.1.7-history.json"
        archive.write_text(json.dumps([{"started": 100, "result": "complete", "file": "Saved.3mf"}]), encoding="utf-8")
        self.config.clear()
        status, body, _ = request(plugin.WEB_PORT, "/flashforge/history")
        self.assertEqual(status, 200)
        self.assertEqual(self.server.printers(), [])
        saved = next(printer for printer in json.loads(body)["printers"] if printer["host"] == "192.168.1.7")
        self.assertEqual(saved["jobs"][0]["file"], "Saved.3mf")

    def test_separate_tab_uses_configured_printers_and_obeys_placement(self):
        def reject_preset(host):
            self.fail("The FlashForge tab must work without a matching OrcaSlicer profile")

        self.server.check_view = reject_preset
        path = "/flashforge/status"
        self.server.placement = "device"
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

    def test_successful_connection_check_resumes_polling_after_permission_denial(self):
        monitor = self.server.monitors["127.0.0.1"]
        with self.server.printer_io("127.0.0.1"):
            with patch.object(monitor.adapter, "status", side_effect=PermissionError):
                monitor.sample(int(time.time()))
        error = self.wait_for_status(503)
        self.assertIn("Allow access to the printer", json.loads(error)["error"])
        headers = {"Origin": f"http://{plugin.WEB_HOST}:{plugin.WEB_PORT}",
                   "X-FlashForge-Tab-Action": "diagnostics"}
        status, _, _ = request(plugin.WEB_PORT, self.url("diagnostics"), method="POST", headers=headers)
        self.assertEqual(status, 200)
        self.assertIn("observedAt", self.wait_for_status())

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
        job = "1" * 32
        status, body, _ = request(plugin.WEB_PORT, self.url("action"), method="POST",
                                  headers={"Origin": f"http://{plugin.WEB_HOST}:{plugin.WEB_PORT}",
                                           "Content-Type": "application/json",
                                           "X-FlashForge-Tab-Action": "action"},
                                  body=json.dumps({"action": "pause", "job": job}))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["state"], "PAUSED")
        self.assertEqual(self.acted, [("127.0.0.1", "pause")])

    def test_job_action_rejects_request_without_an_observed_job(self):
        status, _, _ = request(plugin.WEB_PORT, self.url("action"), method="POST",
                               headers={"Origin": f"http://{plugin.WEB_HOST}:{plugin.WEB_PORT}",
                                        "Content-Type": "application/json", "X-FlashForge-Tab-Action": "action"},
                               body=json.dumps({"action": "cancel"}))
        self.assertEqual(status, 400)
        self.assertEqual(self.acted, [])

    def test_out_of_range_manual_action_reports_travel_limit(self):
        def act(host, action):
            return plugin.checked_manual_target(action.removeprefix("manual/"), 110, (220, 220, 220), "5m")

        with patch.object(self.server, "act", act):
            status, body, _ = request(plugin.WEB_PORT, self.url("action"), method="POST",
                                      headers={"Origin": f"http://{plugin.WEB_HOST}:{plugin.WEB_PORT}",
                                               "Content-Type": "application/json",
                                               "X-FlashForge-Tab-Action": "action"},
                                      body=json.dumps({"action": "manual/x+1"}))
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body), {"code": "travel_limit",
                                            "error": "X move exceeds the axis travel limits (-110 to 110 mm)."})

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
        self.volume = (220, 220, 220)
        self.commands = []
        self.grant = True
        self.control_state = "READY"
        self.move_mode = "READY"
        self.position = b"X:100 Y:20 Z:5 A:0\r\nok\r\n"
        self.jog_reply = None
        original = self.printer.reply

        def reply(cmd):
            self.commands.append(cmd)
            if cmd == "M601 S1":
                return b"Control Success V2.1.\r\nok\r\n" if self.grant else b"Control Failed.\r\nok\r\n"
            if cmd == "M119" and self.commands[-2:] == ["M601 S1", "M119"]:
                return f"MachineStatus: {self.control_state}\r\nMoveMode: {self.move_mode}\r\nok\r\n".encode()
            if cmd == "M114":
                return self.position
            if cmd.startswith("G1 X") and self.jog_reply is not None:
                return self.jog_reply
            if cmd == "M115":
                x, y, z = self.volume
                return original(cmd).replace(b"Flashforge Adventurer 5M", self.model.encode()).replace(
                    b"X: 220 Y: 220 Z: 220", f"X: {x} Y: {y} Z: {z}".encode()
                )
            if cmd == "M119":
                return original(cmd).replace(b"ok\r\n", f"MoveMode: {self.move_mode}\r\nok\r\n".encode())
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

    def test_stored_print_routes_and_busy_guard(self):
        monitor = self.server.monitors["127.0.0.1"]
        monitor.files = self.adapter.files()
        file_id = next(iter(monitor.files))
        name = monitor.files[file_id][0]
        for model in ("Flashforge Adventurer III", "Flashforge Adventurer 4 Pro",
                      "Flashforge Adventurer 5M", "AD5X"):
            with self.subTest(model=model):
                self.model = model
                monitor.print_request_until = 0
                current = self.adapter.status(True)
                current["info"]["model"] = model
                current["materials"] = [{"id": 1, "name": "PLA", "color": "#334455", "present": True}]
                mapped = model == "AD5X"
                action = "print/" + file_id + "/0/" + ("1000" if mapped else "")
                with patch.object(self.adapter, "status", return_value=current), patch.object(plugin, "printer_api", return_value={}) as http:
                    self.service.act("127.0.0.1", action)
                    if model.endswith(("III", "4 Pro")):
                        self.assertIn("M23 0:/user/" + name, self.commands)
                    else:
                        args = http.call_args.args
                        self.assertEqual(args[3], "/printGcode")
                        self.assertEqual(args[4]["fileName"], name)
                        if mapped:
                            self.assertEqual(args[4]["materialMappings"][0]["slotId"], 1)
                    with self.assertRaisesRegex(ValueError, "already sent"):
                        self.service.act("127.0.0.1", action)
                    current["state"] = "PRINTING"
                    with self.assertRaisesRegex(ValueError, "idle"):
                        self.service.act("127.0.0.1", action)

    def test_5m_warns_for_only_the_steps_that_would_cross_a_limit(self):
        self.position = b"X:107.037 Y:-108.037 Z:220.000 A:0\r\nok\r\n"
        controls = self.adapter.status(True)["manual"]
        self.assertEqual(set(controls["travelLimited"]), {"x+10", "y-10", "z+1", "z+10"})
        self.assertTrue(set(controls["travelLimited"]).issubset(controls["allowed"]))
        self.position = b"X:0 Y:0 A:0\r\nok\r\n"
        self.assertEqual(self.adapter.status(True)["manual"]["travelLimited"], [])

    def test_5m_can_report_completed_job_while_machine_remains_busy(self):
        self.printer.state = "BUSY"
        self.printer.detail["status"] = "completed"
        status = self.adapter.status(True)
        self.assertEqual(status["state"], "BUSY")
        self.assertTrue(status["complete"])
        self.assertFalse(status["active"])
        self.assertEqual(status["manual"]["allowed"], [])
        self.printer.state = "READY"
        self.printer.detail["status"] = "ready"
        status = self.adapter.status(True)
        self.assertEqual(status["state"], "READY")
        self.assertFalse(status["complete"])

    def test_5m_canceled_job_retains_history_and_restores_confirmed_tcp_controls(self):
        monitor = self.server.monitors["127.0.0.1"]
        now = int(time.time())
        self.printer.state = "BUILDING_FROM_SD"
        self.printer.detail["status"] = "printing"
        monitor.history.observe(self.adapter.status(True), now)

        self.printer.state = "READY"
        self.printer.detail["status"] = "cancelled"
        status = self.adapter.status(True)
        monitor.history.observe(status, now + 1)
        self.assertEqual(status["status"], "Canceled")
        self.assertFalse(status["active"])
        self.assertEqual(monitor.history.jobs[0]["result"], "cancelled")
        self.assertIn("heat0", status["manual"]["allowed"])
        self.assertIn("x+1", status["manual"]["allowed"])
        self.assertNotIn("level", status["manual"]["allowed"])
        self.assertNotIn("release", status["manual"]["allowed"])
        self.act("x+1")
        self.assertEqual(self.commands[-2:], ["G1 X101.000 F300", "M602"])

        self.printer.detail["status"] = "ready"
        status = self.adapter.status(True)
        self.assertEqual(status["status"], "Ready")
        self.assertIn("level", status["manual"]["allowed"])

    def test_5m_cancel_keeps_controls_locked_without_confirmed_idle_axes(self):
        for state, detail_state, move_mode, error in (
            ("CANCELING", "cancel", "READY", None),
            ("CANCELED", "canceled", "READY", None),
            ("READY", "canceled", "MOVING", None),
            ("READY", "canceled", "", None),
            ("READY", "canceled", "READY", "E1"),
        ):
            with self.subTest(state=state, detail=detail_state, motion=move_mode, error=error):
                self.printer.state = state
                self.printer.detail.update(status=detail_state, errorCode=error)
                self.move_mode = move_mode
                self.assertEqual(self.adapter.status(True)["manual"]["allowed"], [])
                self.commands.clear()
                with self.assertRaises(ValueError):
                    self.act("x+1")
                self.assertNotIn("M601 S1", self.commands)

        self.printer.state = "READY"
        self.printer.detail.update(status="canceled", errorCode=None)
        self.control_state = "CANCELING"  # State changed when acquiring manual control.
        self.commands.clear()
        with self.assertRaisesRegex(plugin.ManualAxesBusy, "must be idle"):
            self.act("x+1")
        self.assertEqual(self.commands[-3:], ["M601 S1", "M119", "M602"])

    def test_5m_jog_and_reject_out_of_bounds_pause_or_denied_control(self):
        self.act("x-10")
        self.assertEqual(self.commands[-6:], ["M601 S1", "M119", "M114", "G90", "G1 X90.000 F300", "M602"])
        for key, state, grant in (("z-10", "READY", True), ("home", "PAUSED", True), ("home", "READY", False)):
            self.commands.clear()
            self.control_state, self.grant = state, grant
            with self.assertRaises(ValueError):
                self.act(key)
            self.assertFalse(any(c.startswith(("G1 ", "G28")) for c in self.commands))
        with self.assertRaisesRegex(ValueError, "Printer denied manual control"):
            self.act("home")

    def test_5m_release_persists_until_home_completes(self):
        host = "127.0.0.1"
        monitor = self.server.monitors[host]
        status = self.adapter.status(True)
        self.commands.clear()
        self.assertEqual(self.act("release"), "Motor release sent.")
        self.assertEqual(self.commands[-4:], ["M601 S1", "M119", "M18", "M602"])

        monitor = plugin.PrinterMonitor(self.adapter, monitor.history.path)
        self.server.monitors[host] = monitor
        monitor.bind_manual_printer(status)
        controls = status["manual"]
        self.assertTrue(monitor.manual_controls_view(controls)["released"])
        self.assertIn("home", monitor.manual_controls_view(controls)["allowed"])
        for key in ("x+1", "z+1", "level", "release"):
            with self.subTest(key=key):
                self.commands.clear()
                with self.assertRaises(ValueError):
                    self.act(key)
                self.assertFalse(any(code.startswith(("G1 ", "G28", "M18")) for code in self.commands))

        for key in ("e+10", "e-10"):
            self.assertIn(key, monitor.manual_controls_view(controls, status)["allowed"])
            self.act(key)
        self.assertIn("Waiting", self.act("home"))
        self.assertEqual(self.commands[-2:], ["G28", "M602"])
        for key in ("e+10", "e-10"):
            with self.assertRaises(ValueError):
                self.act(key)
        self.assertTrue(monitor.manual_controls_view(controls)["releaseHoming"])
        with self.assertRaises(ValueError):
            self.act("home")
        monitor.update_manual_home(status)
        self.assertTrue(monitor.manual_release_marker.is_file())
        monitor.update_manual_home({**status, "state": "BUSY", "sensors": {"moveMode": "HOMING"}})
        self.assertTrue(monitor.manual_release_marker.is_file())
        monitor.update_manual_home(status)
        self.assertFalse(monitor.manual_release_marker.is_file())
        self.act("x+1")
        self.assertEqual(self.commands[-2:], ["G1 X101.000 F300", "M602"])

    def test_home_requirement_follows_serial_to_a_new_address(self):
        old = self.server.monitors["127.0.0.1"]
        self.act("release")
        new_host = "127.0.0.2"
        moved = plugin.PrinterMonitor(self.adapter, old.history.path.with_name("moved.json"))
        self.entry["host"] = new_host
        self.server.monitors = {new_host: moved}
        with patch.object(plugin, "tcp_manual_move") as motion:
            with self.assertRaises(plugin.ManualHomeRequired):
                self.service.act(new_host, "manual/x+1")
            motion.assert_not_called()
        self.assertEqual(moved.manual_release_marker, old.manual_release_marker)
        status = self.adapter.status(True)
        other = plugin.PrinterMonitor(self.adapter, old.history.path)
        other.bind_manual_printer({**status, "info": {**status["info"], "serial": "OTHER"}})
        self.assertFalse(other.manual_home_required())

    def test_old_address_only_marker_requires_verified_home_after_move(self):
        (self.server.storage / "flashforge-127.0.0.9-history.manual-home-required").touch()
        status = self.adapter.status(True)
        with self.assertRaises(plugin.ManualHomeRequired):
            self.act("x+1")
        self.act("home")
        monitor = self.server.monitors["127.0.0.1"]
        monitor.update_manual_home({**status, "state": "BUSY", "sensors": {"moveMode": "HOMING"}})
        monitor.update_manual_home(status)
        restored = plugin.PrinterMonitor(self.adapter, monitor.history.path.with_name("new-address.json"))
        restored.bind_manual_printer(status)
        self.assertFalse(restored.manual_home_required())

    def test_5m_release_checks_idle_and_does_not_clear_lock_after_failed_home(self):
        monitor = self.server.monitors["127.0.0.1"]
        self.move_mode = "MOVING"
        with self.assertRaisesRegex(ValueError, "must be idle"):
            self.act("release")
        self.assertNotIn("M18", self.commands)
        self.assertFalse(monitor.manual_release_marker.is_file())
        self.move_mode = "WAIT_ON_TOOL"
        with self.assertRaisesRegex(ValueError, "did not confirm that the axes are idle"):
            self.act("release")
        self.assertNotIn("M18", self.commands)
        self.assertFalse(monitor.manual_release_marker.is_file())
        self.move_mode = "READY"
        self.act("release")
        self.act("home")
        status = self.adapter.status(True)
        monitor.update_manual_home({**status, "state": "BUSY", "sensors": {"moveMode": "HOMING"}})
        monitor.update_manual_home({**status, "state": "ERROR"})
        monitor.update_manual_home(status)
        self.assertTrue(monitor.manual_release_marker.is_file())
        self.assertIn("home", monitor.manual_controls_view(status["manual"])["allowed"])
        with self.assertRaises(plugin.ManualHomeRequired):
            self.act("x+1")

    def test_5m_release_home_recovers_after_lost_reply_and_poll(self):
        monitor = self.server.monitors["127.0.0.1"]
        self.act("release")
        original_command = plugin.command

        def lose_home_reply(sock, code, *args):
            reply = original_command(sock, code, *args)
            if code == "G28":
                raise TimeoutError("Lost the G28 reply")
            return reply

        with patch.object(plugin, "command", side_effect=lose_home_reply):
            with self.assertRaises(TimeoutError):
                self.act("home")
        self.assertEqual(self.commands[-2:], ["G28", "M602"])
        status = self.adapter.status(True)
        controls = status["manual"]
        self.assertTrue(monitor.manual_controls_view(controls)["releaseHoming"])
        with patch.object(plugin.time, "monotonic", return_value=monitor.manual_home_until - 30) as clock:
            parked = {**status, "sensors": {"moveMode": "HOMING", "homingPark": (110, 110, 220)}}
            monitor.update_manual_home(parked)
            clock.return_value += 11
            monitor.update_manual_home(parked)
            self.assertTrue(monitor.manual_release_marker.is_file())
            monitor.update_manual_home({**status, "state": "BUSY", "sensors": {"moveMode": "HOMING"}})
            with patch.object(self.adapter, "status", side_effect=TimeoutError):
                monitor.sample(0)
            self.assertTrue(monitor.manual_controls_view(controls)["releaseHoming"])
            monitor.update_manual_home(status)
        self.assertFalse(monitor.manual_release_marker.is_file())
        self.act("x+1")
        self.act("z+1")
        self.assertIn("G1 X101.000 F300", self.commands)
        self.assertIn("G1 Z6.000 F300", self.commands)

    def test_5m_release_home_confirms_parked_when_session_close_fails(self):
        monitor = self.server.monitors["127.0.0.1"]
        self.act("release")
        original_command = plugin.command

        def lose_close_reply(sock, code, *args):
            reply = original_command(sock, code, *args)
            if code == "M602":
                raise TimeoutError("Lost the M602 reply")
            return reply

        with patch.object(plugin, "command", side_effect=lose_close_reply):
            with self.assertRaises(TimeoutError):
                self.act("home")
        status = self.adapter.status(True)
        parked = {**status, "sensors": {"moveMode": "HOMING", "homingPark": (110, 110, 220)}}
        with patch.object(plugin.time, "monotonic", return_value=monitor.manual_home_until - 30) as clock:
            monitor.update_manual_home(parked)
            self.assertTrue(monitor.manual_release_marker.is_file())
            clock.return_value += 11
            monitor.update_manual_home(parked)
        self.assertFalse(monitor.manual_release_marker.is_file())

    def test_5m_release_home_accepts_reply_after_normal_command_deadline(self):
        monitor = self.server.monitors["127.0.0.1"]
        self.act("release")
        reply = self.printer.reply

        def slow_home(command):
            if command == "G28":
                time.sleep(2.1)
            return reply(command)

        self.printer.reply = slow_home
        self.assertIn("Waiting", self.act("home"))
        status = self.adapter.status(True)
        self.assertTrue(monitor.manual_controls_view(status["manual"])["releaseHomeAcknowledged"])
        parked = {**status, "sensors": {"moveMode": "HOMING", "homingPark": (110, 110, 220)}}
        with patch.object(plugin.time, "monotonic", return_value=monitor.manual_home_until - 30) as clock:
            monitor.update_manual_home(parked)
            clock.return_value += 11
            monitor.update_manual_home(parked)
        self.assertFalse(monitor.manual_release_marker.is_file())

    def test_5m_release_home_confirms_ready_at_final_park_without_a_motion_sample(self):
        monitor = self.server.monitors["127.0.0.1"]
        self.act("release")
        self.act("home")
        self.position = b"X:110.025 Y:110.062 Z:25 A:0\r\nok\r\n"
        monitor.update_manual_home(self.adapter.status(True))
        self.assertTrue(monitor.manual_release_marker.is_file())
        self.position = b"X:110.025 Y:110.062 Z:220 A:0\r\nok\r\n"
        status = self.adapter.status(True)
        self.assertEqual(status["sensors"]["moveMode"], "READY")
        monitor.update_manual_home(status)
        self.assertFalse(monitor.manual_release_marker.is_file())
        self.assertIn("x-1", monitor.manual_controls_view(status["manual"], status)["allowed"])

    def test_released_filament_with_stale_homing_requires_stable_park(self):
        monitor = self.server.monitors["127.0.0.1"]
        self.act("release")
        self.move_mode = "HOMING"
        self.position = b"X:110.037 Y:110.050 Z:220 A:0\r\nok\r\n"
        status = self.adapter.status(True)
        now = time.monotonic()
        with patch.object(plugin.time, "monotonic", return_value=now - 11):
            monitor.update_manual_home(status)
        with self.assertRaises(ValueError):
            self.act("e+10")
        monitor.update_manual_home(status)
        self.act("e+10")
        self.act("e-10")
        self.assertTrue(monitor.manual_release_marker.is_file())
        self.assertIn("G1 E10 F120", self.commands)
        self.assertIn("G1 E-10 F120", self.commands)

    def test_short_settle_only_at_full_parking_position(self):
        monitor = self.server.monitors["127.0.0.1"]
        for z, ready in ((220, True), (25, False)):
            position = (110.05, 110.025, z)
            status = {"sensors": {"stationaryCandidate": position}}
            with patch.object(plugin.time, "monotonic", return_value=100):
                monitor.homing_park_observation = (position, 99, 100)
                self.assertIsNone(monitor.settled_homing_position(status))
                monitor.homing_park_observation = (position, 98, 100)
                self.assertEqual(bool(monitor.settled_homing_position(status)), ready)

    def test_5m_stale_homing_requires_stable_parking_telemetry_and_wire_recheck(self):
        monitor = self.server.monitors["127.0.0.1"]
        self.position = b"X:110.050 Y:110.037 Z:220.000 A:0\r\nok\r\n"
        self.move_mode = "HOMING"
        reply = self.printer.reply

        def reply_with_homing(command):
            data = reply(command)
            if command == "M119" and b"MoveMode:" not in data:
                data = data.replace(b"ok\r\n", b"MoveMode: HOMING\r\nok\r\n")
            return data

        self.printer.reply = reply_with_homing
        status = self.adapter.status(True)
        self.assertEqual(status["sensors"]["homingPark"], (110.05, 110.037, 220))
        now = time.monotonic()
        with patch.object(plugin.time, "monotonic", return_value=now - 10):
            monitor.update_manual_home(status)
        with self.assertRaises(plugin.ManualAxesBusy):
            self.act("x-1")
        with self.assertRaises(plugin.ManualAxesBusy):
            self.act("home")
        with patch.object(plugin.time, "monotonic", return_value=now):
            monitor.update_manual_home(status)
        self.act("x-1")
        self.assertEqual(self.commands[-4:], ["M114", "G90", "G1 X109.050 F300", "M602"])

        self.position = b"X:109.050 Y:110.037 Z:220.000 A:0\r\nok\r\n"
        self.commands.clear()
        with self.assertRaises(plugin.ManualAxesBusy):
            plugin.tcp_manual_move("127.0.0.1", "x-1", self.volume, self.model,
                                   settled_homing=(110.05, 110.037, 220))
        self.assertEqual(self.commands, ["M601 S1", "M119", "M114", "M602"])

        status = self.adapter.status(True)
        monitor.update_manual_home(status)
        self.act("z-1")
        self.assertIn("G1 Z219.000 F300", self.commands)

        # A fresh session away from park requires Home before another jog.
        monitor.expected_manual_position = None
        monitor.update_manual_home(status)
        with patch.object(plugin.time, "monotonic", return_value=time.monotonic() + 11):
            monitor.update_manual_home(status)
            allowed = monitor.manual_controls_view(status["manual"], status)["allowed"]
            self.assertIn("home", allowed)
            self.assertNotIn("x-1", allowed)
            self.act("home")
            self.assertEqual(self.commands[-2:], ["G28", "M602"])

    def test_only_explicit_homing_refusal_requires_home(self):
        self.jog_reply = b"CMD G1 Received.\r\nError: Must home axis first: 101.000 20.000 5.000\r\nok\r\n"
        with self.assertRaises(plugin.ManualHomeRequired):
            self.act("x+1")
        self.assertEqual(self.commands[-1], "M602")
        self.assertNotIn("G28", self.commands)
        self.jog_reply = b"Error: Move out of range.\r\nok\r\n"
        with self.assertRaisesRegex(ValueError, "Printer rejected G1"):
            self.act("x+1")

    def test_5m_jog_checks_centered_xy_limits_before_sending_motion(self):
        for axis, reading in (("X", 109.050), ("Y", 109.025)):
            self.position = f"X:{reading} Y:{reading} Z:220\r\nok\r\n".encode()
            key = axis.lower() + "+1"
            self.assertNotIn(key, self.adapter.status(True)["manual"]["travelLimited"])
            self.act(key)
            self.assertEqual(self.commands[-2:], [f"G1 {axis}110.000 F300", "M602"])
        for position, key in (
            (b"X:110.037 Y:0 Z:5\r\nok\r\n", "x+1"),
            (b"X:-109.500 Y:0 Z:5\r\nok\r\n", "x-1"),
            (b"X:0 Y:109.500 Z:5\r\nok\r\n", "y+1"),
            (b"X:0 Y:-109.500 Z:5\r\nok\r\n", "y-1"),
            (b"X:0 Y:0 Z:0.500\r\nok\r\n", "z-1"),
            (b"X:0 Y:0 Z:219.500\r\nok\r\n", "z+1"),
        ):
            with self.subTest(key=key, position=position):
                self.position = position
                self.commands.clear()
                with self.assertRaisesRegex(ValueError, "axis travel limits"):
                    self.act(key)
                self.assertEqual(self.commands[-4:], ["M601 S1", "M119", "M114", "M602"])

        self.position = b"X:110.037 Y:-109.500 Z:5\r\nok\r\n"
        self.act("x-1")
        self.assertEqual(self.commands[-2:], ["G1 X109.037 F300", "M602"])
        self.act("y+1")
        self.assertEqual(self.commands[-2:], ["G1 Y-108.500 F300", "M602"])

    def test_jog_without_reported_axis_limits_sends_no_motion(self):
        self.commands.clear()
        with self.assertRaisesRegex(ValueError, "did not report the axis travel limits"):
            plugin.tcp_manual_move("127.0.0.1", "x+1", None, self.model)
        self.assertEqual(self.commands, ["M601 S1", "M119", "M114", "M602"])

    def test_ad5x_position_probe_only_reads_the_position(self):
        self.position = b"X:219.5 Y:15 Z:20\r\nok\r\n"
        self.assertEqual(plugin.ad5x_axis_position("127.0.0.1", "X"), 219.5)
        self.assertEqual(self.commands, ["M114"])

    def test_legacy_xyz_jogs_respect_model_limits(self):
        self.printer.detail["pid"] = None
        for model, x, y, z in (("Adventurer 3", 75, 75, 150), ("Adventurer 4 Pro", 110, 100, 250)):
            self.model = model
            self.volume = (x * 2, y * 2, z)
            self.position = f"X:{x} Y:{-y} Z:{z - 0.5}\r\nok\r\n".encode()
            with self.subTest(model=model):
                self.commands.clear()
                for key in ("x+1", "y-1", "z+1"):
                    with self.assertRaisesRegex(ValueError, "axis travel limits"):
                        self.act(key)
                self.assertFalse(any(code.startswith("G1 ") for code in self.commands))
                self.act("x-1")
                self.assertIn(f"G1 X{x - 1:.3f} F300", self.commands)
                self.act("y+1")
                self.assertIn(f"G1 Y{1 - y:.3f} F300", self.commands)
                self.act("z-1")
                self.assertIn(f"G1 Z{z - 1.5:.3f} F300", self.commands)

    def test_jog_waits_for_active_motion_even_when_machine_status_is_ready(self):
        for mode in ("MOVING", "HOMING"):
            with self.subTest(mode=mode):
                self.move_mode = mode
                self.commands.clear()
                with self.assertRaisesRegex(ValueError, "must be idle"):
                    self.act("x+1")
                if mode == "MOVING":
                    self.assertEqual(self.commands[-3:], ["M601 S1", "M119", "M602"])
                self.assertFalse(any(code.startswith("G1 ") for code in self.commands))

    def test_calibration_tracks_confirmed_activity_and_return_to_ready(self):
        monitor = self.server.monitors["127.0.0.1"]
        now = int(time.time())
        printer_api = plugin.printer_api

        def accept_calibration(host, serial, code, path, fields=None, **options):
            if path == "/control":
                return {"code": 0}
            return printer_api(host, serial, code, path, fields, **options)

        with patch.object(plugin, "printer_api", side_effect=accept_calibration):
            self.act("level")
            self.assertEqual(monitor.calibration, {"key": "level", "phase": "waiting"})
            with self.assertRaisesRegex(ValueError, "Waiting for the printer"):
                self.act("shaper")
        monitor.sample(now + 1)
        self.assertEqual(monitor.calibration["phase"], "waiting")
        self.printer.state = "HEATING"
        self.printer.detail["status"] = "calibrate_doing"
        monitor.sample(now + 5)
        self.assertEqual(monitor.latest["state"], "CALIBRATING")
        self.assertIsNone(monitor.latest["percent"])
        self.assertFalse(monitor.latest["active"])
        self.assertEqual(monitor.calibration, {"key": "level", "phase": "running"})
        self.printer.state = "READY"
        self.printer.detail["status"] = "ready"
        monitor.sample(now + 10)
        self.assertEqual(monitor.calibration, {"key": "level", "phase": "ready"})
        monitor.sample(now + 42)
        self.assertIsNone(monitor.calibration)
        with patch.object(plugin, "printer_api", side_effect=accept_calibration):
            self.act("shaper")
        monitor.sample(monitor.calibration_deadline + 1)
        self.assertEqual(monitor.calibration, {"key": "shaper", "phase": "unconfirmed"})

    def test_repeat_feed_and_retract_each_start_from_zero(self):
        for key in ("e+10", "e+10", "e-10", "e-10"):
            self.act(key)
            distance = int(key[1:])
            self.assertEqual(self.commands[-4:], ["M105", "G92 E0", f"G1 E{distance} F120", "M602"])

    def test_ready_jog_records_target_for_rounded_homing_readback(self):
        monitor = self.server.monitors["127.0.0.1"]
        self.act("x-1")
        target = monitor.expected_manual_position
        self.assertIsNotNone(target)
        measured = (target[0] + 0.013, *target[1:])
        status = self.adapter.status(True)
        status["sensors"].update(moveMode="HOMING", stationaryCandidate=measured)
        monitor.update_manual_home(status)
        self.assertEqual(monitor.settled_homing_position(status), measured)

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

    def test_nozzle_heat_feed_cool_cycle(self):
        actual, target = 25, 0
        stale_target = None
        previous = self.printer.reply
        def thermal(cmd):
            nonlocal target, stale_target
            if cmd.startswith("M104 S"):
                stale_target = target
                target = int(cmd[6:])
                return b"ok\r\n"
            if cmd == "M105":
                reported = target if stale_target is None else stale_target
                stale_target = None
                return f"T0:{actual}/{reported} B:25/0\r\nok\r\n".encode()
            return previous(cmd)
        self.printer.reply = thermal
        self.act("release")
        self.act("heat200")
        self.assertEqual(target, 200)
        with self.assertRaises(ValueError):
            self.act("e+10")
        actual = 181
        self.act("e+10")
        self.act("e-10")
        self.act("heat0")
        self.assertEqual(target, 0)
        actual = 179
        with self.assertRaises(ValueError):
            self.act("e-10")
        with self.assertRaises(ValueError):
            self.act("heat999")

    def test_model_capability_matrix(self):
        for model, part in (("Adventurer 3", "nozzle"), ("Adventurer 4 Pro", "nozzle"),
                            ("Adventurer 5M", "bed"), ("Adventurer 5M Pro", "bed"), ("AD5X", "bed")):
            with self.subTest(model=model):
                controls = plugin.manual_controls({"model": model, "status": "ready"}, "READY", model)
                self.assertEqual(controls["zPart"], part)
                self.assertEqual("release" in controls["allowed"], model.startswith("Adventurer 5M"))
        for pid in ("0023", "0024", "0028", "0029", 35, 36, 40, 41):
            d = dict(pid=pid, status="ready", autoShutdown="close", moveCtrl=1, extrudeCtrl=1)
            controls = plugin.manual_controls(d, "READY")
            expected_groups = ["calibration", "timer"]
            if pid in ("0023", "0024", 35, 36):
                expected_groups.append("release")
            self.assertCountEqual(controls["groups"], expected_groups)
            self.assertIn("shaper", controls["allowed"])
            self.assertNotIn("home", controls["allowed"])
            self.assertNotIn("release", controls["allowed"])
            self.assertFalse(plugin.manual_controls(d, "PAUSED")["allowed"])
        self.assertIsNone(plugin.manual_controls({"pid":"9999", "model":"AD5X"}, "READY"))
        self.assertIsNone(plugin.manual_controls({"pid":"0028"}, "READY", "Adventurer 5M"))

    def test_manual_status_keeps_heat_and_timer_with_their_controls(self):
        detail = dict(model=self.model, status="ready", autoShutdown="open", autoShutdownTime=30)
        for temperature, needs_heat in ((25, True), (180, False)):
            with self.subTest(temperature=temperature):
                controls = plugin.manual_controls(detail, "READY", self.model, nozzle=temperature)
                self.assertEqual(controls["description"], "Ready")
                self.assertEqual(controls["timerValue"], "30")
                self.assertEqual(controls["filamentNeedsHeat"], needs_heat)
                self.assertEqual("e+10" in controls["allowed"], not needs_heat)


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
        self.ignore_light = False
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
                    cmd = body["payload"]["cmd"]
                    if cmd == "lightControl_cmd":
                        if not fixture.ignore_light:
                            fixture.detail["lightStatus"] = args["status"]
                    elif cmd == "msConfig_cmd":
                        info = fixture.detail["matlStationInfo"]["slotInfos"][args["slot"] - 1]
                        info.update(materialName=args["mt"], materialColor="#" + args["rgb"].lstrip("#"))
                    elif fixture.detail.get("pid") == "0026" or cmd in ("calibration_cmd", "delayClose_cmd"):
                        if cmd == "errorCodeCtrl_cmd" and args["errorCode"] == fixture.detail["errorCode"]:
                            fixture.detail["errorCode"] = ""
                        elif cmd == "delayClose_cmd":
                            fixture.detail.update(autoShutdown=args["automaticShutdown"], autoShutdownTime=args["shutdownAfterTime"])
                    elif cmd in ("streamCtrl", "streamCtrl_cmd"):
                        self.send_error(400)
                        return
                    elif fixture.control_readbacks is None:
                        fixture.detail["status"] = {"pause": "pause", "continue": "printing",
                                                    "cancel": "canceling"}[args["action"]]
                    else:
                        fixture.pending_states = list(fixture.control_readbacks)
                    result = {"code": 0}
                elif self.path == "/printGcode":
                    fixture.detail.update(status="heating", printFileName=body["fileName"])
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

    def act_with_readbacks(self, action, states, **options):
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
                return service.act(host, action, **options)

    def set_material_station(self):
        self.detail["matlStationInfo"] = {"slotCnt": 4, "stateAction": 0, "slotInfos": [
            {"slotId": 1, "hasFilament": True, "materialName": "PLA", "materialColor": "#112233"},
            {"slotId": 2, "hasFilament": True, "materialName": "PETG", "materialColor": "#445566"},
        ]}

    def test_light_requires_observed_change_and_slot_uses_model_palette(self):
        self.assertEqual(self.act_with_readbacks("light/off", []), "Off")
        self.ignore_light = True
        with self.assertRaisesRegex(ValueError, "did not confirm"):
            self.act_with_readbacks("light/on", [])
        self.set_material_station()
        with self.assertRaisesRegex(ValueError, "station are idle"):
            self.act_with_readbacks("slot/1/PETG/4CAAF8", [])
        self.detail["status"] = "ready"
        for model, pid, color, wire in (("Creator 5 Pro", "29", "4CAAF8", "#4CAAF8"), ("AD5X", "0026", "45A8F9", "45A8F9")):
            with self.subTest(model=model):
                self.detail.update(model=model, pid=pid)
                self.assertEqual(self.act_with_readbacks(f"slot/1/PETG/{color}", []), "Slot updated.")
                command = next(body["payload"] for path, body in reversed(self.calls) if path == "/control")
                self.assertEqual(command, {"cmd": "msConfig_cmd", "args": {"slot": 1, "mt": "PETG", "rgb": wire}})

    def test_creator_reprint_uses_original_materials_and_caches_only_after_observation(self):
        self.detail["status"] = "ready"
        self.set_material_station()
        host = "127.0.0.1"
        entry = {"host": host, "code": "abc01234", "serial": "SN-C5P"}
        with tempfile.TemporaryDirectory() as folder, plugin.LoopbackServer((host, 0), plugin.MonitorHandler) as server:
            server.configure(lambda: {"printers": [entry]}, Path(folder))
            monitor = plugin.PrinterMonitor(self.adapter, Path(folder) / "history.json")
            server.monitors[host] = monitor
            service = plugin.FlashForgeService()
            service._server = server
            monitor.sample(int(time.time()))
            monitor.files = self.adapter.files()
            file_id = next(iter(monitor.files))
            action = f"print/{file_id}/0/1020"
            with self.assertRaisesRegex(ValueError, "original sliced"):
                service.act(host, action)
            port = server.server_port
            worker = threading.Thread(target=HTTPServer.serve_forever, args=(server, 0.05), daemon=True)
            worker.start()
            try:
                url = f"/{host}/print-source?" + urlencode({"file": file_id, "name": "Тест 5.gcode.3mf"})
                headers = {"Origin": f"http://{host}:{port}", "X-FlashForge-Tab-Action": "print-source"}
                with patch.object(plugin, "WEB_PORT", port):
                    status, body, _ = request(port, url, "POST", headers, sliced_source())
                self.assertEqual(status, 200, body)
                token = json.loads(body)["token"]
                with self.assertRaisesRegex(ValueError, "requires PETG"):
                    service.act(host, f"print/{file_id}/0/1010/{token}")
                service.act(host, action + "/" + token)
                payload = next(body for path, body in self.calls if path == "/printGcode")
                self.assertEqual([(item["toolId"], item["slotId"]) for item in payload["materialMappings"]], [(0, 1), (2, 2)])
                self.assertFalse(server.sources)
                monitor.sample(int(time.time()))
                server.confirm_print_source(monitor)
                self.assertTrue(server.sources)
                self.assertTrue(server.source_path.exists())
                with patch.object(plugin, "WEB_PORT", port):
                    status, body, _ = request(port, f"/{host}/print-source?file={file_id}", "POST", headers)
                self.assertEqual(status, 200, body)
                self.assertEqual([item["toolId"] for item in json.loads(body)["requirements"]], [0, 2])
                server.begin_upload()
                self.assertFalse(server.sources)
            finally:
                server.shutdown()
                worker.join()

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
                if cmd == "moveCtrl_cmd":
                    self.assertIn("Axis limits could not be checked", result)

    def test_ad5x_checks_travel_before_http_jog_when_position_is_available(self):
        self.detail.update(pid="0026", model="AD5X", status="ready", moveCtrl=1, measure="220x220x220")
        for key, position in (("x+1", 220), ("y-10", 5), ("z-1", 0), ("z+10", 215)):
            with self.subTest(key=key), patch.object(plugin, "ad5x_axis_position", return_value=position) as read:
                self.calls.clear()
                with self.assertRaisesRegex(ValueError, "axis travel limits"):
                    self.act_with_readbacks("manual/" + key, [])
                read.assert_called_once_with("127.0.0.1", key[0].upper())
                self.assertFalse(any(path == "/control" for path, _ in self.calls))
        with patch.object(plugin, "ad5x_axis_position", return_value=220):
            self.calls.clear()
            self.assertEqual(self.act_with_readbacks("manual/x-1", []), "Printer accepted the command.")
            self.assertEqual(
                [body["payload"] for path, body in self.calls if path == "/control"],
                [{"cmd": "moveCtrl_cmd", "args": {"axis": "x", "delta": -1}}],
            )

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

    def test_early_ad5x_firmware_hides_and_rejects_unsupported_manual_commands(self):
        self.detail.update(pid="0026", model="AD5X", status="ready", rightTemp=195,
                           moveCtrl=1, extrudeCtrl=1, autoShutdown="close")
        for firmware in ("1.0.8", "AD5X-1.1.5-1.1.1-3.0.7"):
            for key, error in (("home", ""), ("x+1", ""), ("e+10", ""), ("clear", "E0048")):
                with self.subTest(firmware=firmware, key=key):
                    self.detail.update(firmwareVersion=firmware, errorCode=error)
                    controls = self.adapter.status(True)["manual"]
                    self.assertEqual(controls["groups"], ["calibration", "timer"])
                    with self.assertRaisesRegex(ValueError, "Command unavailable"):
                        self.act_with_readbacks("manual/" + key, [])
        self.assertFalse(any(path == "/control" for path, _ in self.calls))
        self.detail.update(firmwareVersion="AD5X-1.1.6-1.1.1-3.0.7", errorCode="")
        self.assertIn("home", self.adapter.status(True)["manual"]["allowed"])

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

    def test_grid_action_refuses_to_control_a_different_file(self):
        with self.assertRaisesRegex(ValueError, "print job changed"):
            self.act_with_readbacks("cancel", [], expected_file="Older.3mf")
        self.assertFalse(any(path == "/control" for path, _ in self.calls))
        self.assertEqual(self.act_with_readbacks(
            "pause", ["pause"], expected_file=self.detail["printFileName"]), "PAUSED")

    def test_cancel_rechecks_the_observed_run_before_sending(self):
        host = "127.0.0.1"
        with tempfile.TemporaryDirectory() as folder:
            with plugin.LoopbackServer((host, 0), plugin.MonitorHandler) as server:
                entry = {"host": host, "code": "abc01234", "serial": "SN-C5P"}
                server.configure(lambda: {"printers": [entry]}, Path(folder))
                monitor = plugin.PrinterMonitor(self.adapter, Path(folder) / "history.json")
                server.monitors[host] = monitor
                service = plugin.FlashForgeService()
                service._server = server
                monitor.sample(int(time.time()))
                original = monitor.latest
                job = original["jobToken"]
                fresh = self.adapter.status(True)
                with patch.object(plugin, "control_job") as command:
                    for update in ({"file": "Next.3mf"}, {"layer": 1}):
                        monitor.latest = original.copy()
                        with self.subTest(update=update), patch.object(self.adapter, "status", return_value={**fresh, **update}):
                            with self.assertRaisesRegex(ValueError, "print job changed"):
                                service.act(host, "cancel", expected_job=job)
                        self.assertIsNone(monitor.latest["jobToken"])
                        with self.assertRaisesRegex(ValueError, "print job changed"):
                            service.act(host, "cancel", expected_job=job)
                    command.assert_not_called()

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

    def test_creator_identity_without_model_and_camera_url(self):
        self.detail.pop("cameraStreamUrl")
        for pid, model in (("0028", "Creator 5"), ("0x0029", "Creator 5 Pro"), (41, "Creator 5 Pro")):
            with self.subTest(pid=pid):
                self.detail.update(pid=pid, model="")
                status = self.adapter.status(True)
                self.assertEqual(status["info"]["model"], model)
                self.assertEqual(status["materialFamily"], "creator")
                self.assertEqual(len(status["tools"]), 4)
                self.assertEqual(status["chamber"] is not None, model.endswith(" Pro"))
                self.assertTrue(status["managedCamera"])
                self.assertEqual(status["cameraUrl"], "")
                self.assertIs(status["light"], True)

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

    def test_single_nozzle_models_ignore_extra_temperature_channels(self):
        for model in ("AD5X", "Adventurer 5M"):
            self.detail.update(model=model, pid="0026", rightTemp=201, rightTargetTemp=215)
            data = self.adapter.status(False)
            self.assertEqual((data["nozzle"], data["nozzleTarget"]), (201, 215))
            self.assertEqual(data["tools"], [])

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
                current = json.loads(body)
                self.assertEqual(current["file"], "Тест 5.3mf")
                job = current["jobToken"]
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
                        web_port, path + "action", method="POST", body=json.dumps({"action": action, "job": job}),
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
        placement = patch.object(plugin, "PLACEMENT_FILE", Path(self.folder.name) / "monitor-placement.txt")
        placement.start()
        self.addCleanup(placement.stop)
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
        self.service.active_placement = "device"
        self.config = {"flashforge_host": "127.0.0.1", "check_code": "code", "placement": "device"}
        self.service.get_config = lambda: json.dumps(self.config)
        self.service._page_store = Path(self.folder.name) / "device-pages.json"
        self.service._switch_lock = threading.Lock()
        self.service._placement_lock = threading.Lock()
        self.service._next_placement = None
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
        for key in ("_page_store", "_server", "get_config", "active_placement"):
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

    def test_first_printer_setup_can_open_without_a_profile_address(self):
        self.service._server.printers = lambda: []
        self.service._server.settings_for = lambda: ("", "")
        self.preset.values["print_host"] = ""
        self.service.restore_selected_view()
        url = self.preset.values["print_host_webui"]
        self.assertEqual(url, plugin.monitor_url(plugin.SETUP_PAGE))
        self.assertEqual(plugin.monitor_identity(url), "")
        self.service.save_config = lambda value: self.config.update(json.loads(value))
        self.service.apply_settings([{"host": "127.0.0.1"}], placement="flashforge", first=True)
        self.assertEqual(self.preset.values["print_host"], "127.0.0.1")
        self.assertEqual(self.preset.values["print_host_webui"], plugin.monitor_url("127.0.0.1"))
        self.assertEqual(self.service.active_placement, "device")
        self.assertEqual(plugin.startup_placement(), "flashforge")

    def test_flashforge_model_can_use_monitor_without_rewriting_other_transport(self):
        self.preset.values.update(host_type="octoprint", printer_model="Flashforge Creator 5 Pro", print_host="")
        self.service.restore_selected_view()
        self.assertEqual(self.preset.values["print_host_webui"], plugin.monitor_url("127.0.0.1"))
        self.assertEqual(self.service.check_view("127.0.0.1"), "127.0.0.1")
        self.assertEqual(self.preset.values["print_host"], "")
        self.assertEqual(self.preset.values["host_type"], "octoprint")

    def test_placement_only_preserves_printers_and_first_setup_cannot_overwrite(self):
        saved = []
        self.service.save_config = lambda value: saved.append(json.loads(value))
        self.service.apply_settings(placement="flashforge")
        self.assertEqual(saved[0]["flashforge_host"], "127.0.0.1")
        self.assertEqual(saved[0]["placement"], "flashforge")
        with self.assertRaisesRegex(RuntimeError, "already configured"):
            self.service.apply_settings([{"host": "192.168.1.4"}], first=True)
        self.assertEqual(len(saved), 1)

    def test_first_setup_retries_the_same_save_after_a_placement_write_failure(self):
        self.config.clear()
        self.service._server.settings_for = lambda: (self.config.get("flashforge_host", ""), "")
        self.service.save_config = lambda value: self.config.update(json.loads(value))
        printers = [{"host": "192.168.1.4", "name": "Fixture"}]
        with patch.object(self.service, "write_next_placement", side_effect=PermissionError):
            with self.assertRaises(PermissionError):
                self.service.apply_settings(printers, placement="flashforge", first=True)
        self.assertEqual(self.config["flashforge_host"], "192.168.1.4")
        with self.assertRaisesRegex(RuntimeError, "already configured"):
            self.service.apply_settings([{"host": "192.168.1.5"}], placement="flashforge", first=True)
        with patch.object(self.service, "_point_device_tab") as open_view:
            self.assertEqual(self.service.apply_settings(printers, placement="flashforge", first=True), "192.168.1.4")
            open_view.assert_called_once_with("192.168.1.4")
        self.assertEqual(plugin.PLACEMENT_FILE.read_text(encoding="ascii"), "flashforge")

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

    def test_page_supplies_a_standalone_svg_tab_icon(self):
        from xml.etree import ElementTree
        page = plugin.FlashForgePage()
        path = Path(page.get_icon())
        self.assertEqual(path.parent, Path(self.folder.name))
        self.assertEqual(ElementTree.parse(path).getroot().tag, "{http://www.w3.org/2000/svg}svg")
        modified = path.stat().st_mtime_ns
        self.assertEqual(page.get_icon(), str(path))
        self.assertEqual(path.stat().st_mtime_ns, modified)

    def test_plugin_settings_register_flashforge_page_only_after_reload(self):
        self.service._placement_lock = threading.Lock()
        self.service._next_placement = None
        self.config.pop("placement")
        path = Path(self.folder.name) / "monitor-placement.txt"
        with patch.object(plugin, "PLACEMENT_FILE", path), patch.object(orca, "register_capability") as register, \
                patch.object(plugin.FlashForgeService, "active_placement", "device"):
            package = plugin.FlashForgeDeviceTab()
            for index, choice in enumerate(("device", "flashforge", "device")):
                if index:
                    previous = plugin.FlashForgeService.active_placement
                    self.config["placement"] = choice
                    self.service.sync_next_placement()
                    self.assertEqual(plugin.FlashForgeService.active_placement, previous)
                self.assertEqual(self.service.config()["placement"], choice)
                register.reset_mock()
                package.register_capabilities()
                self.service.active_placement = plugin.FlashForgeService.active_placement
                self.service.restore_selected_view()
                expected = [plugin.FlashForgeService]
                if choice == "flashforge":
                    expected.append(plugin.FlashForgePage)
                self.assertEqual([call.args[0] for call in register.call_args_list], expected)
                self.assertEqual(self.preset.values["print_host_webui"],
                                 plugin.monitor_url("127.0.0.1") if choice == "device" else "")

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
