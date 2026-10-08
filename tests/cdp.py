"""A tiny Chrome DevTools Protocol client (stdlib only: raw websocket) for driving a real browser in the UI checks.

``Browser.connect(port)`` attaches to a browser started with ``--remote-debugging-port``; ``page.eval(js)`` runs
JavaScript in the page and returns its JSON value (``await`` works), ``page.wait(js)`` polls an expression until true.
Console errors and uncaught exceptions of the page are collected in ``page.errors``."""
from __future__ import annotations

import base64
import json
from pathlib import Path
import os
import socket
import struct
import time
import urllib.request


class WebSocket:
    def __init__(self, url: str) -> None:
        assert url.startswith("ws://")
        host, _, path = url[5:].partition("/")
        hostname, _, port = host.partition(":")
        self.sock = socket.create_connection((hostname, int(port or 80)), timeout=30)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET /{path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            head += self.sock.recv(1)
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise ConnectionError(head.decode(errors="replace"))

    def send(self, text: str) -> None:
        data = text.encode()
        mask = os.urandom(4)
        n = len(data)
        header = bytes([0x81])
        if n < 126:
            header += bytes([0x80 | n])
        elif n < 65536:
            header += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            header += bytes([0x80 | 127]) + struct.pack(">Q", n)
        self.sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _read(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("websocket closed")
            buf += chunk
        return buf

    def recv(self) -> str:
        message = b""
        while True:
            b1, b2 = self._read(2)
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            payload = self._read(n)
            if b1 & 0x0F in (1, 0):
                message += payload
                if b1 & 0x80:
                    return message.decode()

    def close(self) -> None:
        self.sock.close()


class Page:
    def __init__(self, ws_url: str) -> None:
        self.ws = WebSocket(ws_url)
        self._id = 0
        self.errors: list[str] = []
        self.call("Runtime.enable")
        self.call("Page.enable")

    def call(self, method: str, **params):
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("method") == "Runtime.exceptionThrown":
                d = msg["params"]["exceptionDetails"]
                self.errors.append((d.get("exception") or {}).get("description") or d.get("text", ""))
            elif msg.get("method") == "Runtime.consoleAPICalled" and msg["params"]["type"] == "error":
                self.errors.append(" ".join(str(a.get("value", a.get("description", ""))) for a in msg["params"]["args"]))
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(msg["error"])
                return msg["result"]

    def goto(self, url: str) -> None:
        """Load ``url`` afresh (a fragment-only change would not reload the page, so go through about:blank)."""
        self.call("Page.navigate", url="about:blank")
        self.wait("location.href === 'about:blank'")
        self.call("Page.navigate", url=url)
        self.wait(f"location.href.startsWith({url.split('#')[0]!r}) && document.readyState === 'complete'")

    def eval(self, js: str):
        res = self.call("Runtime.evaluate", expression=js, awaitPromise=True, returnByValue=True)
        if "exceptionDetails" in res:
            d = res["exceptionDetails"]
            raise RuntimeError((d.get("exception") or {}).get("description") or d.get("text"))
        return res["result"].get("value")

    def wait(self, js: str, timeout: float = 30.0, every: float = 0.2):
        """Poll ``js`` until it is truthy in the page (an element counts as true, null / false / 0 / "" do not)."""
        end = time.time() + timeout
        last: object = None
        while time.time() < end:
            try:
                res = self.call("Runtime.evaluate", expression=js, awaitPromise=True)["result"]
                if "exceptionDetails" not in res and res.get("type") != "undefined" and res.get("subtype") != "null":
                    if res.get("type") == "object" or res.get("value") not in (False, 0, "", None):
                        return res.get("value", True)
                last = res.get("value", res.get("description"))
            except RuntimeError as exc:
                last = str(exc)
            time.sleep(every)
        raise TimeoutError(f"timed out waiting for: {js} (last: {last!r})")

    def screenshot(self, path: str, width: int = 1280, height: int = 900) -> None:
        """Save a PNG of the whole page at the given viewport width (for looking at, not for asserting)."""
        import base64
        self.call("Emulation.setDeviceMetricsOverride", width=width, height=height, deviceScaleFactor=1, mobile=False)
        full = self.eval("Math.max(document.documentElement.scrollHeight, 400)")
        self.call("Emulation.setDeviceMetricsOverride", width=width, height=min(int(full), 2400), deviceScaleFactor=1, mobile=False)
        data = self.call("Page.captureScreenshot", format="png")["data"]
        Path(path).write_bytes(base64.b64decode(data))
        self.call("Emulation.clearDeviceMetricsOverride")

    def close(self) -> None:
        self.ws.close()


class Browser:
    def __init__(self, port: int) -> None:
        self.port = port

    @classmethod
    def connect(cls, port: int) -> "Browser":
        urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=5).read()
        return cls(port)

    def shutdown(self) -> None:
        """Ask the browser to quit (``Browser.close``): every process of it exits. Needed when the browser was started
        through ``flatpak-spawn --host``: stopping that client leaves the browser running on the host."""
        try:
            info = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/version", timeout=5).read())
            ws = WebSocket(info["webSocketDebuggerUrl"])
            try:
                ws.send(json.dumps({"id": 1, "method": "Browser.close"}))
                try:
                    ws.recv()
                except (ConnectionError, OSError):
                    pass
            finally:
                ws.close()
        except (OSError, ValueError, KeyError, ConnectionError):
            pass

    def new_page(self) -> Page:
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/json/new?about:blank", method="PUT")
        info = json.loads(urllib.request.urlopen(req, timeout=10).read())
        return Page(info["webSocketDebuggerUrl"])
