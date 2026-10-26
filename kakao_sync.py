"""Idempotent handoff from Computer Use to local/hosted schedule storage.

This module never reads KakaoTalk's private database or sends messages.
Computer Use supplies a versioned batch after reading the authorized room.
"""
import argparse
import hashlib
import json
import re
import sqlite3
import uuid
from datetime import date, time
from pathlib import Path
from threading import Lock

ROOT = Path(__file__).resolve().parent
DATA_FIELDS = ["schedule_date", "schedule_time", "category", "title", "company", "quantity", "location", "memo", "specification", "person", "participants", "end_time", "vehicle_count"]
CATEGORIES = {"자재반입", "타설", "회의", "기타"}
KAKAO_ROOM_NAME = "일정공유방☆"
INBOX_LOCK = Lock()


def is_target_room(room_name):
    # Do not guess when multiple rooms have similar names.
    return room_name == KAKAO_ROOM_NAME


def normalize(value):
    return re.sub(r"[\W_]+", "", str(value or "")).casefold()


def validate_batch(raw):
    if isinstance(raw, bytes):
        if len(raw) > 2_000_000:
            raise ValueError("일정 파일은 2MB 이하로 올려주세요.")
        raw = raw.decode("utf-8-sig")
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise ValueError("지원하지 않는 일정 파일 형식입니다.")
    if "room_name" in raw and not is_target_room(raw["room_name"]):
        raise ValueError(f"자동 수집 대상은 ‘{KAKAO_ROOM_NAME}’ 방뿐입니다.")
    source = raw.get("source")
    if not isinstance(source, str) or not source.strip() or len(source) > 300:
        raise ValueError("일정의 출처가 필요합니다.")
    records = raw.get("schedules")
    if not isinstance(records, list) or not 1 <= len(records) <= 200:
        raise ValueError("한 번에 1~200건의 일정을 가져올 수 있습니다.")
    normalized = []
    for index, record in enumerate(records, 1):
        if not isinstance(record, dict):
            raise ValueError(f"{index}번째 일정을 확인해주세요.")
        row = {}
        for field in DATA_FIELDS:
            value = record.get(field)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{index}번째 일정의 입력 형식이 올바르지 않습니다.")
            row[field] = (value or "").strip()
            if len(row[field]) > 20_000:
                raise ValueError(f"{index}번째 일정의 내용이 너무 깁니다.")
        if not row["title"]:
            raise ValueError(f"{index}번째 일정에 일정명이 없습니다.")
        try:
            row["schedule_date"] = date.fromisoformat(row["schedule_date"]).isoformat()
            for field in ("schedule_time", "end_time"):
                row[field] = time.fromisoformat(row[field]).strftime("%H:%M") if row[field] else None
        except ValueError as error:
            raise ValueError(f"{index}번째 일정의 날짜 또는 시간을 확인해주세요.") from error
        row["category"] = row["category"] or "기타"
        if row["category"] not in CATEGORIES:
            raise ValueError(f"{index}번째 일정 구분은 자재반입·타설·회의·기타 중 하나여야 합니다.")
        # Stable identity ignores JSON whitespace and record order.
        identity = record.get("source_id")
        if identity is not None and (not isinstance(identity, str) or len(identity) > 500):
            raise ValueError(f"{index}번째 일정의 출처 번호를 확인해주세요.")
        identity = identity or hashlib.sha256(json.dumps(row, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        row["source_key"] = hashlib.sha256((source + "\n" + identity).encode()).hexdigest()
        normalized.append(row)
    return source, normalized


def import_batch(connection, raw):
    source, records = validate_batch(raw)
    inserted, skipped = [], []
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("""CREATE TABLE IF NOT EXISTS schedule_imports (
            source_key TEXT PRIMARY KEY, schedule_id INTEGER NOT NULL,
            source TEXT NOT NULL, imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")
        for record in records:
            known = connection.execute("SELECT schedule_id FROM schedule_imports WHERE source_key = ?", (record["source_key"],)).fetchone()
            if known:
                skipped.append(known[0])
                continue
            # Include trash: importing an old message must not resurrect a deleted schedule.
            existing = connection.execute("SELECT id,title,location,category,schedule_time,end_time FROM schedules WHERE schedule_date = ?", (record["schedule_date"],)).fetchall()
            duplicate = next((row for row in existing if normalize(row[1]) == normalize(record["title"]) and normalize(row[2]) == normalize(record["location"]) and row[3] == record["category"] and (not record["schedule_time"] or not row[4] or record["schedule_time"] == row[4]) and (not record["end_time"] or not row[5] or record["end_time"] == row[5])), None)
            if duplicate:
                schedule_id = duplicate[0]
                skipped.append(schedule_id)
            else:
                values = [record[field] for field in DATA_FIELDS]
                cursor = connection.execute("INSERT INTO schedules (" + ",".join(DATA_FIELDS) + ") VALUES (" + ",".join("?" for _ in DATA_FIELDS) + ")", values)
                schedule_id = cursor.lastrowid
                inserted.append(schedule_id)
            connection.execute("INSERT INTO schedule_imports (source_key,schedule_id,source) VALUES (?,?,?)", (record["source_key"], schedule_id, source))
    return {"source": source, "inserted": inserted, "skipped": skipped}


def save_direct_read(connection, value):
    """Validate the component envelope and acknowledge only committed imports."""
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError("카카오톡 인식 결과를 전달받지 못했습니다. 다시 시도해주세요.")
    request_id = value.get("request_id")
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 100:
        raise ValueError("카카오톡 요청 번호를 확인하지 못했습니다.")
    batch = value.get("batch")
    if not isinstance(batch, dict) or batch.get("room_name") != KAKAO_ROOM_NAME:
        raise ValueError("지정한 채팅방에서 읽은 내용이 아닙니다.")
    if not isinstance(batch.get("schedules"), list):
        raise ValueError("인식한 일정 목록을 확인하지 못했습니다.")
    pending = value.get("pending", [])
    if not isinstance(pending, list):
        raise ValueError("확인이 필요한 메시지 형식이 올바르지 않습니다.")
    result = import_batch(connection, batch) if batch["schedules"] else {"inserted": [], "skipped": []}
    # import_batch has exited its transaction before this acknowledgment exists.
    return {"request_id": request_id, "status": "saved", "inserted": len(result["inserted"]), "skipped": len(result["skipped"]), "pending": len(pending)}, result, pending[:200]


def enqueue_batch(raw, inbox=None):
    validate_batch(raw)
    inbox = Path(inbox or ROOT / ".kakao-inbox")
    inbox.mkdir(parents=True, exist_ok=True)
    payload = raw if isinstance(raw, bytes) else (raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)).encode("utf-8")
    name = uuid.uuid4().hex
    temporary = inbox / (name + ".tmp")
    temporary.write_bytes(payload)
    destination = inbox / (name + ".json")
    temporary.replace(destination)
    return destination


def drain_inbox(connection, inbox=None):
    with INBOX_LOCK:
        return _drain_inbox(connection, inbox)


def _drain_inbox(connection, inbox=None):
    inbox = Path(inbox or ROOT / ".kakao-inbox")
    if not inbox.exists():
        return []
    results = []
    for path in sorted(inbox.glob("*.json")):
        try:
            result = import_batch(connection, path.read_bytes())
        except FileNotFoundError:
            continue
        except (ValueError, UnicodeError) as error:
            results.append({"error": str(error), "file": path.name})
            destination = inbox / "failed" / path.name
        else:
            results.append(result)
            destination = inbox / "processed" / path.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        path.replace(destination)
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="컴퓨터 유즈가 정리한 일정 파일을 자동등록 수신함에 전달합니다.")
    parser.add_argument("file", type=Path)
    arguments = parser.parse_args()
    print(enqueue_batch(arguments.file.read_bytes()))
