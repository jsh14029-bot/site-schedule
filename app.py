import json
import sqlite3
import hashlib
import os
from pathlib import Path
from html import escape
from datetime import date, time, datetime, timedelta, timezone
from functools import lru_cache

try:
    import holidays
except ImportError:
    holidays = None

import streamlit as st
from streamlit_calendar import calendar
from schedule_autofill import parse_schedule_text, recognize_schedule_image
from kakao_sync import KAKAO_ROOM_NAME, ROOT as KAKAO_ROOT, drain_inbox, import_batch, save_direct_read
from cloud_storage import open_database, migrate_postgres, StorageError

DATABASE_ERRORS = (sqlite3.Error, StorageError)

st.set_page_config(page_title="현장 일정관리", page_icon="🏗️", layout="wide")

CATEGORIES = ["자재반입", "타설", "회의", "기타"]
CATEGORY_COLORS = {"자재반입": "#1976D2", "타설": "#D32F2F", "회의": "#FFEB3B", "기타": "#FFFFFF"}
# A안: 협의기관은 company, 장소/위치는 location을 재사용합니다.
FIELDS = {
    "자재반입": [("company", "업체"), ("quantity", "물량"), ("location", "위치"), ("specification", "규격")],
    "타설": [("company", "업체"), ("quantity", "물량"), ("location", "위치"), ("specification", "규격")],
    "주요공정": [("company", "업체"), ("quantity", "물량"), ("location", "위치")],
    "검측/감리": [("company", "업체"), ("location", "위치"), ("person", "담당자")],
    "대외업무": [("company", "협의기관"), ("location", "장소"), ("person", "담당자")],
    "회의": [("participants", "참석대상"), ("location", "장소")],
    "기타": [("company", "업체"), ("location", "장소")],
}
for category_fields in FIELDS.values():
    for index, (field, _) in enumerate(category_fields):
        if field == "quantity":
            category_fields.insert(index + 1, ("vehicle_count", "차량대수"))
            break
DETAIL_KEYS = ["company", "quantity", "location", "specification", "person", "participants", "vehicle_count"]
DB_COLUMNS = ["schedule_date", "schedule_time", "category", "title", "company", "quantity", "location", "memo", "specification", "person", "participants", "end_time", "vehicle_count"]


def migrate_database(connection):
    if getattr(connection, "is_postgres", False):
        migrate_postgres(connection)
        return
    # 기존 행을 변경하지 않고 동일 트랜잭션에서 부족한 열만 추가합니다.
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute("""CREATE TABLE IF NOT EXISTS schedules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            schedule_date TEXT NOT NULL, schedule_time TEXT, category TEXT,
            title TEXT NOT NULL, company TEXT, quantity TEXT, location TEXT, memo TEXT
        )""")
        existing = {row[1] for row in connection.execute("PRAGMA table_info(schedules)")}
        for column in ("specification", "person", "participants", "end_time", "vehicle_count"):
            if column not in existing:
                connection.execute(f"ALTER TABLE schedules ADD COLUMN {column} TEXT")
        if "completed" not in existing:
            connection.execute("ALTER TABLE schedules ADD COLUMN completed INTEGER NOT NULL DEFAULT 0")
        if "deleted_at" not in existing:
            connection.execute("ALTER TABLE schedules ADD COLUMN deleted_at TEXT")
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def get_schedules(connection, start=None, end=None, deleted=False):
    sql = "SELECT id, completed, " + ", ".join(DB_COLUMNS) + " FROM schedules WHERE deleted_at IS " + ("NOT NULL" if deleted else "NULL")
    params = ()
    if start is not None:
        sql += " AND schedule_date BETWEEN ? AND ?"
        params = (str(start), str(end))
    return connection.execute(sql + " ORDER BY schedule_date, schedule_time, id", params).fetchall()


def detail_items(row):
    fields = FIELDS.get(row["category"], FIELDS["기타"])
    items = [(label, row[key]) for key, label in fields if row[key]]
    # 구버전 및 구분 변경으로 현재 폼에 없는 값도 표시해 정보 누락 방지.
    used = {key for key, label in fields}
    labels = dict(zip(DETAIL_KEYS, ["업체(기존)", "물량(기존)", "장소(기존)", "규격(기존)", "담당자(기존)", "참석대상(기존)", "차량대수(기존)"]))
    items.extend((labels[key], row[key]) for key in DETAIL_KEYS if key not in used and row[key])
    if row["memo"]:
        items.append(("비고", row["memo"]))
    return items


def schedule_time_label(row):
    arrival = row["schedule_time"]
    end = row["end_time"]
    if end:
        return f"도착 {arrival} ~ 종료 {end}" if arrival else f"종료 {end}"
    return f"도착 {arrival}" if arrival else "시간 미정"


def show_schedule(row):
    st.markdown(f"### {row['schedule_date']} · {schedule_time_label(row)}")
    title = escape(f"[{row['category']}] {row['title']}")
    if row["completed"]:
        st.markdown(f"✅ 완료 · <strong><s>{title}</s></strong>", unsafe_allow_html=True)
    else:
        st.markdown(f"<strong>{title}</strong>", unsafe_allow_html=True)
    for label, value in detail_items(row):
        st.write(f"{label} : {value}")


def set_completed(connection, schedule_id, completed):
    with connection:
        connection.execute("UPDATE schedules SET completed = ? WHERE id = ?", (int(completed), schedule_id))


def delete_schedule(connection, schedule_id):
    with connection:
        connection.execute("UPDATE schedules SET deleted_at = CURRENT_TIMESTAMP WHERE id = ? AND deleted_at IS NULL", (schedule_id,))


def restore_schedule(connection, schedule_id):
    with connection:
        connection.execute("UPDATE schedules SET deleted_at = NULL WHERE id = ?", (schedule_id,))


def empty_trash(connection, schedule_ids):
    with connection:
        connection.executemany("DELETE FROM schedules WHERE id = ? AND deleted_at IS NOT NULL", [(schedule_id,) for schedule_id in schedule_ids])


def show_trash(connection):
    deleted_rows = get_schedules(connection, deleted=True)
    with st.container(border=True):
        st.subheader(f"🗑️ 휴지통 · {len(deleted_rows)}건")
        st.caption("삭제한 일정을 확인하고 되돌릴 수 있습니다.")
        if not deleted_rows:
            st.info("휴지통이 비어 있습니다.")
            st.session_state.pop("empty_trash_ids", None)
        if st.button("🗑️ 휴지통 비우기", disabled=not deleted_rows, use_container_width=True):
            st.session_state["empty_trash_ids"] = [row["id"] for row in deleted_rows]
        pending_ids = st.session_state.get("empty_trash_ids")
        if pending_ids:
            st.warning(f"선택한 삭제 일정 {len(pending_ids)}건을 영구 삭제합니다. 비우면 복구할 수 없습니다.")
            confirm, cancel = st.columns(2)
            if confirm.button("영구 삭제", type="primary", use_container_width=True):
                empty_trash(connection, pending_ids)
                st.session_state.pop("empty_trash_ids", None)
                st.session_state["flash"] = "휴지통을 비웠습니다."
                st.rerun()
            if cancel.button("취소", use_container_width=True):
                st.session_state.pop("empty_trash_ids", None)
                st.rerun()
        st.divider()
        for row in deleted_rows:
            with st.container(border=True):
                show_schedule(row)
                if st.button("↩️ 삭제 취소 (복구)", key=f"restore_{row['id']}", use_container_width=True):
                    restore_schedule(connection, row["id"])
                    st.session_state.pop("empty_trash_ids", None)
                    st.session_state["selected_schedule_id"] = row["id"]
                    st.session_state["flash"] = "✅ 삭제를 취소하고 일정을 복구했습니다."
                    st.rerun()


def completion_button(connection, row, prefix):
    if st.button("↩️ 완료 취소" if row["completed"] else "✅ 완료", key=f"{prefix}_complete_{row['id']}"):
        set_completed(connection, row["id"], not row["completed"])
        st.session_state["flash"] = "완료를 취소했습니다." if row["completed"] else "✅ 완료한 일정으로 표시했습니다."
        st.rerun()


def schedule_form(prefix, category, saved=None):
    saved = dict(saved) if saved is not None else {}
    try:
        saved_date = date.fromisoformat(saved.get("schedule_date") or date.today().isoformat())
    except ValueError:
        saved_date = date.today()
    try:
        saved_time = time.fromisoformat(saved["schedule_time"]) if saved.get("schedule_time") else (None if saved else time(9, 0))
    except ValueError:
        saved_time = time(9, 0)
    try:
        saved_end_time = time.fromisoformat(saved["end_time"]) if saved.get("end_time") else None
    except ValueError:
        saved_end_time = None
    # 카테고리 변경 시 입력 중인 공통값을 유지합니다.
    widget_keys = ["schedule_date", "schedule_time", "end_time", "title", "memo"] + DETAIL_KEYS
    draft = st.session_state.get(f"{prefix}_draft", {})
    for key in widget_keys:
        state_key = f"{prefix}_{key}"
        if state_key not in st.session_state:
            st.session_state[state_key] = draft.get(key, saved_date if key == "schedule_date" else saved_time if key == "schedule_time" else saved_end_time if key == "end_time" else saved.get(key) or "")
    values = {key: st.session_state[f"{prefix}_{key}"] for key in DETAIL_KEYS}
    # 조건부로 숨겨지는 위젯의 값을 Streamlit 정리 이후에도 보관.
    with st.form(f"{prefix}_form"):
        left, arrival_col, end_col = st.columns(3)
        with left:
            values["schedule_date"] = st.date_input("날짜", key=f"{prefix}_schedule_date")
        with arrival_col:
            values["schedule_time"] = st.time_input("도착시간", value=None, key=f"{prefix}_schedule_time")
        with end_col:
            values["end_time"] = st.time_input("종료시간", value=None, key=f"{prefix}_end_time")
        values["category"] = category
        values["title"] = st.text_input("일정명 *", key=f"{prefix}_title")
        for key, label in FIELDS.get(category, FIELDS["기타"]):
            values[key] = st.text_input(label, key=f"{prefix}_{key}")
        values["memo"] = st.text_area("비고", key=f"{prefix}_memo")
        submitted = st.form_submit_button("💾 수정사항 저장" if saved else "💾 일정 등록", use_container_width=True)
    st.session_state[f"{prefix}_draft"] = dict(values)
    return submitted, values


def save_schedule(connection, values, schedule_id=None):
    data = dict(values)
    data["schedule_date"] = str(data["schedule_date"])
    data["schedule_time"] = data["schedule_time"].strftime("%H:%M") if data["schedule_time"] else None
    data["end_time"] = data["end_time"].strftime("%H:%M") if data.get("end_time") else None
    params = [data[key].strip() if isinstance(data[key], str) else data[key] for key in DB_COLUMNS]
    with connection:
        if schedule_id is None:
            connection.execute("INSERT INTO schedules (" + ", ".join(DB_COLUMNS) + ") VALUES (" + ", ".join("?" for _ in DB_COLUMNS) + ")", params)
        else:
            connection.execute("UPDATE schedules SET " + ", ".join(key + " = ?" for key in DB_COLUMNS) + " WHERE id = ?", params + [schedule_id])


def korean_date(value):
    parsed = date.fromisoformat(str(value))
    return f"{parsed.month}/{parsed.day}({'월화수목금토일'[parsed.weekday()]})"


def make_notice(rows, start, end):
    lines = ["📢 [현장 주요 일정]", f"기간 : {korean_date(start)} ~ {korean_date(end)}", ""]
    for heading, group in [("🚚 주요 자재반입", [r for r in rows if r["category"] == "자재반입"]), ("🏗️ 주요 공사·업무", [r for r in rows if r["category"] != "자재반입"])]:
        if not group:
            continue
        lines.extend([heading, "━━━━━━━━━━━━━━━━"])
        current = None
        for row in group:
            if current != row["schedule_date"]:
                if current is not None:
                    lines.append("")
                lines.append(f"■ {korean_date(row['schedule_date'])}")
                current = row["schedule_date"]
            category = "" if row["category"] == "자재반입" else f"[{row['category']}] "
            status = "[완료] " if row["completed"] else ""
            lines.append(f"{status}{schedule_time_label(row)} {category}{row['title']}")
            lines.extend(f"- {label} : {value}" for label, value in detail_items(row))
        lines.append("")
    lines.append("※ 관련 담당자께서는 일정 확인 바랍니다.")
    return "\n".join(lines)


def toggle_notice_selection(schedule_ids):
    selected = set(st.session_state.get("notice_selected_ids", []))
    target = set(schedule_ids)
    if target <= selected:
        selected.difference_update(target)
    else:
        selected.update(target)
    st.session_state["notice_selected_ids"] = sorted(selected)


def filter_calendar_rows(rows, category):
    if category is None:
        return rows
    if category == "기타":
        return [row for row in rows if row["category"] == "기타" or row["category"] not in CATEGORIES]
    return [row for row in rows if row["category"] == category]


def select_calendar_category(category):
    st.session_state["calendar_category"] = category
    # 이전 클릭 결과로 상세 화면에 이동하지 않도록 캘린더 상태를 갱신합니다.
    st.session_state["calendar_generation"] = st.session_state.get("calendar_generation", 0) + 1


def calendar_event(row):
    color = CATEGORY_COLORS.get(row["category"], CATEGORY_COLORS["기타"])
    light_background = color in ("#FFEB3B", "#FFFFFF")
    return {
        "id": str(row["id"]),
        "title": f"{'[완료] ' if row['completed'] else ''}{schedule_time_label(row)} {row['title']}",
        "start": row["schedule_date"],
        "classNames": ["schedule-completed"] if row["completed"] else [],
        "backgroundColor": color,
        "borderColor": "#9E9E9E" if color == "#FFFFFF" else color,
        "textColor": "#212121" if light_background else "#FFFFFF",
    }


@lru_cache(maxsize=8)
def korean_holiday_events(first_year, last_year):
    if holidays is None:
        return []
    korean_holidays = holidays.country_holidays("KR", years=range(first_year, last_year + 1), observed=True, language="ko")
    return [{
        "id": f"holiday_{day.isoformat()}",
        "title": name,
        "start": day.isoformat(),
        "allDay": True,
        "display": "block",
        "editable": False,
        "classNames": ["korean-holiday"],
        "sortOrder": -1,
        "backgroundColor": "transparent",
        "borderColor": "transparent",
        "textColor": "#D32F2F",
    } for day, name in sorted(korean_holidays.items())]


def calendar_css(holiday_events):
    css = """
        .fc-event, .fc-event * { cursor: pointer !important; }
        .schedule-completed .fc-event-title { text-decoration: line-through; }
        .korean-holiday, .korean-holiday * { cursor: default !important; }
        .korean-holiday .fc-event-title { font-weight: 700; }
    """
    # 이벤트가 '더 보기'에 숨겨져도 해당 날짜 숫자는 공휴일 색상을 유지합니다.
    selectors = [f'.fc-daygrid-day[data-date="{date.fromisoformat(event["start"]).isoformat()}"] .fc-daygrid-day-number' for event in holiday_events]
    if selectors:
        css += ",\n".join(selectors) + " { color: #D32F2F !important; font-weight: 700; opacity: 1; }"
    return css


def queue_schedule_autofill(text):
    fields, warnings = parse_schedule_text(text)
    st.session_state["autofill_warnings"] = warnings
    if fields:
        st.session_state["autofill_pending"] = fields
        st.session_state["autofill_count"] = len(fields)
        st.rerun()
    else:
        st.warning("항목을 인식하지 못했습니다. 날짜나 ‘업체:’ 같은 항목명을 함께 입력해주세요.")


def apply_pending_autofill():
    fields = st.session_state.pop("autofill_pending", None)
    if not fields:
        return
    generation = st.session_state.get("new_generation", 0)
    old_prefix = f"new_{generation}"
    draft = dict(st.session_state.get(f"{old_prefix}_draft", {}))
    for key in ["schedule_date", "schedule_time", "end_time", "title", "memo"] + DETAIL_KEYS:
        if f"{old_prefix}_{key}" in st.session_state:
            draft[key] = st.session_state[f"{old_prefix}_{key}"]
    draft.update(fields)
    if fields.get("category") in CATEGORIES:
        st.session_state["new_category"] = fields["category"]
    generation += 1
    st.session_state["new_generation"] = generation
    st.session_state[f"new_{generation}_draft"] = draft


def show_autofill_panel():
    with st.container(border=True):
        st.subheader("✨ 일정 자동입력")
        st.caption("한 번에 일정 1건을 입력하세요. 인식한 항목만 왼쪽에 반영되며, 확인 후 ‘일정 등록’을 눌러 저장합니다.")
        text_tab, image_tab = st.tabs(["📝 텍스트", "🖼️ 캡처 이미지"])
        with text_tab:
            text = st.text_area("일정 내용을 붙여넣으세요", key="autofill_text", height=260, placeholder="날짜: 2026-10-20\n구분: 자재반입\n일정명: 철근 반입\n도착시간: 09:00\n종료시간: 11:30\n업체: 한국철강\n물량: 20톤\n차량대수: 3\n위치: 503동\n규격: HD25\n비고: 현장 도착 전 연락")
            if st.button("📝 텍스트로 자동입력", use_container_width=True, disabled=not text.strip()):
                queue_schedule_autofill(text)
        with image_tab:
            st.caption("캡처를 복사한 뒤 ‘캡처 붙여넣기’를 누르세요. 붙여넣기가 안 되면 이미지 파일을 올릴 수 있습니다.")
            source = st.radio("이미지 입력 방법", ["캡처 붙여넣기", "이미지 파일 업로드"], horizontal=True, key="autofill_image_source")
            image = None
            if source == "캡처 붙여넣기":
                try:
                    from streamlit_paste_button import paste_image_button
                    result = paste_image_button("📋 캡처 붙여넣기", key="schedule_image_paste", errors="raise")
                    image = result.image_data
                except ImportError:
                    st.info("이미지 붙여넣기를 사용하려면 python -m pip install -r requirements.txt 를 실행해주세요.")
            else:
                uploaded = st.file_uploader("일정 캡처 이미지", type=["png", "jpg", "jpeg", "webp"], key="autofill_upload")
                if uploaded is not None:
                    try:
                        from PIL import Image
                        image = Image.open(uploaded)
                    except Exception:
                        st.error("이미지를 열 수 없습니다. PNG 또는 JPG 파일을 다시 선택해주세요.")
            if image is not None:
                st.image(image, caption="인식할 캡처 이미지", use_container_width=True)
                digest = hashlib.sha256(image.tobytes() + str(image.size).encode()).hexdigest()
                retry = st.button("🔄 이미지 다시 인식", use_container_width=True)
                if retry or st.session_state.get("autofill_image_digest") != digest:
                    st.session_state["autofill_image_digest"] = digest
                    try:
                        with st.spinner("이미지의 한글을 읽고 있습니다. 첫 실행은 인식 모델 다운로드로 시간이 걸릴 수 있습니다."):
                            extracted = recognize_schedule_image(image)
                        st.session_state["autofill_ocr_text"] = extracted
                        if extracted.strip():
                            queue_schedule_autofill(extracted)
                        else:
                            st.warning("이미지에서 글자를 찾지 못했습니다. 일정 부분을 크게 잘라서 다시 시도해주세요.")
                    except ImportError:
                        st.error("이미지 인식에 필요한 프로그램이 없습니다. python -m pip install -r requirements.txt 실행 후 다시 시도해주세요.")
                    except Exception:
                        st.error("이미지 인식을 완료하지 못했습니다. 인터넷 연결을 확인한 뒤 다시 인식하거나 텍스트 방법을 사용해주세요.")
            if "autofill_ocr_text" in st.session_state:
                recognized = st.text_area("이미지에서 인식한 글자 (수정 가능)", key="autofill_ocr_text", height=200)
                if st.button("인식한 글자로 다시 자동입력", disabled=not recognized.strip(), use_container_width=True):
                    queue_schedule_autofill(recognized)
        if st.session_state.get("autofill_count"):
            st.success(f"{st.session_state['autofill_count']}개 항목을 자동입력했습니다. 왼쪽 내용을 확인해주세요.")
        for warning in st.session_state.get("autofill_warnings", []):
            st.warning(warning)


def show_direct_kakao(connection):
    from streamlit.components.v1 import declare_component
    root = Path(__file__).resolve().parent
    with st.container(border=True):
        st.subheader("💬 카카오톡 직접 연결")
        st.caption("버튼을 누른 이 PC의 카카오톡을 읽습니다. 사용하는 Windows PC마다 처음 한 번 설치해주세요.")
        try:
            from pc_package import get_pc_installer
            installer = get_pc_installer(root)
        except (ImportError, OSError, ValueError):
            st.info("PC 설치 파일을 준비 중입니다. 관리자가 KakaoPC_Setup.msi와 pc_package.py를 업로드하면 다운로드 버튼이 표시됩니다.")
        else:
            st.download_button(
                "📥 카카오톡 PC 연결 설치 파일 받기 (MSI)",
                data=installer, file_name="KakaoPC_Setup.msi",
                mime="application/x-msi", key="download_kakao_pc",
                use_container_width=True,
            )
        st.caption("처음 사용하는 PC에서는 아래 설명을 1번부터 따라가세요. 이미 설치했다면 바탕화면 Site Schedule Kakao PC 바로가기로 실행하면 됩니다. 설치 파일 버전: 1.2.1.")
        with st.expander("처음 설치하는 분 · 다운로드부터 연결까지 자세히 보기", expanded=True):
            download_tab, warning_tab, install_tab, connect_tab = st.tabs([
                "1. 다운로드", "2. 보안 경고", "3. 설치", "4. 연결",
            ])
            with download_tab:
                st.markdown("**① 위의 ‘설치 파일 받기(MSI)’ 버튼을 누르세요.**")
                st.markdown("**② 브라우저 오른쪽 위 다운로드 표시를 누르세요.** 안 보이면 **Ctrl+J**를 누르면 다운로드 목록이 열립니다.")
                st.markdown("**③ `KakaoPC_Setup.msi`를 찾으세요.** 여러 번 받았다면 파일 이름 뒤에 `(1)`, `(2)`가 붙을 수 있습니다.")
                st.info("파일 옆에 ‘일반적으로 다운로드되지 않습니다’가 나오면 옆의 **2. 보안 경고** 탭을 확인하세요. 경고 없이 완료됐다면 **3. 설치**로 이동하세요.")
                st.caption("Windows 10/11 64비트 PC용입니다. Python과 필요한 라이브러리가 포함되어 있습니다.")
            with warning_tab:
                st.markdown("**‘일반적으로 다운로드되지 않습니다’라는 Edge 경고가 보일 때**")
                st.write("이 설치 파일은 디지털 서명이 없는 새 프로그램이라 경고가 나타날 수 있습니다. 다운로드 출처가 이 일정관리 사이트의 설치 버튼인지 먼저 확인하세요.")
                st.markdown("**① Ctrl+J → 해당 MSI 항목에 마우스를 올리세요.** 항목 옆의 **점 세 개(⋯)** 또는 마우스 오른쪽 메뉴를 여세요. 다운로드 목록 맨 위의 공통 메뉴와는 다릅니다.")
                st.markdown("**② ‘유지’가 보이면 선택하세요.** 추가 확인창에 ‘더 보기 → 계속 유지’가 보이면 내용을 확인한 뒤 선택하세요. 브라우저 버전에 따라 이름과 순서가 다를 수 있습니다.")
                st.markdown("**③ ‘취소 / 삭제’ 확인창만 보이나요?** ‘삭제’ 글자 자체 대신 **삭제 버튼 오른쪽의 작은 화살표(∨)**를 눌러 유지 항목이 있는지 확인하세요.")
                st.markdown("**④ 다운로드가 완료되면 ‘파일 열기’를 누르세요.** 이어서 Windows의 ‘PC를 보호했습니다’가 나타날 수 있습니다. 출처를 확인하고 실행하기로 결정했다면 **추가 정보 → 실행**을 선택할 수 있습니다.")
                st.caption("‘게시자: 알 수 없음’은 이 파일에 정식 디지털 서명이 없다는 뜻입니다. 경고가 없다는 사실만으로 안전성이 보장되지는 않습니다.")
                st.warning("보안 기능 전체를 끄지 마세요. 회사 정책으로 유지·실행이 차단되거나 악성 프로그램이 탐지되면 진행을 멈추고 사내 관리자에게 확인하세요.")
                st.caption("‘자세히 알아보기’를 누르면 Microsoft 도움말 페이지가 열립니다. 설치를 계속하려면 다운로드 목록(Ctrl+J)으로 돌아오세요.")
            with install_tab:
                st.markdown("**① 받은 `KakaoPC_Setup.msi`를 더블클릭하세요.** 파일을 못 찾겠다면 Ctrl+J에서 폴더 열기를 누르거나 파일 탐색기의 다운로드 폴더를 확인하세요.")
                st.markdown("**② Windows Installer가 필요한 파일을 설치합니다.** 설치 창이 닫힐 때까지 기다리세요.")
                st.markdown("**③ 바탕화면의 Site Schedule Kakao PC 바로가기를 더블클릭하세요.** MSI 설치 후 연결 프로그램을 직접 실행하는 단계입니다.")
                st.markdown("**④ ‘연결 실행 중입니다’ 표시와 연결코드 페이지가 나오는지 확인하세요.** 연결코드 페이지 주소는 이 PC의 `http://127.0.0.1:8765/`입니다.")
                st.info("이 설치 파일에는 Python이 포함되어 있습니다. 별도로 Python을 설치하거나 명령어를 입력할 필요가 없습니다.")
            with connect_tab:
                st.markdown("**① 연결코드 페이지의 코드를 복사하세요.** 페이지가 자동으로 안 열리면 아래 **이 컴퓨터의 연결코드 보기** 링크를 누르세요.")
                st.markdown("**② 이 일정관리 사이트로 돌아와 아래 ‘PC 연결코드’ 칸에 붙여넣으세요.** 다른 사람의 코드가 아닌 현재 PC의 코드를 사용하세요.")
                st.markdown("**③ PC 카카오톡에 로그인하고 ‘일정공유방☆’을 별도 대화창으로 열어두세요.** 카카오톡의 다른 팝업이나 대화상자는 닫아주세요.")
                st.markdown("**④ 아래에서 읽을 메시지의 시작일과 종료일을 고른 뒤 ‘카카오톡 일정 가져와서 등록’을 누르세요.** 처음 브라우저에서 로컬 네트워크 접근 권한을 물으면 내용을 확인하세요. 이 기능은 브라우저와 현재 PC의 연결 프로그램 사이 통신을 사용합니다.")
                st.markdown("**⑤ 등록 결과와 캘린더의 일정 내용을 확인하세요.** 정보가 부족한 메시지는 확인이 필요한 항목으로 나올 수 있습니다.")
                st.caption("연결코드는 다른 사람과 공유하지 마세요. 이 기능은 가져오기 버튼을 눌렀을 때 지정한 방의 텍스트를 읽으며, 메시지를 보내지 않습니다.")
        with st.expander("설치 후 사용 · 연결이 안 될 때"):
            st.markdown("**다음 날 다시 사용할 때**: 바탕화면의 **Site Schedule Kakao PC**를 실행하고 카카오톡 대화방을 열어주세요. 매번 다시 설치할 필요는 없습니다.")
            st.markdown("**검은 연결 창을 닫아도 되나요?**: 이 버전은 창을 닫으면 연결이 종료됩니다. 카카오톡 가져오기를 사용하는 동안 창을 열어두세요. 다시 연결하려면 바탕화면 바로가기를 실행하세요.")
            st.markdown("**127.0.0.1 연결 거부**: 이 PC의 연결 프로그램이 꺼져 있을 수 있습니다. 바탕화면 바로가기를 실행하고 ‘연결 실행 중’ 표시를 확인한 다음 연결코드 페이지를 다시 여세요.")
            st.markdown("**PC 연결코드 오류**: 현재 PC의 연결코드 페이지에서 다시 복사하세요. 다른 PC의 코드는 사용할 수 없습니다.")
            st.markdown("**이미 실행 중 / 연결 시작 실패**: 예전 `start_kakao_pc.bat` 실행 창이 남아 있다면 종료한 뒤 새 바로가기로 실행하세요.")
            st.markdown("**설치·실행 오류**: 오류 문구가 보이는 화면을 담당자에게 전달하세요. 연결코드와 비밀번호는 가려주세요. 실행 기록은 파일 탐색기 주소창에 아래 경로를 넣으면 확인할 수 있습니다.")
            st.code(r"%LOCALAPPDATA%\SiteScheduleKakaoPC\data\bridge-startup.log", language="text")
            st.caption("일정 조회·직접 등록에는 PC 연결 프로그램이 필요하지 않습니다. 카카오톡 가져오기 기능을 사용하는 PC에서만 실행하면 됩니다.")
        component_path = root / "kakao_component"
        if not component_path.is_dir():
            st.error("kakao_component 폴더를 코드와 함께 업로드해주세요.")
            return
        reader_component = declare_component("kakao_direct_reader", path=str(component_path))
        local_token = ""
        token_path = root / ".local-agent" / "pairing-token.txt"
        if os.name == "nt" and token_path.exists():
            local_token = token_path.read_text(encoding="ascii").strip()
        today_korea = datetime.now(timezone(timedelta(hours=9))).date().isoformat()
        value = reader_component(
            today=today_korea, local_token=local_token,
            save_ack=st.session_state.get("kakao_direct_ack"),
            key="kakao_direct_reader_save_v2", default=None,
        )
        st.caption("PC에서 인식한 뒤 공용 DB 저장 결과를 확인합니다. 등록 후보 수만으로 저장 완료를 판단하지 마세요.")
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (ValueError, TypeError):
                st.error("인식 결과를 사이트에 전달하지 못했습니다. 다시 가져오기를 눌러주세요.")
                value = None
        if value is not None and not isinstance(value, dict):
            st.error("인식 결과의 형식을 확인하지 못했습니다. 다시 가져오기를 눌러주세요.")
        if isinstance(value, dict) and value.get("request_id") != st.session_state.get("kakao_direct_seen"):
            request_id = value.get("request_id")
            try:
                ack, result, pending = save_direct_read(connection, value)
            except (ValueError, TypeError) as error:
                ack = {"request_id": request_id, "status": "error", "message": str(error)}
            except DATABASE_ERRORS:
                ack = {"request_id": request_id, "status": "error", "message": "공용 DB 저장을 확인하지 못했습니다. 연결 상태를 확인하고 저장 결과 다시 확인을 눌러주세요."}
            else:
                st.session_state["kakao_direct_result"] = result
                st.session_state["kakao_direct_pending"] = pending
            st.session_state["kakao_direct_ack"] = ack
            st.session_state["kakao_direct_seen"] = request_id
            st.rerun()  # Send the committed save result back to the PC component.
        ack = st.session_state.get("kakao_direct_ack")
        result = st.session_state.get("kakao_direct_result")
        if ack and ack.get("status") == "error":
            st.error(ack["message"])
        elif ack and ack.get("status") == "saved" and result is not None:
            st.success(f"DB 저장 확인: 새 등록 {ack['inserted']}건 · 중복 제외 {ack['skipped']}건")
            ids = list(dict.fromkeys(result["inserted"] + result["skipped"]))
            if ids:
                imported_rows = connection.execute(
                    "SELECT id,schedule_date,title,deleted_at FROM schedules WHERE id IN (" + ",".join("?" for _ in ids) + ") ORDER BY schedule_date,id", ids,
                ).fetchall()
                inserted_ids = set(result["inserted"])
                st.dataframe([
                    {"일정 날짜": row["schedule_date"], "일정명": row["title"], "결과": "새 등록" if row["id"] in inserted_ids else ("중복 · 휴지통에 있음" if row["deleted_at"] else "중복 · 이미 등록됨")}
                    for row in imported_rows
                ], hide_index=True, use_container_width=True)
                st.caption("캘린더에는 메시지를 보낸 날짜가 아니라 위 일정 날짜에 표시됩니다. 휴지통에 있는 일정은 자동 복원하지 않습니다.")
                if st.button("등록된 일정 보기", key="kakao_show_saved"):
                    st.session_state["pending_menu"] = "📋 일정관리"
                    st.rerun()
        pending = st.session_state.get("kakao_direct_pending", [])
        if pending:
            with st.expander(f"확인이 필요한 메시지 {len(pending)}건", expanded=True):
                for index, item in enumerate(pending):
                    st.text(str(item.get("text", ""))[:20_000])
                    st.caption(" / ".join(map(str, item.get("reasons", []))))
                    if st.button("왼쪽 입력란에서 확인하기", key=f"kakao_pending_{index}"):
                        st.session_state["pending_menu"] = "➕ 일정등록"
                        queue_schedule_autofill(str(item.get("text", "")))


def show_notice_builder(connection):
    preview_panel, list_panel = st.columns([3, 2], gap="large")
    with preview_panel:
        st.subheader("📢 카카오톡 공지문 만들기")
        st.caption("기간을 지정한 뒤 오른쪽의 날짜나 일정을 누르면 공지문을 미리 볼 수 있습니다.")
        start_col, end_col = st.columns(2)
        start = start_col.date_input("시작일", value=date.today(), key="notice_start")
        end = end_col.date_input("종료일", value=date.today(), key="notice_end")
        legacy_categories = sorted({row["category"] for row in get_schedules(connection) if row["category"] and row["category"] not in CATEGORIES})
        categories = st.multiselect("일정 구분", CATEGORIES + legacy_categories, default=CATEGORIES, key="notice_categories")
        valid = start <= end and bool(categories)
        rows = [row for row in get_schedules(connection, start, end) if row["category"] in categories] if valid else []
        filters = (str(start), str(end), tuple(sorted(categories)))
        if st.session_state.get("notice_filters") != filters:
            st.session_state["notice_filters"] = filters
            st.session_state.pop("notice_selected_ids", None)
        if start > end:
            st.error("종료일은 시작일보다 빠를 수 없습니다.")
        elif not categories:
            st.warning("일정 구분을 1개 이상 선택해주세요.")
        if st.button("📝 기간 전체 공지문 생성", type="primary", use_container_width=True, disabled=not rows):
            st.session_state["notice_selected_ids"] = [row["id"] for row in rows]
        preview = st.container()

    with list_panel:
        with st.container(border=True):
            st.subheader(f"📅 기간 내 일정 · {len(rows)}건")
            st.caption("여러 일정을 눌러 공지문에 함께 담으세요. 다시 누르면 선택이 해제됩니다.")
            available_ids = {row["id"] for row in rows}
            selected_ids = set(st.session_state.get("notice_selected_ids", [])) & available_ids
            st.session_state["notice_selected_ids"] = sorted(selected_ids)
            st.caption(f"선택한 일정: {len(selected_ids)}건")
            if st.button("선택 전체 해제", disabled=not selected_ids, use_container_width=True):
                st.session_state["notice_selected_ids"] = []
                selected_ids = set()
            if not rows:
                st.info("선택한 기간과 구분에 해당하는 일정이 없습니다." if valid else "왼쪽에서 기간과 일정 구분을 확인해주세요.")
            days = {}
            for row in rows:
                days.setdefault(row["schedule_date"], []).append(row)
            for day, day_rows in days.items():
                st.markdown(f"**{korean_date(day)} · {len(day_rows)}건**")
                day_ids = [row["id"] for row in day_rows]
                day_selected = set(day_ids) <= selected_ids
                st.button("이 날짜 선택 해제" if day_selected else "이 날짜 전체 선택", key=f"notice_day_{day}", use_container_width=True, type="primary" if day_selected else "secondary", on_click=toggle_notice_selection, args=(day_ids,))
                for row in day_rows:
                    status = "[완료] " if row["completed"] else ""
                    is_selected = row["id"] in selected_ids
                    label = f"{'☑ ' if is_selected else '☐ '}{status}{schedule_time_label(row)} · [{row['category']}] {row['title']}"
                    st.button(label, key=f"notice_event_{row['id']}", use_container_width=True, type="primary" if is_selected else "secondary", on_click=toggle_notice_selection, args=([row["id"]],))
                st.divider()

    with preview:
        st.subheader("📝 공지문 미리보기")
        selected_ids = st.session_state.get("notice_selected_ids", [])
        selected_rows = [row for row in rows if row["id"] in selected_ids]
        if selected_rows:
            selected_start = min(row["schedule_date"] for row in selected_rows)
            selected_end = max(row["schedule_date"] for row in selected_rows)
            is_all = len(selected_rows) == len(rows)
            text = make_notice(selected_rows, start if is_all else selected_start, end if is_all else selected_end)
            st.text_area("아래 내용을 복사하여 카카오톡에 붙여넣으세요.", value=text, height=550)
            st.success(f"총 {len(selected_rows)}건의 일정을 반영했습니다.")
        else:
            st.info("오른쪽에서 일정을 클릭하거나 ‘기간 전체 공지문 생성’을 눌러주세요.")


@st.fragment(run_every="10s")
def receive_kakao_schedules():
    # No remote connection is needed when there are no local inbox files.
    inbox = KAKAO_ROOT / ".kakao-inbox"
    if not inbox.is_dir() or not any(inbox.glob("*.json")):
        return
    connection = None
    try:
        connection = open_database()
        results = drain_inbox(connection)
    except DATABASE_ERRORS:
        st.caption("일정 저장소가 사용 중입니다. 잠시 후 자동으로 다시 확인합니다.")
        return
    finally:
        if connection is not None:
            connection.close()
    inserted = sum(len(result.get("inserted", [])) for result in results)
    skipped = sum(len(result.get("skipped", [])) for result in results)
    if results:
        st.session_state["kakao_last_results"] = results
    if inserted:
        st.session_state["flash"] = f"카카오톡 일정 {inserted}건 자동등록 · 중복 {skipped}건 제외"
        st.rerun()


def show_kakao_sync(connection):
    st.subheader("🔄 카카오톡 일정 가져오기")
    st.info(f"수집 대상: {KAKAO_ROOM_NAME} · 텍스트 메시지만 · 사진·첨부파일 제외")
    show_direct_kakao(connection)
    st.caption("이 컴퓨터에서 전달받은 일정은 화면이 열려 있는 동안 10초마다 확인합니다. 카카오톡을 직접 상시 감시하는 기능은 아닙니다.")
    st.markdown("**온라인 프로그램에 반영하기**")
    st.caption("컴퓨터 유즈가 만든 일정 파일을 올리면 바로 등록됩니다. 이미 등록되었거나 휴지통에 있는 일정은 다시 만들지 않습니다. 확인되지 않은 값은 비워둡니다.")
    uploaded = st.file_uploader("카카오톡 일정 파일 (.json) — 업로드 즉시 자동등록", type=["json"], key="kakao_batch_upload")
    if uploaded is not None:
        payload = uploaded.getvalue()
        digest = hashlib.sha256(payload).hexdigest()
        if st.session_state.get("kakao_uploaded_digest") != digest:
            try:
                result = import_batch(connection, payload)
            except (ValueError, UnicodeError):
                st.error("일정 파일의 형식·날짜·시간을 확인해주세요. 이 파일의 일정은 등록하지 않았습니다.")
            except DATABASE_ERRORS:
                st.error("일정 저장소가 사용 중입니다. 잠시 후 다시 시도해주세요.")
            else:
                st.session_state["kakao_uploaded_digest"] = digest
                st.session_state["kakao_upload_result"] = result
        result = st.session_state.get("kakao_upload_result") if st.session_state.get("kakao_uploaded_digest") == digest else None
        if result:
            st.success(f"등록 {len(result['inserted'])}건 · 중복 제외 {len(result['skipped'])}건")
    for result in st.session_state.get("kakao_last_results", []):
        if "error" in result:
            st.warning("전달받은 일정 파일의 형식이 맞지 않습니다. 원본은 수신함에 보관했습니다.")
        else:
            st.caption(f"최근 수신: 등록 {len(result['inserted'])}건 · 중복 제외 {len(result['skipped'])}건")
    st.caption("로컬·온라인은 현재 별도 저장소입니다. 같은 파일을 양쪽에 전달하면 같은 일정을 등록할 수 있으며, 이후 수정사항까지 계속 공유하려면 공용 DB 연결이 필요합니다.")


def main():
    # 기존 버전과 동일한 경로. 기존 프로젝트 폴더에서 실행하세요.
    conn = None
    try:
        conn = open_database()
        migrate_database(conn)
        if getattr(conn, "is_postgres", False):
            st.caption("☁️ 공용 DB 연결됨 · 등록·수정 즉시 저장 · 서버 재시작 후에도 유지")
        else:
            st.warning("로컬 SQLite 모드입니다. 클라우드에서는 데이터 보존을 보장하지 않습니다.")
        receive_kakao_schedules()
        st.title("🏗️ 현장 일정관리")
        st.caption("주요 자재반입 및 현장 업무일정을 한 곳에서 관리합니다.")
        if "pending_menu" in st.session_state:
            st.session_state["main_menu"] = st.session_state.pop("pending_menu")
        menu = st.radio("메뉴", ["📅 캘린더", "➕ 일정등록", "📋 일정관리", "📢 카카오톡 공지", "🔄 카카오톡 연동"], horizontal=True, label_visibility="collapsed", key="main_menu")
        st.divider()
        if "flash" in st.session_state:
            st.success(st.session_state.pop("flash"))
        if menu == "📅 캘린더":
            rows = get_schedules(conn)
            for col, label, count in zip(st.columns(3), ["오늘 일정", "전체 등록 일정", "자재반입 일정"], [sum(r["schedule_date"] == date.today().isoformat() for r in rows), len(rows), sum(r["category"] == "자재반입" for r in rows)]):
                col.metric(label, f"{count}건")
            st.subheader("📅 월간 일정")
            selected_category = st.session_state.get("calendar_category")
            filters = [(None, "📅 전체"), ("자재반입", "🔵 자재반입"), ("타설", "🔴 타설"), ("회의", "🟡 회의"), ("기타", "⚪ 기타")]
            for column, (category, label) in zip(st.columns(5), filters):
                column.button(label, key=f"calendar_filter_{category or 'all'}", type="primary" if selected_category == category else "secondary", use_container_width=True, on_click=select_calendar_category, args=(category,))
            visible_rows = filter_calendar_rows(rows, selected_category)
            st.caption(f"{selected_category or '전체'} 일정 · {len(visible_rows)}건" + (" (기존 구분 포함)" if selected_category == "기타" else ""))
            if not visible_rows:
                st.info("해당 구분으로 등록된 일정이 없습니다.")
            events = [calendar_event(row) for row in visible_rows]
            current_year = date.today().year
            schedule_years = [date.fromisoformat(row["schedule_date"]).year for row in rows]
            first_year = max(1900, min([current_year - 10] + schedule_years) - 1)
            last_year = min(2050, max([current_year + 10] + schedule_years) + 1)
            holiday_events = korean_holiday_events(first_year, last_year)
            events += holiday_events
            if holidays is None:
                st.warning("공휴일 표시를 위해 명령창에서 python -m pip install -r requirements.txt 를 실행한 뒤 프로그램을 다시 실행해주세요.")
            else:
                st.caption(f"공휴일·대체공휴일은 붉은 글씨로 표시됩니다. ({first_year}~{last_year}년, 임시공휴일은 라이브러리 반영 기준)")
            result = calendar(events=events, options={"initialView": "dayGridMonth", "headerToolbar": {"left": "prev,next today", "center": "title", "right": ""}, "locale": "ko", "height": 700, "dayMaxEvents": True, "eventOrder": "sortOrder,start,title", "buttonText": {"today": "오늘"}}, custom_css=calendar_css(holiday_events), key=f"main_calendar_{st.session_state.get('calendar_generation', 0)}")
            if result and result.get("eventClick"):
                clicked = next((r for r in visible_rows if str(r["id"]) == str(result["eventClick"]["event"]["id"])), None)
                if clicked:
                    st.session_state["selected_schedule_id"] = clicked["id"]
                    st.session_state["pending_menu"] = "📋 일정관리"
                    st.session_state["calendar_generation"] = st.session_state.get("calendar_generation", 0) + 1
                    st.rerun()
        elif menu == "➕ 일정등록":
            apply_pending_autofill()
            form_panel, assist_panel = st.columns([3, 2], gap="large")
            with assist_panel:
                show_autofill_panel()
                show_direct_kakao(conn)
            with form_panel:
                st.subheader("➕ 새 일정 등록")
                category = st.selectbox("구분", CATEGORIES, key="new_category")
                # 새 등록마다 키를 교체하여 성공 후에만 초기화.
                prefix = f"new_{st.session_state.get('new_generation', 0)}"
                submitted, values = schedule_form(prefix, category)
                if submitted:
                    if not values["title"].strip():
                        st.error("일정명을 입력해주세요.")
                    else:
                        save_schedule(conn, values)
                        st.session_state["new_generation"] = st.session_state.get("new_generation", 0) + 1
                        st.session_state.pop("autofill_count", None)
                        st.session_state.pop("autofill_warnings", None)
                        st.session_state["flash"] = "✅ 일정이 등록되었습니다."
                        st.rerun()
        elif menu == "📋 일정관리":
            schedule_panel, trash_panel = st.columns([3, 2], gap="large")
            with schedule_panel:
                st.subheader("📋 등록된 일정")
                rows = get_schedules(conn)
                selected_id = st.session_state.get("selected_schedule_id")
                selected = next((row for row in rows if row["id"] == selected_id), None)
                if selected:
                    st.info("📌 캘린더에서 선택한 일정을 맨 위에 표시했습니다.")
                    rows = [selected] + [row for row in rows if row["id"] != selected_id]
                    if st.button("전체 일정 순서로 보기"):
                        st.session_state.pop("selected_schedule_id", None)
                        st.rerun()
                if not rows:
                    st.info("등록된 일정이 없습니다.")
                for row in rows:
                    with st.container(border=True):
                        if row["id"] == selected_id:
                            st.caption("📌 선택한 일정 상세")
                        show_schedule(row)
                        complete, left, right = st.columns([2, 1, 1])
                        with complete:
                            completion_button(conn, row, "manage")
                        if left.button("✏️ 수정", key=f"edit_{row['id']}"):
                            st.session_state["edit_id"] = row["id"]
                            st.session_state["edit_generation"] = st.session_state.get("edit_generation", 0) + 1
                            st.rerun()
                        if right.button("🗑️ 삭제", key=f"delete_{row['id']}"):
                            delete_schedule(conn, row["id"])
                            st.session_state["flash"] = "일정을 삭제했습니다. 오른쪽 ‘휴지통’에서 삭제 취소할 수 있습니다."
                            if st.session_state.get("edit_id") == row["id"]:
                                st.session_state.pop("edit_id", None)
                            st.rerun()
                saved = next((r for r in rows if r["id"] == st.session_state.get("edit_id")), None)
                if saved:
                    st.divider()
                    st.subheader("✏️ 일정 수정")
                    prefix = f"edit_{saved['id']}_{st.session_state.get('edit_generation', 0)}"
                    options = CATEGORIES if saved["category"] in CATEGORIES else [saved["category"]] + CATEGORIES
                    category = st.selectbox("구분", options, index=options.index(saved["category"]), key=f"{prefix}_category")
                    submitted, values = schedule_form(prefix, category, saved)
                    if submitted:
                        if not values["title"].strip():
                            st.error("일정명을 입력해주세요.")
                        else:
                            save_schedule(conn, values, saved["id"])
                            st.session_state.pop("edit_id", None)
                            st.session_state["flash"] = "✅ 수정사항이 저장되었습니다."
                            st.rerun()
            with trash_panel:
                show_trash(conn)
        elif menu == "🔄 카카오톡 연동":
            show_kakao_sync(conn)
        else:
            show_notice_builder(conn)
    except DATABASE_ERRORS:
        st.error("DB 연결 또는 저장에 실패했습니다. 이번 변경은 저장되지 않았습니다. 입력 내용을 유지한 채 설정을 확인하고 다시 시도하세요.")
        st.info("온라인 사용: Manage app → Settings → Secrets에 Supabase [storage] 설정이 필요합니다. 연결 실패 시 빈 DB로 전환하지 않습니다.")
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    main()
