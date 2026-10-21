"""Read only the authorized KakaoTalk room's exported plain-text messages.

The local product uses KakaoTalk's normal Ctrl+S dialog. It never accesses
private Kakao databases, types into a message box, or opens attachments.
"""
import ctypes
import hashlib
import re
import sys
import time as clock
import uuid
from datetime import date
from pathlib import Path

from kakao_sync import KAKAO_ROOM_NAME
from schedule_autofill import parse_schedule_text

ROOT = Path(__file__).resolve().parent
DAY = re.compile(r"^-*\s*(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일\s*(?:[월화수목금토일]요일)?\s*-*\s*$")
MESSAGE = re.compile(r"^\[([^\]\n]+)\]\s*\[((?:오전|오후)\s*\d{1,2}:\d{2})\]\s*(.*)$")
MOBILE = re.compile(r"^(\d{4})[.년/-]\s*(\d{1,2})[.월/-]\s*(\d{1,2})[.일]?\s*((?:오전|오후)\s*\d{1,2}:\d{2}),\s*(.*?)\s*:\s*(.*)$")
WORK = re.compile(r"반입|타설|회의|미장|정착|작업|검측|검사|자재|철근|납품|입고")
MEDIA = re.compile(r"^(?:사진(?:\s*\d+장)?|동영상|이모티콘|파일|삭제된 메시지입니다\.?|메시지가 삭제되었습니다\.?)$")


def parse_transcript(text, read_date):
    """Filter by posting date before examining schedule content."""
    read_date = date.fromisoformat(str(read_date))
    if KAKAO_ROOM_NAME not in text[:500]:
        raise ValueError(f"‘{KAKAO_ROOM_NAME}’ 대화 내용인지 확인하지 못했습니다.")
    messages = []
    current_day, current = None, None
    for line in text.lstrip("\ufeff").splitlines():
        day_match = DAY.fullmatch(line.strip())
        mobile_match = MOBILE.match(line)
        message_match = MESSAGE.match(line)
        if day_match:
            current_day = date(*map(int, day_match.groups()))
            current = None
        elif mobile_match:
            y, m, d, posted_time, sender, body = mobile_match.groups()
            current_day = date(int(y), int(m), int(d))
            current = {"day": current_day, "time": posted_time, "sender": sender, "lines": [body]}
            messages.append(current)
        elif message_match:
            sender, posted_time, body = message_match.groups()
            current = {"day": current_day, "time": posted_time, "sender": sender, "lines": [body]}
            messages.append(current)
        elif current is not None:
            current["lines"].append(line)
    if not any(message["day"] is not None for message in messages):
        raise ValueError("대화 날짜를 확인하지 못했습니다. 카카오톡 대화 내용 저장 형식을 확인해주세요.")
    schedules, pending = [], []
    count = 0
    for message in messages:
        if message["day"] != read_date:
            continue
        body = "\n".join(message["lines"]).strip()
        count += 1
        attachment = re.match(r"^(?:파일|사진|동영상)\s*:", body) or re.fullmatch(r"[^\n]+\.(?:pdf|xlsx?|docx?|pptx?|hwp|hwpx|zip|dwg|png|jpe?g|mp4)", body, re.IGNORECASE)
        if not body or MEDIA.fullmatch(body) or attachment or re.fullmatch(r"https?://\S+", body) or not WORK.search(body):
            continue
        identity = hashlib.sha256((str(read_date) + message["sender"] + message["time"] + body).encode()).hexdigest()
        fields, warnings = parse_schedule_text(body, today=read_date)
        reasons = []
        if "schedule_date" not in fields:
            reasons.append("작업 날짜가 명시되지 않았습니다.")
        if "title" not in fields:
            reasons.append("일정명을 확인해주세요.")
        if re.search(r"취소|변경|연기|완료|중지", body):
            reasons.append("변경·완료·중지 여부를 확인해주세요.")
        if any("여러 날짜" in warning or "올바르지" in warning or "시간을 확인" in warning for warning in warnings):
            reasons.extend(warnings)
        if reasons:
            pending.append({"id": identity, "sender": message["sender"], "posted_time": message["time"], "text": body, "reasons": reasons})
            continue
        for key, value in list(fields.items()):
            if isinstance(value, date):
                fields[key] = value.isoformat()
            elif hasattr(value, "strftime"):
                fields[key] = value.strftime("%H:%M")
        vehicle_text = fields.get("vehicle_count", "")
        if not re.search(r"(?:물량|수량)\s*[:：]", body) and fields.get("quantity") and fields["quantity"] in vehicle_text:
            fields.pop("quantity", None)  # '차량대수: 25T 3대' describes trucks, not material quantity.
        vehicle_match = re.search(r"(\d+)\s*대\b", vehicle_text)
        if vehicle_match:
            fields["vehicle_count"] = vehicle_match[1]
        elif vehicle_text and not re.fullmatch(r"\d+", vehicle_text):
            fields.pop("vehicle_count", None)
        fields.setdefault("category", "기타")
        fields.setdefault("schedule_time", None)
        fields.setdefault("end_time", None)
        fields["source_id"] = identity
        fields["memo"] = f"출처: {KAKAO_ROOM_NAME} / {read_date} {message['time']} / {message['sender']}\n원문:\n{body}"
        schedules.append(fields)
    batch = {"version": 1, "room_name": KAKAO_ROOM_NAME, "source": f"KakaoTalk:{KAKAO_ROOM_NAME}", "schedules": schedules}
    return {"batch": batch, "pending": pending, "messages_read": count, "read_date": read_date.isoformat()}


def _is_kakao_process(pid):
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buffer))
        return bool(kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size))) and Path(buffer.value).name.casefold() == "kakaotalk.exe"
    finally:
        kernel.CloseHandle(handle)


def read_open_room(read_date):
    if sys.platform != "win32":
        raise ValueError("카카오톡이 열린 Windows PC에서 연결 프로그램을 실행해주세요.")
    import pythoncom
    pythoncom.CoInitialize()
    try:
        return _read_open_room_windows(read_date)
    finally:
        pythoncom.CoUninitialize()


def read_exported_transcript(exported, read_date, timeout=15):
    """Kakao may still hold the export open after the Save button returns."""
    deadline = clock.monotonic() + timeout
    last_error = None
    while clock.monotonic() < deadline:
        try:
            data = exported.read_bytes()
        except (FileNotFoundError, PermissionError) as error:
            last_error = error
            clock.sleep(0.15)
            continue
        if not data:
            clock.sleep(0.15)
            continue
        encodings = ("utf-16",) if data.startswith((b"\xff\xfe", b"\xfe\xff")) else ("utf-8-sig", "cp949")
        for encoding in encodings:
            try:
                text = data.decode(encoding)
            except UnicodeError:
                continue
            return parse_transcript(text, read_date)
        raise ValueError("저장된 대화 파일의 문자 형식을 읽지 못했습니다.")
    if isinstance(last_error, PermissionError):
        raise ValueError("대화 파일은 저장됐지만 카카오톡이 파일을 사용 중이라 읽지 못했습니다. 잠시 후 다시 눌러주세요.")
    raise ValueError("지정한 위치에서 저장된 대화 파일을 찾지 못했습니다. 저장창이 닫혔는지 확인해주세요.")


def _read_open_room_windows(read_date):
    from pywinauto import Desktop
    windows = [window for window in Desktop(backend="win32").windows(title=KAKAO_ROOM_NAME, visible_only=True) if _is_kakao_process(window.process_id())]
    if len(windows) != 1:
        raise ValueError(f"‘{KAKAO_ROOM_NAME}’ 채팅방을 별도 창으로 하나만 열어주세요. 현재 {len(windows)}개입니다.")
    room = windows[0]
    if not room.is_enabled():
        raise ValueError("카카오톡에 열린 대화상자를 닫은 뒤 다시 눌러주세요.")
    folder = ROOT / ".local-agent"
    folder.mkdir(exist_ok=True)
    exported = folder / ("transcript-" + uuid.uuid4().hex + ".txt")
    # These are product UI operations, bounded to the verified KakaoTalk process.
    stage = "채팅방 선택"
    try:
        room.set_focus()
        room.type_keys("^s", set_foreground=True)
        stage = "저장창 찾기"
        # This is the native common file dialog. Use its Win32 controls;
        # UIA discovery can miss Kakao's owned modal and leave Ctrl+S unfinished.
        desktop = Desktop(backend="win32")
        dialog = desktop.window(process=room.process_id(), title_re="^(다른 이름으로 저장|Save As)$", class_name="#32770")
        dialog.wait("visible", timeout=12)
        stage = "저장 파일명 입력"
        filename = dialog.child_window(control_id=1001, class_name="Edit")
        filename.set_edit_text(str(exported))
        if filename.window_text() != str(exported):
            raise ValueError("저장창의 파일명을 자동으로 입력하지 못했습니다. 아직 일정은 등록하지 않았습니다.")
        stage = "저장 버튼 실행"
        # BM_CLICK must be posted, not sent synchronously. Kakao's save handler
        # opens a modal "export complete" window before returning. A synchronous
        # click/invoke therefore waits for that modal and never reaches file read.
        dialog.child_window(control_id=1, class_name="Button").post_message(0x00F5, 0, 0)
        stage = "저장 파일 읽기"
        return read_exported_transcript(exported, read_date)
    except (ValueError, ImportError):
        raise
    except Exception as error:
        # Do not expose chat text, local paths or credentials in browser errors.
        raise ValueError(f"카카오톡 연결 중 ‘{stage}’ 단계에서 실패했습니다. 오류 종류: {type(error).__name__}") from error
    finally:
        if exported.parent.resolve() == folder.resolve():
            try:
                exported.unlink(missing_ok=True)
            except OSError:
                # Cleanup must never discard a successfully parsed schedule batch.
                pass
