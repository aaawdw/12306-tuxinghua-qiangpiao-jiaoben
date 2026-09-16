import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

DEMO_CONFIG = {
    "from": "北京西", "to": "西安北", "travelDate": "2026-09-30", "saleAt": "2026-09-17T09:15:00+08:00",
    "passengers": [{"name": "演练乘客", "ticket": "成人票"}],
    "preferences": [{"train": "G87", "seat": "二等座"}],
    "allowNoSeat": False, "berth": "不限", "waitlist": "关闭", "pollIntervalMs": 3000,
    "maxRunMinutes": 0.1, "browser": "chrome",
}


class DemoServer:
    def __init__(self):
        self.records = {"submits": 0, "payments": 0}
        records = self.records
        pages = Path(__file__).with_name("demo_pages")

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                route = urlparse(self.path).path
                if route == "/otn/leftTicket/query":
                    content = json.dumps({"status": True, "data": {"result": ["demo"]}}).encode()
                    kind = "application/json"
                else:
                    name = "order" if route == "/order" else "payment" if route == "/otn/payOrder/init" else "query"
                    content = (pages / (name + ".html")).read_bytes()
                    kind = "text/html;charset=utf-8"
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)

            def do_POST(self):
                if self.path == "/record/submit":
                    records["submits"] += 1
                elif self.path == "/record/pay":
                    records["payments"] += 1
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
