"""Local Korean schedule parsing and screenshot OCR (no paid API)."""

import re
import unicodedata
from datetime import date, time, timedelta
from functools import lru_cache
from pathlib import Path
from threading import Lock


ALIASES = {
    "schedule_date": ["일정날짜", "작업일", "반입일", "타설일", "날짜", "일자", "일시"],
    "schedule_time": ["도착시간", "도착예정시간", "시작시간", "반입시간", "도착", "시작", "시간"],
    "end_time": ["종료시간", "완료시간", "종료예정시간", "종료", "완료"],
    "category": ["일정구분", "구분", "분류"],
    "title": ["일정명", "작업명", "작업내용", "제목", "내용"],
    "company": ["협의기관", "업체명", "업체", "회사"],
    "quantity": ["물량", "수량"],
    "vehicle_count": ["차량대수", "차량수", "대수", "차량"],
    "location": ["작업위치", "반입위치", "위치", "장소"],
    "specification": ["규격", "사양"],
    "person": ["담당자", "담당"],
    "participants": ["참석대상", "참석자", "참석"],
    "memo": ["비고", "메모", "참고"],
}
LABELS = {alias: key for key, aliases in ALIASES.items() for alias in aliases}
LABEL_PATTERN = "|".join(re.escape(alias) for alias in sorted(LABELS, key=len, reverse=True))
TIME_PATTERN = r"(?<!\d)(?:(오전|오후)\s*)?(\d{1,2})\s*(?::\s*(\d{2})(?!\d)|시(?:\s*(\d{1,2})\s*분)?)"


def parse_time(value):
    match = re.search(TIME_PATTERN, value)
    if not match:
        return None
    period, hour, minute, korean_minute = match.groups()
    hour, minute = int(hour), int(minute or korean_minute or 0)
    if period:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if period == "오후" else 0)
    try:
        return time(hour, minute)
    except ValueError:
        return None


def parse_schedule_text(text, today=None):
    """Return only recognized fields; leave unspecified form fields untouched."""
    today = today or date.today()
    text = unicodedata.normalize("NFKC", text).replace("\r\n", "\n").strip()
    if not text:
        return {}, ["일정 내용을 입력해주세요."]
    lines = [line.strip(" •·■-\t") for line in text.splitlines() if line.strip()]
    # OCR may put a label and its value on separate lines.
    joined = []
    index = 0
    while index < len(lines):
        label = re.sub(r"\s", "", lines[index].rstrip(":"))
        if label in LABELS and index + 1 < len(lines) and re.sub(r"\s", "", lines[index + 1].rstrip(":")) not in LABELS:
            joined.append(label + ": " + lines[index + 1])
            index += 2
        else:
            joined.append(lines[index])
            index += 1
    normalized = "\n".join(joined)
    matches = list(re.finditer(rf"(?<!\S)({LABEL_PATTERN})\s*[:：]\s*", normalized))
    labeled = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(normalized)
        value = normalized[match.end():end].strip(" \n,;|")
        if value:
            labeled[LABELS[match.group(1)]] = value
    # Also accept '업체 한국철강' on its own line.
    for line in joined:
        match = re.fullmatch(rf"({LABEL_PATTERN})\s+(.+)", line)
        if match:
            labeled.setdefault(LABELS[match[1]], match[2].strip())
    fields = {key: value for key, value in labeled.items() if key not in ("schedule_date", "schedule_time", "end_time", "category")}
    warnings = []
    date_source = labeled.get("schedule_date", text)
    date_matches = list(re.finditer(r"(?<!\d)(?:(20\d{2})\s*(?:[./-]|년)\s*)?(\d{1,2})\s*(?:[./-]|월)\s*(\d{1,2})(?:\s*일)?(?!\d)", date_source))
    if date_matches:
        match = date_matches[0]
        try:
            fields["schedule_date"] = date(int(match[1] or today.year), int(match[2]), int(match[3]))
        except ValueError:
            warnings.append("날짜를 확인해주세요. 올바르지 않은 날짜는 자동 입력하지 않았습니다.")
        if len(date_matches) > 1:
            warnings.append("여러 날짜가 보입니다. 첫 번째 날짜를 사용했으니 확인해주세요.")
    else:
        for word, offset in [("모레", 2), ("내일", 1), ("오늘", 0)]:
            if word in date_source:
                fields["schedule_date"] = today + timedelta(days=offset)
                break
    for key in ("schedule_time", "end_time"):
        if key in labeled:
            parsed = parse_time(labeled[key])
            if parsed is not None:
                fields[key] = parsed
            else:
                warnings.append(f"{'도착' if key == 'schedule_time' else '종료'}시간을 확인해주세요.")
    time_source = labeled.get("schedule_time", labeled.get("schedule_date", text))
    times = [parse_time(match[0]) for match in re.finditer(TIME_PATTERN, time_source)]
    times = [value for value in times if value is not None]
    if times:
        if "schedule_time" not in labeled and "end_time" not in labeled:
            fields.setdefault("schedule_time", times[0])
        if len(times) > 1:
            fields.setdefault("end_time", times[1])
    category_text = labeled.get("category", text)
    for category, pattern in [("타설", "타설|레미콘"), ("회의", "회의|미팅"), ("자재반입", "자재반입|반입|납품|입고"), ("기타", "기타")]:
        if re.search(pattern, category_text):
            fields["category"] = category
            break
    if "quantity" not in fields:
        match = re.search(r"\d[\d,]*(?:\.\d+)?\s*(?:m3|m³|㎥|루베|톤|ton|kg|t\b|m2|m²|㎡|제곱미터|개|본)", text, re.IGNORECASE)
        if match:
            fields["quantity"] = match[0]
    if "vehicle_count" not in fields:
        match = re.search(r"(?<!\d)(\d+)\s*대(?!상)", text)
        if match:
            fields["vehicle_count"] = match[1]
    if "location" not in fields:
        match = re.search(r"\d+동(?:\s*(?:지하\s*)?\d+층)?|지하\s*\d+층", text)
        if match:
            fields["location"] = match[0]
    if "company" not in fields:
        match = re.search(r"[가-힣A-Za-z]+(?:철강|건설|산업|레미콘|개발)\b", text)
        if match:
            fields["company"] = match[0]
    if "title" not in fields:
        candidates = [line for line in joined if not re.match(rf"^(?:{LABEL_PATTERN})(?:\s*[:：]|\s+)", line)]
        if candidates:
            fields["title"] = candidates[0][:200]
    if "schedule_date" not in fields:
        warnings.append("날짜를 인식하지 못했습니다. 왼쪽의 날짜를 확인해주세요.")
    if "title" not in fields:
        warnings.append("일정명을 인식하지 못했습니다. 왼쪽에 일정명을 입력해주세요.")
    return fields, warnings


OCR_LOCK = Lock()


@lru_cache(maxsize=1)
def get_ocr_engine():
    from rapidocr import ModelType, OCRVersion, RapidOCR
    model_dir = Path(__file__).resolve().parent / ".ocr-models"
    return RapidOCR(params={"Det.ocr_version": OCRVersion.PPOCRV4, "Det.model_type": ModelType.MOBILE, "Rec.lang_type": "korean", "Rec.ocr_version": OCRVersion.PPOCRV5, "Rec.model_type": ModelType.MOBILE, "Global.model_root_dir": str(model_dir)})


def recognize_schedule_image(image):
    """OCR stays on the app server. Models download on first use."""
    from PIL import ImageOps
    import numpy as np
    image = ImageOps.exif_transpose(image).convert("RGB")
    if image.width * image.height > 20_000_000:
        raise ValueError("이미지가 너무 큽니다. 일정 부분만 잘라서 다시 올려주세요.")
    with OCR_LOCK:
        output = get_ocr_engine()(np.asarray(image))
    if not output.txts:
        return ""
    # Group boxes on the same row so table-style 'label | value' remains readable.
    boxes = output.boxes
    if boxes is None:
        return "\n".join(output.txts)
    entries = []
    for box, text in zip(boxes, output.txts):
        x = float(min(point[0] for point in box))
        y = float(sum(point[1] for point in box) / len(box))
        height = float(max(point[1] for point in box) - min(point[1] for point in box))
        entries.append((y, x, max(height, 1), text))
    grouped = []
    for entry in sorted(entries):
        if grouped and abs(entry[0] - grouped[-1][0][0]) <= max(entry[2], grouped[-1][0][2]) * 0.5:
            grouped[-1].append(entry)
        else:
            grouped.append([entry])
    return "\n".join(" ".join(entry[3] for entry in sorted(group, key=lambda item: item[1])) for group in grouped)
