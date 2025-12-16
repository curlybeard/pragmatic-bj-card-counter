import sys
import time
import json
import threading
import argparse
import re
from urllib.parse import urlparse, parse_qs
from typing import Optional, Dict  # <--- Imported for compatibility

import websocket

# ----------------------------
# Helpers
# ----------------------------

def now_ms() -> int:
    return int(time.time() * 1000)

# Changed 'str | None' to 'Optional[str]'
def extract_table_id(ws_url: str) -> Optional[str]:
    try:
        qs = parse_qs(urlparse(ws_url).query)
        # Pragmatic example uses tableId=bjusbim6hcbj2412
        if "tableId" in qs and qs["tableId"]:
            return qs["tableId"][0]
    except Exception:
        pass
    return None

def safe_preview(s: str, n: int = 200) -> str:
    s = s.replace("\n", "\\n")
    return s[:n] + ("…" if len(s) > n else "")

# ----------------------------
# Monitor
# ----------------------------

class WSMonitor:
    def __init__(
        self,
        ws_url: str,
        headers: Dict[str, str], # Changed dict[] to Dict[] for older python
        app_ping_interval: float,
        ws_ping_interval: float,
        ws_ping_timeout: float,
        channel: Optional[str],  # Changed 'str | None' to 'Optional[str]'
        log_raw: bool,
    ):
        self.ws_url = ws_url
        self.headers = headers
        self.header_list = [f"{k}: {v}" for k, v in headers.items()]

        self.app_ping_interval = app_ping_interval
        self.ws_ping_interval = ws_ping_interval
        self.ws_ping_timeout = ws_ping_timeout

        self.channel = channel
        self.log_raw = log_raw

        self._stop = threading.Event()
        self._send_lock = threading.Lock()

        # timestamps
        self.connected_ts = None
        self.last_rx_ts = 0.0
        self.last_non_pong_rx_ts = 0.0
        self.last_app_pong_ts = 0.0
        self.last_ws_pong_ts = 0.0
        self.last_ws_ping_rx_ts = 0.0

        self.ws_app = None

    # ---- WebSocketApp callbacks ----

    def on_open(self, ws):
        self.connected_ts = time.time()
        self.last_rx_ts = time.time()
        self.last_non_pong_rx_ts = time.time()
        print("[OPEN] Connected")

        if self.channel:
            print(f"[INFO] App channel = {self.channel}")
        else:
            print("[WARN] No app channel known. App-level <ping> will be DISABLED unless you pass --channel")

        # Start background threads
        self._stop.clear()
        threading.Thread(target=self._status_loop, daemon=True).start()

        if self.channel and self.app_ping_interval > 0:
            threading.Thread(target=self._app_ping_loop, daemon=True).start()

    def on_close(self, ws, code, reason):
        print(f"[CLOSE] code={code} reason={reason!r}")
        self._stop.set()

    def on_error(self, ws, error):
        print(f"[ERROR] {error!r}")
        # don't set stop here; let close happen, but you can if you want:
        # self._stop.set()

    def on_ping(self, ws, data):
        self.last_ws_ping_rx_ts = time.time()
        print(f"[WS <= PING] {len(data) if data else 0} bytes payload={data!r}")

    def on_pong(self, ws, data):
        self.last_ws_pong_ts = time.time()
        print(f"[WS <= PONG] {len(data) if data else 0} bytes payload={data!r}")

    def on_message(self, ws, message):
        self.last_rx_ts = time.time()

        if isinstance(message, (bytes, bytearray)):
            # pragmatic may send binary video; we ignore but still count as rx
            if self.log_raw:
                print(f"[RX BIN] {len(message)} bytes")
            return

        msg = message.strip()

        # Application-level pong from server
        if msg.startswith("{"):
            try:
                data = json.loads(msg)
            except Exception:
                print(f"[RX TXT] (invalid json) {safe_preview(msg)}")
                self.last_non_pong_rx_ts = time.time()
                return

            if "pong" in data:
                self.last_app_pong_ts = time.time()
                pong = data.get("pong", {})
                print(f"[APP <= PONG] channel={pong.get('channel')} time={pong.get('time')} seq={pong.get('seq')}")
                return

            # Any other JSON is "real data"
            self.last_non_pong_rx_ts = time.time()

            # Minimal summary of keys so we don't spam PII
            keys = list(data.keys())
            print(f"[DATA <= JSON] keys={keys}")

            if self.log_raw:
                print(f"         raw={safe_preview(msg, 400)}")

            return

        # Some servers also use XML-ish messages
        if msg.startswith("<"):
            self.last_non_pong_rx_ts = time.time()
            if msg.startswith("<ping"):
                # IMPORTANT: In your browser, <ping> is outbound, so you may never see this inbound.
                print(f"[APP <= XML] {safe_preview(msg)}")
                return

            print(f"[RX TXT] {safe_preview(msg)}")
            return

        # Other text
        self.last_non_pong_rx_ts = time.time()
        if self.log_raw:
            print(f"[RX TXT] {safe_preview(msg)}")

    # ---- Background loops ----

    def _send_text(self, text: str):
        ws = self.ws_app
        if not ws:
            return
        with self._send_lock:
            try:
                ws.send(text)
            except Exception as e:
                print(f"[SEND ERROR] {e!r}")

    def _app_ping_loop(self):
        """
        App-level heartbeat: browser sends <ping channel="table-..." time="..."/>
        Server replies with JSON {"pong":{...}}
        """
        # small delay so connection is stable
        time.sleep(1.0)

        while not self._stop.is_set():
            t = now_ms()
            ping_xml = f'<ping channel="{self.channel}" time="{t}"/>'
            self._send_text(ping_xml)
            print(f"[APP => PING] channel={self.channel} time={t}")
            self._stop.wait(self.app_ping_interval)

    def _status_loop(self):
        while not self._stop.is_set():
            time.sleep(5)

            if not self.connected_ts:
                continue

            since_open = int(time.time() - self.connected_ts)
            idle_rx = int(time.time() - self.last_rx_ts) if self.last_rx_ts else -1
            idle_data = int(time.time() - self.last_non_pong_rx_ts) if self.last_non_pong_rx_ts else -1
            idle_app_pong = int(time.time() - self.last_app_pong_ts) if self.last_app_pong_ts else -1
            idle_ws_pong = int(time.time() - self.last_ws_pong_ts) if self.last_ws_pong_ts else -1

            print(
                "[STATUS] "
                f"up={since_open}s "
                f"idle_rx={idle_rx}s "
                f"idle_nonpong={idle_data}s "
                f"idle_app_pong={idle_app_pong}s "
                f"idle_ws_pong={idle_ws_pong}s"
            )

    # ---- Run loop ----

    def run_forever(self):
        while True:
            self._stop.clear()
            self.connected_ts = None
            self.last_rx_ts = 0.0
            self.last_non_pong_rx_ts = 0.0
            self.last_app_pong_ts = 0.0
            self.last_ws_pong_ts = 0.0
            self.last_ws_ping_rx_ts = 0.0

            print(f"[CONNECT] {self.ws_url}")
            try:
                self.ws_app = websocket.WebSocketApp(
                    self.ws_url,
                    header=self.header_list,
                    on_open=self.on_open,
                    on_message=self.on_message,
                    on_error=self.on_error,
                    on_close=self.on_close,
                    on_ping=self.on_ping,
                    on_pong=self.on_pong,
                )

                # WS protocol keepalive (control frames)
                if self.ws_ping_interval > 0:
                    self.ws_app.run_forever(
                        ping_interval=self.ws_ping_interval,
                        ping_timeout=self.ws_ping_timeout,
                    )
                else:
                    self.ws_app.run_forever()

            except Exception as e:
                print(f"[RUN ERROR] {e!r}")

            print("[RECONNECT] sleeping 2s…")
            time.sleep(2)

# ----------------------------
# CLI
# ----------------------------

def main():
    p = argparse.ArgumentParser()
    p.add_argument("url", nargs="?", help="wss://... (full pragmatic WS url)")
    p.add_argument("--app-ping", type=float, default=25.0, help="seconds; 0 disables app-level <ping>")
    p.add_argument("--ws-ping", type=float, default=20.0, help="seconds; 0 disables WS protocol ping")
    p.add_argument("--ws-timeout", type=float, default=10.0, help="seconds")
    p.add_argument("--channel", type=str, default=None, help='e.g. "table-bjusbim6hcbj2412"')
    p.add_argument("--log-raw", action="store_true", help="log raw messages (may include PII)")
    p.add_argument("--trace", action="store_true", help="websocket-client low-level trace (very noisy)")
    args = p.parse_args()

    if args.trace:
        websocket.enableTrace(True)

    ws_url = args.url
    if not ws_url:
        print("Usage: python main.py 'wss://...'\n(pass the full WSS URL)")
        sys.exit(1)

    # You can keep these minimal; add what your endpoint requires.
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Origin": "https://duel.com",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Accept-Language": "en-US,en;q=0.9",
    }

    channel = args.channel
    if not channel:
        table_id = extract_table_id(ws_url)
        if table_id:
            channel = f"table-{table_id}"

    mon = WSMonitor(
        ws_url=ws_url,
        headers=headers,
        app_ping_interval=args.app_ping,
        ws_ping_interval=args.ws_ping,
        ws_ping_timeout=args.ws_timeout,
        channel=channel,
        log_raw=args.log_raw,
    )
    mon.run_forever()

if __name__ == "__main__":
    main()