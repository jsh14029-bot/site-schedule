"""Loopback-only bridge for the user-initiated KakaoTalk read button."""
import hmac
import html
import json
import secrets
import sys
import site
from collections import OrderedDict
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock

ROOT = Path(__file__).resolve().parent
if (ROOT / ".autofill-deps").exists():
    site.addsitedir(str(ROOT / ".autofill-deps"))
    sys.path.insert(0, str(ROOT / ".autofill-deps"))
from kakao_pc_reader import read_open_room

ALLOWED_ORIGINS = {"http://localhost:8501", "http://127.0.0.1:8501", "https://site-schedule-edhe97xqsrtqi9ojdpzqle.streamlit.app"}
PORT = 8765
READ_LOCK = Lock()


def load_token():
    folder = ROOT / ".local-agent"
    folder.mkdir(exist_ok=True)
    path = folder / "pairing-token.txt"
    if not path.exists():
        path.write_text(secrets.token_urlsafe(32), encoding="ascii")
    return path.read_text(encoding="ascii").strip()


def make_handler(token, reader=read_open_room):
    results = OrderedDict()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Neither credentials nor message bodies go into request logs.

        def allowed(self):
            return self.headers.get("Host") in {f"127.0.0.1:{PORT}", f"localhost:{PORT}"} and (not self.headers.get("Origin") or self.headers["Origin"] in ALLOWED_ORIGINS)

        def reply(self, status, payload, content_type="application/json; charset=utf-8"):
            data = payload.encode() if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            origin = self.headers.get("Origin")
            if origin in ALLOWED_ORIGINS:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_OPTIONS(self):
            if not self.allowed():
                self.reply(403, {"error": "허용되지 않은 연결입니다."})
                return
            self.send_response(204)
            if self.headers.get("Origin"):
                self.send_header("Access-Control-Allow-Origin", self.headers["Origin"])
            self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Schedule-Token")
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.end_headers()

        def do_GET(self):
            if not self.allowed():
                self.reply(403, {"error": "허용되지 않은 연결입니다."})
            elif self.path == "/health":
                self.reply(200, {"connected": True, "room": "일정공유방☆"})
            elif self.path == "/" and not self.headers.get("Origin"):
                self.reply(200, '<!doctype html><meta charset="utf-8"><title>카카오톡 PC 연결</title><h1>카카오톡 PC 연결 실행 중</h1><p>일정관리 프로그램의 PC 연결코드 칸에 아래 코드를 복사하세요. 다른 사람에게 공유하지 마세요.</p><textarea readonly cols="55" rows="2">' + html.escape(token) + '</textarea><p>대상: 일정공유방☆ · 오늘의 텍스트만 · 메시지 전송 없음</p>', "text/html; charset=utf-8")
            else:
                self.reply(404, {"error": "없는 경로입니다."})

        def do_POST(self):
            if not self.allowed() or not hmac.compare_digest(self.headers.get("X-Schedule-Token", ""), token):
                self.reply(403, {"error": "PC 연결코드를 확인해주세요."})
                return
            if self.path != "/read":
                self.reply(404, {"error": "없는 경로입니다."})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 4096:
                    raise ValueError("요청 크기가 올바르지 않습니다.")
                request = json.loads(self.rfile.read(length))
                read_date = date.fromisoformat(request["read_date"])
                request_id = request["request_id"]
                if not isinstance(request_id, str) or not 1 <= len(request_id) <= 100:
                    raise ValueError("요청 번호를 확인해주세요.")
            except (ValueError, TypeError, KeyError):
                self.reply(400, {"error": "날짜와 요청 내용을 확인해주세요."})
                return
            if not READ_LOCK.acquire(blocking=False):
                self.reply(409, {"error": "이미 카카오톡을 읽고 있습니다. 잠시 후 다시 눌러주세요."})
                return
            try:
                if request_id not in results:
                    result = reader(read_date)
                    result["request_id"] = request_id
                    results[request_id] = result
                    if len(results) > 20:
                        results.popitem(last=False)
                self.reply(200, results[request_id])
            except ValueError as error:
                self.reply(400, {"error": str(error)})
            except ImportError:
                self.reply(500, {"error": "PC 연결용 라이브러리가 없습니다. requirements-pc.txt를 설치해주세요."})
            except Exception:
                self.reply(500, {"error": "채팅 텍스트를 읽지 못했습니다. 일정공유방☆을 열고 카카오톡의 다른 대화상자를 닫은 뒤 다시 시도해주세요."})
            finally:
                READ_LOCK.release()

    return Handler


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), make_handler(load_token()))
    print(f"KakaoTalk PC bridge ready: http://127.0.0.1:{PORT}")
    print("Open that local address to view the pairing code. Keep this program running.")
    server.serve_forever()
