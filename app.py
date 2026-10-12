import sqlite3
from datetime import date, time

import streamlit as st
from streamlit_calendar import calendar

st.set_page_config(page_title="현장 일정관리", page_icon="🏗️", layout="wide")

CATEGORIES = ["자재반입", "주요공정", "검측/감리", "대외업무", "회의", "기타"]
CATEGORY_COLORS = dict(zip(CATEGORIES, ["#E74C3C", "#F39C12", "#3498DB", "#27AE60", "#8E44AD", "#7F8C8D"]))
# A안: 협의기관은 company, 장소/위치는 location을 재사용합니다.
FIELDS = {
    "자재반입": [("company", "업체"), ("quantity", "물량"), ("location", "위치"), ("specification", "규격")],
    "주요공정": [("company", "업체"), ("quantity", "물량"), ("location", "위치")],
    "검측/감리": [("company", "업체"), ("location", "위치"), ("person", "담당자")],
    "대외업무": [("company", "협의기관"), ("location", "장소"), ("person", "담당자")],
    "회의": [("participants", "참석대상"), ("location", "장소")],
    "기타": [("company", "업체"), ("location", "장소")],
}
DETAIL_KEYS = ["company", "quantity", "location", "specification", "person", "participants"]
DB_COLUMNS = ["schedule_date", "schedule_time", "category", "title", "company", "quantity", "location", "memo", "specification", "person", "participants"]


def migrate_database(connection):
    # 기존 행을 변경하지 않고 동일 트랜잭션에서 부족한 열만 추가합니다.
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute("""CREATE TABLE IF NOT EXISTS schedules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            schedule_date TEXT NOT NULL, schedule_time TEXT, category TEXT,
            title TEXT NOT NULL, company TEXT, quantity TEXT, location TEXT, memo TEXT
        )""")
        existing = {row[1] for row in connection.execute("PRAGMA table_info(schedules)")}
        for column in ("specification", "person", "participants"):
            if column not in existing:
                connection.execute(f"ALTER TABLE schedules ADD COLUMN {column} TEXT")
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def get_schedules(connection, start=None, end=None):
    sql = "SELECT id, " + ", ".join(DB_COLUMNS) + " FROM schedules"
    params = ()
    if start is not None:
        sql += " WHERE schedule_date BETWEEN ? AND ?"
        params = (str(start), str(end))
    return connection.execute(sql + " ORDER BY schedule_date, schedule_time, id", params).fetchall()


def detail_items(row):
    fields = FIELDS.get(row["category"], FIELDS["기타"])
    items = [(label, row[key]) for key, label in fields if row[key]]
    # 구버전 및 구분 변경으로 현재 폼에 없는 값도 표시해 정보 누락 방지.
    used = {key for key, label in fields}
    labels = dict(zip(DETAIL_KEYS, ["업체(기존)", "물량(기존)", "장소(기존)", "규격(기존)", "담당자(기존)", "참석대상(기존)"]))
    items.extend((labels[key], row[key]) for key in DETAIL_KEYS if key not in used and row[key])
    if row["memo"]:
        items.append(("비고", row["memo"]))
    return items


def show_schedule(row):
    st.markdown(f"### {row['schedule_date']} · {row['schedule_time'] or ''}")
    st.markdown(f"**[{row['category']}] {row['title']}**")
    for label, value in detail_items(row):
        st.write(f"{label} : {value}")


def schedule_form(prefix, category, saved=None):
    saved = dict(saved) if saved is not None else {}
    try:
        saved_date = date.fromisoformat(saved.get("schedule_date") or date.today().isoformat())
    except ValueError:
        saved_date = date.today()
    try:
        saved_time = time.fromisoformat(saved.get("schedule_time") or "09:00")
    except ValueError:
        saved_time = time(9, 0)
    # 카테고리 변경 시 입력 중인 공통값을 유지합니다.
    widget_keys = ["schedule_date", "schedule_time", "title", "memo"] + DETAIL_KEYS
    draft = st.session_state.get(f"{prefix}_draft", {})
    for key in widget_keys:
        state_key = f"{prefix}_{key}"
        if state_key not in st.session_state:
            st.session_state[state_key] = draft.get(key, saved_date if key == "schedule_date" else saved_time if key == "schedule_time" else saved.get(key) or "")
    values = {key: st.session_state[f"{prefix}_{key}"] for key in DETAIL_KEYS}
    # 조건부로 숨겨지는 위젯의 값을 Streamlit 정리 이후에도 보관.
    with st.form(f"{prefix}_form"):
        left, right = st.columns(2)
        with left:
            values["schedule_date"] = st.date_input("날짜", key=f"{prefix}_schedule_date")
        with right:
            values["schedule_time"] = st.time_input("시간", key=f"{prefix}_schedule_time")
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
    data["schedule_time"] = data["schedule_time"].strftime("%H:%M")
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
            lines.append(f"{row['schedule_time'] or ''} {category}{row['title']}")
            lines.extend(f"- {label} : {value}" for label, value in detail_items(row))
        lines.append("")
    lines.append("※ 관련 담당자께서는 일정 확인 바랍니다.")
    return "\n".join(lines)


def main():
    # 기존 버전과 동일한 경로. 기존 프로젝트 폴더에서 실행하세요.
    conn = sqlite3.connect("schedule.db", timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        migrate_database(conn)
        st.title("🏗️ 현장 일정관리")
        st.caption("주요 자재반입 및 현장 업무일정을 한 곳에서 관리합니다.")
        menu = st.radio("메뉴", ["📅 캘린더", "➕ 일정등록", "📋 일정관리", "📢 카카오톡 공지"], horizontal=True, label_visibility="collapsed")
        st.divider()
        if "flash" in st.session_state:
            st.success(st.session_state.pop("flash"))
        if menu == "📅 캘린더":
            rows = get_schedules(conn)
            for col, label, count in zip(st.columns(3), ["오늘 일정", "전체 등록 일정", "자재반입 일정"], [sum(r["schedule_date"] == date.today().isoformat() for r in rows), len(rows), sum(r["category"] == "자재반입" for r in rows)]):
                col.metric(label, f"{count}건")
            st.subheader("📅 월간 일정")
            events = [{"id": str(r["id"]), "title": f"{r['schedule_time'] or ''} {r['title']}", "start": r["schedule_date"], "backgroundColor": CATEGORY_COLORS.get(r["category"], "#7F8C8D"), "borderColor": CATEGORY_COLORS.get(r["category"], "#7F8C8D")} for r in rows]
            result = calendar(events=events, options={"initialView": "dayGridMonth", "headerToolbar": {"left": "prev,next today", "center": "title", "right": ""}, "locale": "ko", "height": 700, "dayMaxEvents": True, "buttonText": {"today": "오늘"}}, key="main_calendar")
            if result and result.get("eventClick"):
                clicked = next((r for r in rows if str(r["id"]) == str(result["eventClick"]["event"]["id"])), None)
                if clicked:
                    st.subheader("📌 선택한 일정")
                    with st.container(border=True):
                        show_schedule(clicked)
        elif menu == "➕ 일정등록":
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
                    st.session_state["flash"] = "✅ 일정이 등록되었습니다."
                    st.rerun()
        elif menu == "📋 일정관리":
            st.subheader("📋 등록된 일정")
            rows = get_schedules(conn)
            if not rows:
                st.info("등록된 일정이 없습니다.")
            for row in rows:
                with st.container(border=True):
                    show_schedule(row)
                    left, right, _ = st.columns([1, 1, 6])
                    if left.button("✏️ 수정", key=f"edit_{row['id']}"):
                        st.session_state["edit_id"] = row["id"]
                        st.session_state["edit_generation"] = st.session_state.get("edit_generation", 0) + 1
                        st.rerun()
                    if right.button("🗑️ 삭제", key=f"delete_{row['id']}"):
                        with conn:
                            conn.execute("DELETE FROM schedules WHERE id = ?", (row["id"],))
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
        else:
            st.subheader("📢 카카오톡 공지문 만들기")
            st.caption("기간과 일정 구분을 선택하면 카카오톡용 공지문을 생성합니다.")
            left, right = st.columns(2)
            start = left.date_input("시작일", value=date.today(), key="notice_start")
            end = right.date_input("종료일", value=date.today(), key="notice_end")
            categories = st.multiselect("일정 구분", CATEGORIES, default=CATEGORIES[:3])
            if st.button("📝 공지문 생성", type="primary", use_container_width=True):
                if start > end:
                    st.error("종료일은 시작일보다 빠를 수 없습니다.")
                elif not categories:
                    st.warning("공지에 포함할 일정 구분을 1개 이상 선택해주세요.")
                else:
                    rows = [r for r in get_schedules(conn, start, end) if r["category"] in categories]
                    if rows:
                        st.session_state["notice_text"] = make_notice(rows, start, end)
                        st.session_state["notice_count"] = len(rows)
                    else:
                        st.session_state.pop("notice_text", None)
                        st.warning("선택한 기간과 구분에 해당하는 일정이 없습니다.")
            if "notice_text" in st.session_state:
                st.subheader("📝 생성된 공지문")
                st.text_area("아래 내용을 복사하여 카카오톡에 붙여넣으세요.", value=st.session_state["notice_text"], height=550)
                st.success(f"총 {st.session_state.get('notice_count', 0)}건의 일정을 반영했습니다.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
