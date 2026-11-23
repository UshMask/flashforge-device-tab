"""Local Creator 5 Pro fixture for the browser lifecycle test. No physical printer."""
import hashlib
import json
import struct
import sys
import tempfile
import threading
import time
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from test_plugin import CreatorHTTPTests, plugin


def main():
    with ExitStack() as stack:
        creator = CreatorHTTPTests()
        stack.callback(creator.doCleanups)
        creator.setUp()
        creator.detail.pop("cameraStreamUrl")  # Firmware can omit the URL.
        events = []
        hashes = set()
        enabled = False
        frames = 0
        sessions = 0

        def control(action):
            nonlocal enabled
            assert action in ("open", "close")
            events.append(action)
            enabled = action == "open"

        creator.camera_control = control
        jpeg = (Path(__file__).parent / "camera-frame.jpg").read_bytes()

        class Camera(BaseHTTPRequestHandler):
            def do_GET(self):
                nonlocal frames, sessions
                if self.path == "/stats":
                    body = json.dumps(dict(events=events, sessions=sessions, frames=frames,
                                           hashes=list(hashes))).encode()
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if not enabled or self.path != "/?action=stream":
                    self.send_error(409)
                    return
                sessions += 1
                events.append("video")
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=cam")
                self.end_headers()
                try:
                    while enabled:
                        frames += 1
                        # Standard JPEG comment makes each downloaded frame identifiable.
                        frame = jpeg[:2] + b"\xff\xfe\x00\x08\xff\xd9" + struct.pack(">I", frames) + jpeg[2:]
                        hashes.add(hashlib.sha256(frame).hexdigest())
                        self.wfile.write(b"--cam\r\nContent-Type: image/jpeg\r\n\r\n" + frame[:1])
                        time.sleep(0.01)
                        self.wfile.write(frame[1:] + b"\r\n")
                        time.sleep(0.12)
                except OSError:
                    pass

            def log_message(self, *args):
                pass

        host = "127.0.0.1"
        camera = stack.enter_context(ThreadingHTTPServer((host, 0), Camera))
        panel = stack.enter_context(plugin.LoopbackServer((host, 0), plugin.MonitorHandler))
        stack.enter_context(patch.multiple(plugin, CAMERA_PORT=camera.server_port, WEB_PORT=panel.server_port))
        storage = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        panel.configure(lambda: {"printers": [{"host": host, "serial": "SN-C5P", "code": "abc01234"}]}, storage)
        for server in (camera, panel):
            threading.Thread(target=server.serve_forever, daemon=True).start()
            stack.callback(server.shutdown)
        print(json.dumps({"panel": f"http://{host}:{panel.server_port}/{host}/",
                          "stats": f"http://{host}:{camera.server_port}/stats"}), flush=True)
        sys.stdin.readline()


if __name__ == "__main__":
    main()
