# app.py
# Website luyện đề THPTQG: Admin upload file đề PDF + ảnh/PDF đáp án,
# server dùng Gemini Vision đọc đáp án tự động thành JSON rồi tạo quiz.
#
# Cài thư viện:
#   pip install flask pymupdf pillow google-genai werkzeug
#
# Tạo Gemini API key ở Google AI Studio rồi set trên PowerShell:
#   setx GEMINI_API_KEY "AIza..."
# Đóng PowerShell, mở lại, rồi chạy:
#   python app.py
#
# Admin:
#   http://127.0.0.1:5000/admin/login
#   Mật khẩu: admin123

import io
import json
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path

import fitz  # PyMuPDF
from flask import (
    Flask,
    abort,
    redirect,
    render_template_string,
    request,
    send_from_directory,
    session,
    url_for,
)
from google import genai
from google.genai import types
from werkzeug.utils import secure_filename
from PIL import Image, ImageEnhance, ImageFilter


APP_SECRET = "doi-secret-nay-di"
ADMIN_PASSWORD = "admin123"
SUPERADMIN_PASSWORD = os.getenv("SUPERADMIN_PASSWORD", "superadmin123")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.getenv("DATA_DIR", os.path.join(BASE_DIR, "data"))

UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
DB_PATH = os.path.join(DATA_DIR, "quiz.db")

os.makedirs(DATA_DIR, exist_ok=True)
# Có thể đổi model:
#   setx GEMINI_MODEL "gemini-2.5-flash"
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

ALLOWED_EXAM_EXT = {".pdf"}
ALLOWED_ANSWER_EXT = {".pdf", ".png", ".jpg", ".jpeg", ".webp"}

SUBJECTS = {
    "math": {
        "label": "Toán",
        "icon": "∑",
        "duration_minutes": 90,
        "part1_count": 12,
        "part2_count": 4,
        "part3_count": 6,
        "part1_point": 0.25,
        "part3_point": 0.5,
    },
    "physics": {
        "label": "Lí",
        "icon": "⚛",
        "duration_minutes": 50,
        "part1_count": 18,
        "part2_count": 4,
        "part3_count": 6,
        "part1_point": 0.25,
        "part3_point": 0.25,
    },
    "cs": {
        "label": "Tin",
        "icon": "💻",
        "duration_minutes": 50,
        "part1_count": 24,
        "part2_count": 4,
        "part3_count": 0,
        "part1_point": 0.25,
        "part3_point": 0,
    },
    "english": {
        "label": "Tiếng Anh",
        "icon": "EN",
        "duration_minutes": 50,
        "part1_count": 60,
        "part2_count": 0,
        "part3_count": 0,
        "part1_point": 10 / 60,
        "part3_point": 0,
    },
    "geography": {
        "label": "Địa",
        "icon": "🌍",
        "duration_minutes": 50,
        "part1_count": 18,
        "part2_count": 4,
        "part3_count": 6,
        "part1_point": 0.25,
        "part3_point": 0.25,
    },
    "history": {
        "label": "Sử",
        "icon": "📜",
        "duration_minutes": 50,
        "part1_count": 24,
        "part2_count": 4,
        "part3_count": 0,
        "part1_point": 0.25,
        "part3_point": 0,
    },
}

# Điểm đúng/sai: trong 1 câu đúng/sai có 4 ý.
PART2_SCORE_MAP = {
    0: 0,
    1: 0.1,
    2: 0.25,
    3: 0.5,
    4: 1.0,
}

os.makedirs(UPLOAD_DIR, exist_ok=True)

app = Flask(__name__)
app.secret_key = APP_SECRET


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS exams (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            subject TEXT NOT NULL,
            exam_file TEXT NOT NULL,
            answer_file TEXT NOT NULL,
            data_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            display_order INTEGER NOT NULL DEFAULT 0
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS submission_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            exam_id INTEGER,
            exam_title TEXT NOT NULL,
            subject TEXT NOT NULL,
            score REAL NOT NULL,
            max_score REAL NOT NULL,
            wrong_total INTEGER NOT NULL,
            duration_seconds INTEGER,
            ip_address TEXT,
            user_agent TEXT,
            visitor_id TEXT,
            submitted_at TEXT NOT NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS site_visitors (
            visitor_id TEXT PRIMARY KEY,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            visit_count INTEGER NOT NULL DEFAULT 1,
            ip_address TEXT,
            user_agent TEXT
        )
        """
    )

    conn.commit()
    conn.close()


def migrate_old_db_if_needed():
    if not os.path.exists(DB_PATH):
        return

    conn = db()
    try:
        cols = conn.execute("PRAGMA table_info(exams)").fetchall()
        if not cols:
            return

        col_names = {c["name"] for c in cols}
        required = {"id", "title", "subject", "exam_file", "answer_file", "data_json", "created_at"}
        if not required.issubset(col_names):
            conn.execute("DROP TABLE IF EXISTS exams")
            conn.commit()
        # Bổ sung cột sắp xếp đề cho database cũ.
        cols = conn.execute("PRAGMA table_info(exams)").fetchall()
        col_names = {c["name"] for c in cols}
        if "display_order" not in col_names:
            conn.execute("ALTER TABLE exams ADD COLUMN display_order INTEGER NOT NULL DEFAULT 0")
            conn.commit()
    finally:
        conn.close()



def ensure_display_order_column():
    conn = db()
    try:
        cols = conn.execute("PRAGMA table_info(exams)").fetchall()
        col_names = {c["name"] for c in cols}
        if cols and "display_order" not in col_names:
            conn.execute("ALTER TABLE exams ADD COLUMN display_order INTEGER NOT NULL DEFAULT 0")
            conn.commit()
    finally:
        conn.close()


def secure_save_upload(file_storage, prefix: str) -> str:
    filename = secure_filename(file_storage.filename)
    final_name = f"{prefix}_{datetime.now().strftime('%Y%m%d%H%M%S')}_{filename}"
    path = os.path.join(UPLOAD_DIR, final_name)
    file_storage.save(path)
    return final_name


def remove_last_pdf_pages(filename: str, pages_to_remove: int = 1) -> str:
    """
    Xóa một hoặc nhiều trang cuối của file PDF đề.
    Dùng khi file đề tải trên mạng có trang đáp án ở cuối.
    Hàm này tạo file PDF mới, không đụng vào file gốc đã upload.
    """
    source_path = os.path.join(UPLOAD_DIR, filename)
    doc = fitz.open(source_path)

    if doc.page_count <= pages_to_remove:
        doc.close()
        raise ValueError("PDF quá ít trang, không thể xóa trang cuối.")

    for _ in range(pages_to_remove):
        doc.delete_page(doc.page_count - 1)

    stem = Path(filename).stem
    new_filename = f"{stem}_no_answer.pdf"
    new_path = os.path.join(UPLOAD_DIR, new_filename)

    doc.save(new_path)
    doc.close()

    try:
        os.remove(source_path)
    except OSError:
        pass

    return new_filename


def normalize_answer_value(value: str) -> str:
    value = str(value).strip().lower()
    value = re.sub(r"\s+", "", value)
    value = value.replace(",", ".")
    return value


def extract_first_number(value: str):
    """
    Lấy số đầu tiên trong đáp án tự luận ngắn.
    Hữu ích khi file đáp án có kèm đơn vị như: 67Hz, 22,9mol, 31,4kg.
    """
    value = str(value or "").strip().replace(",", ".")
    match = re.search(r"[-+]?\d+(?:\.\d+)?", value)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def answer_equal(user_value: str, right_value: str) -> bool:
    u = normalize_answer_value(user_value)
    r = normalize_answer_value(right_value)

    if not u or not r:
        return False

    if u == r:
        return True

    try:
        return abs(float(u) - float(r)) < 1e-9
    except ValueError:
        pass

    # Cho phép người dùng nhập 67 thay vì 67Hz, 22,9 thay vì 22,9mol...
    u_num = extract_first_number(user_value)
    r_num = extract_first_number(right_value)
    if u_num is not None and r_num is not None:
        return abs(u_num - r_num) < 1e-9

    return False


def blank_answers(subject: str) -> dict:
    cfg = SUBJECTS[subject]
    return {
        "part1": {str(i): "" for i in range(1, cfg["part1_count"] + 1)},
        "part2": {
            str(i): {"a": None, "b": None, "c": None, "d": None}
            for i in range(1, cfg["part2_count"] + 1)
        },
        "part3": {str(i): "" for i in range(1, cfg["part3_count"] + 1)},
    }


def normalize_ai_answers(raw: dict, subject: str) -> dict:
    cfg = SUBJECTS[subject]
    fixed = blank_answers(subject)

    # part1 có thể là list ["B","D",...] hoặc dict {"1":"B"}
    p1 = raw.get("part1", {})
    if isinstance(p1, list):
        for i in range(1, cfg["part1_count"] + 1):
            val = str(p1[i - 1]).strip().upper() if i - 1 < len(p1) else ""
            fixed["part1"][str(i)] = val if val in {"A", "B", "C", "D"} else ""
    elif isinstance(p1, dict):
        for i in range(1, cfg["part1_count"] + 1):
            val = str(p1.get(str(i), "")).strip().upper()
            fixed["part1"][str(i)] = val if val in {"A", "B", "C", "D"} else ""

    # part2 có thể là list object hoặc dict object
    p2 = raw.get("part2", {})
    if isinstance(p2, list):
        rows = p2
    elif isinstance(p2, dict):
        rows = [p2.get(str(i), {}) for i in range(1, cfg["part2_count"] + 1)]
    else:
        rows = []

    for i in range(1, cfg["part2_count"] + 1):
        row = rows[i - 1] if i - 1 < len(rows) and isinstance(rows[i - 1], dict) else {}
        fixed["part2"][str(i)] = {
            "a": row.get("a", None),
            "b": row.get("b", None),
            "c": row.get("c", None),
            "d": row.get("d", None),
        }
        for letter in ["a", "b", "c", "d"]:
            if fixed["part2"][str(i)][letter] not in {True, False, None}:
                fixed["part2"][str(i)][letter] = None

    # part3 có thể là list hoặc dict; Tin không có part3.
    p3 = raw.get("part3", {})
    if cfg["part3_count"] <= 0:
        fixed["part3"] = {}
    elif isinstance(p3, list):
        for i in range(1, cfg["part3_count"] + 1):
            fixed["part3"][str(i)] = str(p3[i - 1]).strip() if i - 1 < len(p3) else ""
    elif isinstance(p3, dict):
        for i in range(1, cfg["part3_count"] + 1):
            fixed["part3"][str(i)] = str(p3.get(str(i), "")).strip()

    return fixed


def prepare_answer_image_for_gemini(png_bytes: bytes) -> bytes:
    """
    Tăng độ rõ màu đáp án trước khi gửi Gemini.
    Rất hữu ích với file đáp án là nguyên đề có khoanh/tô màu đáp án:
    - vòng tròn đáp án thường tô vàng/xanh
    - chữ scan PDF đôi khi nhạt, Gemini dễ bỏ qua màu highlight
    """
    img = Image.open(io.BytesIO(png_bytes)).convert("RGB")

    # Tăng màu và nét, nhưng không đổi nội dung.
    img = ImageEnhance.Color(img).enhance(2.4)
    img = ImageEnhance.Contrast(img).enhance(1.18)
    img = ImageEnhance.Sharpness(img).enhance(1.35)
    img = img.filter(ImageFilter.SHARPEN)

    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()


def answer_file_to_gemini_parts(path: str) -> list:
    """
    Chuyển ảnh/PDF đáp án thành list Part để gửi lên Gemini.
    - Ảnh: gửi trực tiếp bytes.
    - PDF: render từng trang thành PNG rồi gửi từng ảnh.
    """
    ext = Path(path).suffix.lower()

    if ext == ".pdf":
        doc = fitz.open(path)
        parts = []
        for page in doc:
            # Render cao hơn để Gemini đọc được vòng tròn tô màu/highlight nhỏ.
            pix = page.get_pixmap(matrix=fitz.Matrix(4, 4), alpha=False)
            png_bytes = prepare_answer_image_for_gemini(pix.tobytes("png"))
            parts.append(types.Part.from_bytes(data=png_bytes, mime_type="image/png"))
        doc.close()
        return parts

    mime = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }.get(ext)

    if not mime:
        raise ValueError("File đáp án phải là PDF hoặc ảnh PNG/JPG/JPEG/WEBP.")

    with open(path, "rb") as f:
        raw = f.read()

    # Với ảnh đáp án đã tô màu, tăng màu để tránh Gemini bỏ sót highlight.
    try:
        raw = prepare_answer_image_for_gemini(raw)
        mime = "image/png"
    except Exception:
        pass

    return [types.Part.from_bytes(data=raw, mime_type=mime)]


def extract_json_from_text(text: str) -> dict:
    """
    Gemini thường trả JSON sạch nếu response_mime_type là application/json.
    Hàm này vẫn có fallback nếu nó bọc ```json ... ```.
    """
    text = (text or "").strip()

    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text, flags=re.I).strip()
        text = re.sub(r"```$", "", text).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # fallback lấy đoạn từ { đầu tiên tới } cuối cùng
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(text[start:end + 1])
        raise


def extract_answers_with_gemini(answer_file_path: str, subject: str) -> tuple[dict, str]:
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError(
            'Chưa có GEMINI_API_KEY. Chạy PowerShell: setx GEMINI_API_KEY "AIza..." rồi mở lại terminal.'
        )

    cfg = SUBJECTS[subject]
    client = genai.Client(api_key=api_key)
    image_parts = answer_file_to_gemini_parts(answer_file_path)

    prompt = f"""
Bạn là hệ thống OCR đọc ĐÁP ÁN THI từ ảnh/PDF. Nhiệm vụ của bạn là CHÉP LẠI ĐÁP ÁN ĐƯỢC ĐÁNH DẤU, không được giải đề.

Môn: {cfg["label"]}

Cấu trúc bắt buộc:
- Phần I có đúng {cfg["part1_count"]} câu trắc nghiệm A/B/C/D.
- Phần II có đúng {cfg["part2_count"]} câu đúng/sai, mỗi câu có 4 ý a,b,c,d.
- Phần III có đúng {cfg["part3_count"]} câu trả lời ngắn.
- Nếu phần nào có 0 câu thì trả object rỗng cho phần đó.

Hãy tự nhận diện 2 kiểu file đáp án sau:

KIỂU A — BẢNG ĐÁP ÁN RÚT GỌN:
- Có tiêu đề như "ĐÁP ÁN", "ĐÁP ÁN THAM KHẢO".
- Phần I: đọc bảng có hàng/cột "Câu" và "Đáp án". Ghép đáp án theo đúng số câu.
- Phần II: đọc bảng có cột a,b,c,d và dòng Câu 1, Câu 2...
  Đ, D, Đúng, Dung = true. S, Sai = false.
- Phần III: đọc hàng/cột "Đáp án", giữ nguyên chuỗi kể cả đơn vị như 67Hz, 22,9mol, 31,4kg.

KIỂU B — NGUYÊN ĐỀ ĐÃ TÔ/KHOANH ĐÁP ÁN:
Đây là trường hợp rất quan trọng.
- Phần I:
  + Mỗi câu có 4 lựa chọn A/B/C/D.
  + Đáp án đúng là lựa chọn có vòng tròn/chữ cái được TÔ MÀU, thường là màu VÀNG, XANH LÁ, hoặc được khoanh/highlight nổi bật.
  + Các vòng tròn viền xanh nhạt/chỉ là ký hiệu lựa chọn bình thường KHÔNG PHẢI đáp án.
  + Tuyệt đối KHÔNG chọn đáp án dựa trên nội dung câu hỏi.
  + Nếu không thấy lựa chọn nào được tô/khoanh màu rõ ràng thì để "".
- Phần II:
  + Ưu tiên đọc nhãn ở cuối mỗi ý: [ĐÚNG] = true, [SAI] = false.
  + Nếu không có nhãn nhưng có bảng Đ/S thì đọc theo bảng.
  + Không tự suy luận đúng sai từ nội dung câu.
- Phần III:
  + Lấy đáp án ngắn được ghi trong bảng đáp án hoặc vùng được tô/highlight.
  + Giữ nguyên đơn vị nếu có.

QUY TẮC CHỐNG ĐỌC SAI:
- Không tự giải đề.
- Không đoán đáp án theo kiến thức.
- Không lấy đáp án từ chữ A/B/C/D đầu dòng nếu nó không được tô/khoanh/highlight.
- Không đọc lẫn cột hoặc lẫn dòng.
- Không lấy mã đề làm đáp án.
- Không lấy số thứ tự câu, chữ "Câu", chữ "Ý", tiêu đề bảng làm đáp án.
- Với file đề tô đáp án, màu vàng/xanh nổi bật mới là đáp án; vòng tròn xanh nhạt thông thường chỉ là bullet lựa chọn.
- Nếu đang phân vân giữa text và màu highlight, hãy tin theo màu highlight.

Trả về DUY NHẤT JSON hợp lệ, không markdown, không giải thích:
{{
  "part1": {{
    "1": "D",
    "2": "C"
  }},
  "part2": {{
    "1": {{"a": true, "b": true, "c": false, "d": true}}
  }},
  "part3": {{
    "1": "4,9",
    "2": "67Hz"
  }}
}}

Bắt buộc trả đủ theo môn hiện tại:
- part1: từ "1" đến "{cfg["part1_count"]}"
- part2: từ "1" đến "{cfg["part2_count"]}", mỗi câu có a,b,c,d nếu có
- part3: từ "1" đến "{cfg["part3_count"]}" nếu có
"""

    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=[prompt] + image_parts,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0,
        ),
    )

    raw_text = response.text or ""
    raw_data = extract_json_from_text(raw_text)
    return normalize_ai_answers(raw_data, subject), raw_text


def build_exam_data(answer_file_path: str, subject: str) -> dict:
    answers, raw_json = extract_answers_with_gemini(answer_file_path, subject)
    return {
        "subject": subject,
        "answers": answers,
        "gemini_debug": {
            "model": GEMINI_MODEL,
            "raw_json": raw_json,
        },
    }


def calculate_total_max_score(subject: str) -> float:
    cfg = SUBJECTS[subject]
    total = 0
    total += cfg["part1_count"] * cfg["part1_point"]
    total += cfg["part2_count"] * 1.0
    total += cfg["part3_count"] * cfg["part3_point"]
    return round(total, 2)


def format_score(value) -> str:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return str(value)

    if value.is_integer():
        return str(int(value))

    return f"{value:.2f}".rstrip("0").rstrip(".").replace(".", ",")


def build_result_summary(results: dict, score: float, max_score: float) -> dict:
    wrong_part1 = [
        r for r in results.get("part1", [])
        if not r.get("ok")
    ]

    wrong_part2 = []
    for r in results.get("part2", []):
        wrong_items = [item for item in r.get("items", []) if not item.get("ok")]
        if wrong_items:
            wrong_part2.append({
                "number": r.get("number"),
                "correct_count": r.get("correct_count", 0),
                "point": r.get("point", 0),
                "items": wrong_items,
            })

    wrong_part3 = [
        r for r in results.get("part3", [])
        if not r.get("ok")
    ]

    part1_total = len(results.get("part1", []))
    part1_correct = part1_total - len(wrong_part1)

    part2_total = len(results.get("part2", []))
    part2_full_correct = len([r for r in results.get("part2", []) if r.get("correct_count") == 4])
    part2_point = sum(float(r.get("point", 0)) for r in results.get("part2", []))

    part3_total = len(results.get("part3", []))
    part3_correct = part3_total - len(wrong_part3)

    return {
        "score_text": format_score(score),
        "max_score_text": format_score(max_score),
        "part1_correct": part1_correct,
        "part1_total": part1_total,
        "part2_full_correct": part2_full_correct,
        "part2_total": part2_total,
        "part2_point_text": format_score(part2_point),
        "part3_correct": part3_correct,
        "part3_total": part3_total,
        "wrong_part1": wrong_part1,
        "wrong_part2": wrong_part2,
        "wrong_part3": wrong_part3,
        "wrong_total": len(wrong_part1) + len(wrong_part2) + len(wrong_part3),
    }


def get_client_ip() -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.headers.get("X-Real-IP") or request.remote_addr or ""


def get_or_create_visitor_id() -> str:
    visitor_id = session.get("visitor_id")
    if not visitor_id:
        visitor_id = f"v{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
        session["visitor_id"] = visitor_id
    return visitor_id


def require_superadmin():
    if not session.get("superadmin"):
        abort(403)


def log_site_visit():
    """
    Ghi nhận visitor ghé trang chủ.
    Không cần đăng nhập. Dựa theo visitor_id trong session/cookie.
    """
    visitor_id = get_or_create_visitor_id()
    now = datetime.now().isoformat(timespec="seconds")
    ip = get_client_ip()
    ua = request.headers.get("User-Agent", "")[:500]

    conn = db()
    exists = conn.execute(
        "SELECT visitor_id FROM site_visitors WHERE visitor_id=?",
        (visitor_id,),
    ).fetchone()

    if exists:
        conn.execute(
            """
            UPDATE site_visitors
            SET last_seen=?, visit_count=visit_count+1, ip_address=?, user_agent=?
            WHERE visitor_id=?
            """,
            (now, ip, ua, visitor_id),
        )
    else:
        conn.execute(
            """
            INSERT INTO site_visitors (visitor_id, first_seen, last_seen, visit_count, ip_address, user_agent)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (visitor_id, now, now, 1, ip, ua),
        )

    conn.commit()
    conn.close()


def log_submission(exam, subject: str, score: float, max_score: float, summary: dict):
    started_at = request.form.get("started_at", "").strip()
    duration_seconds = None

    if started_at:
        try:
            started = datetime.fromisoformat(started_at)
            duration_seconds = max(0, int((datetime.now() - started).total_seconds()))
        except ValueError:
            duration_seconds = None

    conn = db()
    conn.execute(
        """
        INSERT INTO submission_logs (
            exam_id, exam_title, subject, score, max_score, wrong_total,
            duration_seconds, ip_address, user_agent, visitor_id, submitted_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            exam["id"],
            exam["title"],
            subject,
            float(score),
            float(max_score),
            int(summary.get("wrong_total", 0)),
            duration_seconds,
            get_client_ip(),
            request.headers.get("User-Agent", "")[:500],
            get_or_create_visitor_id(),
            datetime.now().isoformat(timespec="seconds"),
        ),
    )
    conn.commit()
    conn.close()


def format_duration(seconds) -> str:
    if seconds is None:
        return ""
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return ""

    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)

    if hours:
        return f"{hours} giờ {minutes} phút {sec} giây"
    if minutes:
        return f"{minutes} phút {sec} giây"
    return f"{sec} giây"


def require_admin():
    if not session.get("admin"):
        abort(403)


def format_datetime_display(value: str) -> str:
    """
    Đổi 2026-05-28T19:29:54 -> 2026-05-28 19:29:54.
    Nếu format khác thì giữ nguyên.
    """
    if not value:
        return ""
    return str(value).replace("T", " ")


@app.route("/")
def home():
    selected_subject = (request.args.get("subject") or "all").strip().lower()

    visitor_id = get_or_create_visitor_id()
    log_site_visit()

    conn = db()
    if selected_subject in SUBJECTS:
        exams = conn.execute(
            """
            SELECT
                e.id, e.title, e.subject, e.created_at, e.display_order,
                COALESCE(s.submit_count, 0) AS submit_count
            FROM exams e
            LEFT JOIN (
                SELECT exam_id, COUNT(*) AS submit_count
                FROM submission_logs
                GROUP BY exam_id
            ) s ON s.exam_id = e.id
            WHERE e.subject=?
            ORDER BY e.display_order DESC, COALESCE(s.submit_count, 0) DESC, e.id DESC
            """,
            (selected_subject,)
        ).fetchall()
    else:
        selected_subject = "all"
        exams = conn.execute(
            """
            SELECT
                e.id, e.title, e.subject, e.created_at, e.display_order,
                COALESCE(s.submit_count, 0) AS submit_count
            FROM exams e
            LEFT JOIN (
                SELECT exam_id, COUNT(*) AS submit_count
                FROM submission_logs
                GROUP BY exam_id
            ) s ON s.exam_id = e.id
            ORDER BY e.display_order DESC, COALESCE(s.submit_count, 0) DESC, e.id DESC
            """
        ).fetchall()

    done_rows = conn.execute(
        """
        SELECT exam_id, MAX(score) AS best_score, MAX(max_score) AS max_score, MAX(submitted_at) AS last_done_at, COUNT(*) AS attempt_count
        FROM submission_logs
        WHERE visitor_id=?
        GROUP BY exam_id
        """,
        (visitor_id,),
    ).fetchall()
    done_map = {int(row["exam_id"]): row for row in done_rows if row["exam_id"] is not None}

    best_seller_count = 0
    for exam_row in exams:
        try:
            best_seller_count = max(best_seller_count, int(exam_row["submit_count"] or 0))
        except (KeyError, TypeError, ValueError):
            pass

    visitor_count = conn.execute("SELECT COUNT(*) AS c FROM site_visitors").fetchone()["c"]
    submit_count = conn.execute("SELECT COUNT(*) AS c FROM submission_logs").fetchone()["c"]
    conn.close()

    return render_template_string(
        HOME_HTML,
        exams=exams,
        subjects=SUBJECTS,
        selected_subject=selected_subject,
        done_map=done_map,
        visitor_count=visitor_count,
        submit_count=submit_count,
        best_seller_count=best_seller_count,
        format_datetime_display=format_datetime_display,
        format_score=format_score,
    )


@app.route("/exam/<int:exam_id>")
def take_exam(exam_id):
    conn = db()
    exam = conn.execute("SELECT * FROM exams WHERE id=?", (exam_id,)).fetchone()
    conn.close()

    if not exam:
        abort(404)

    cfg = SUBJECTS[exam["subject"]]
    return render_template_string(
        TAKE_EXAM_HTML,
        exam=exam,
        cfg=cfg,
        part1_numbers=range(1, cfg["part1_count"] + 1),
        part2_numbers=range(1, cfg["part2_count"] + 1),
        part3_numbers=range(1, cfg["part3_count"] + 1),
        max_score=calculate_total_max_score(exam["subject"]),
        started_at=datetime.now().isoformat(timespec="seconds"),
    )


@app.route("/exam/<int:exam_id>/submit", methods=["POST"])
def submit_exam(exam_id):
    conn = db()
    exam = conn.execute("SELECT * FROM exams WHERE id=?", (exam_id,)).fetchone()
    conn.close()

    if not exam:
        abort(404)

    subject = exam["subject"]
    cfg = SUBJECTS[subject]
    data = json.loads(exam["data_json"])
    answers = data["answers"]

    results = {"part1": [], "part2": [], "part3": []}
    score = 0.0

    # Phần I
    for n in range(1, cfg["part1_count"] + 1):
        n_str = str(n)
        user_ans = request.form.get(f"p1_{n}", "")
        right_ans = answers.get("part1", {}).get(n_str, "")
        ok = user_ans == right_ans and right_ans != ""
        if ok:
            score += cfg["part1_point"]
        results["part1"].append({
            "number": n,
            "user": user_ans,
            "right": right_ans,
            "ok": ok,
            "point": cfg["part1_point"] if ok else 0,
        })

    # Phần II
    for n in range(1, cfg["part2_count"] + 1):
        n_str = str(n)
        right_map = answers.get("part2", {}).get(n_str, {})
        correct_count = 0
        items = []

        for letter in ["a", "b", "c", "d"]:
            user_raw = request.form.get(f"p2_{n}_{letter}", "")
            user_bool = True if user_raw == "true" else False if user_raw == "false" else None
            right_bool = right_map.get(letter, None)
            ok = user_bool is not None and right_bool is not None and user_bool == right_bool
            if ok:
                correct_count += 1
            items.append({
                "letter": letter,
                "user": user_bool,
                "right": right_bool,
                "ok": ok,
            })

        part2_point = PART2_SCORE_MAP.get(correct_count, 0)
        score += part2_point

        results["part2"].append({
            "number": n,
            "correct_count": correct_count,
            "point": part2_point,
            "items": items,
        })

    # Phần III
    for n in range(1, cfg["part3_count"] + 1):
        n_str = str(n)
        user_ans = request.form.get(f"p3_{n}", "").strip()
        right_ans = answers.get("part3", {}).get(n_str, "")
        ok = answer_equal(user_ans, right_ans)
        if ok:
            score += cfg["part3_point"]
        results["part3"].append({
            "number": n,
            "user": user_ans,
            "right": right_ans,
            "ok": ok,
            "point": cfg["part3_point"] if ok else 0,
        })

    score = round(score, 2)
    max_score = calculate_total_max_score(subject)
    summary = build_result_summary(results, score, max_score)

    # Log lượt làm bài, không cần người dùng đăng nhập/đăng kí.
    log_submission(exam, subject, score, max_score, summary)

    return render_template_string(
        RESULT_HTML,
        exam=exam,
        cfg=cfg,
        results=results,
        summary=summary,
        score=score,
        score_text=format_score(score),
        max_score=max_score,
        max_score_text=format_score(max_score),
    )


@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename)


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = None
    if request.method == "POST":
        password = request.form.get("password")
        if password == ADMIN_PASSWORD:
            session["admin"] = True
            return redirect(url_for("admin_dashboard"))
        error = "Sai mật khẩu admin."
    return render_template_string(LOGIN_HTML, error=error)


@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("home"))


@app.route("/admin")
def admin_dashboard():
    require_admin()
    conn = db()
    exams = conn.execute("SELECT id, title, subject, created_at, display_order FROM exams ORDER BY display_order DESC, id DESC").fetchall()
    conn.close()
    return render_template_string(ADMIN_HTML, exams=exams, subjects=SUBJECTS, model=GEMINI_MODEL)


@app.route("/admin/upload", methods=["POST"])
def admin_upload():
    require_admin()

    title = request.form.get("title", "").strip()
    subject = request.form.get("subject", "").strip()
    exam_file = request.files.get("exam_file")
    answer_file = request.files.get("answer_file")

    if subject not in SUBJECTS:
        return "Môn không hợp lệ", 400

    if not title or not exam_file or not answer_file:
        return "Thiếu tiêu đề, file đề hoặc file đáp án", 400

    exam_ext = Path(exam_file.filename).suffix.lower()
    answer_ext = Path(answer_file.filename).suffix.lower()

    if exam_ext not in ALLOWED_EXAM_EXT:
        return "File đề phải là PDF", 400
    if answer_ext not in ALLOWED_ANSWER_EXT:
        return "File đáp án phải là PDF hoặc ảnh png/jpg/jpeg/webp", 400

    saved_exam = secure_save_upload(exam_file, "exam")
    saved_answer = secure_save_upload(answer_file, "answer")

    remove_answer_page = request.form.get("remove_answer_page") == "1"
    if remove_answer_page:
        try:
            saved_exam = remove_last_pdf_pages(saved_exam, pages_to_remove=1)
        except Exception as e:
            return f"Xóa trang đáp án cuối PDF thất bại: {e}", 500

    answer_path = os.path.join(UPLOAD_DIR, saved_answer)

    try:
        data = build_exam_data(answer_path, subject)
        data["exam_processing"] = {
            "removed_last_answer_page": remove_answer_page
        }
    except Exception as e:
        return f"Gemini đọc đáp án thất bại: {e}", 500

    conn = db()
    cur = conn.execute(
        """
        INSERT INTO exams (title, subject, exam_file, answer_file, data_json, created_at, display_order)
        VALUES (?, ?, ?, ?, ?, ?, COALESCE((SELECT MAX(display_order) FROM exams), 0) + 1)
        """,
        (
            title,
            subject,
            saved_exam,
            saved_answer,
            json.dumps(data, ensure_ascii=False),
            datetime.now().isoformat(timespec="seconds"),
        ),
    )
    exam_id = cur.lastrowid
    conn.commit()
    conn.close()

    # Vẫn qua trang sửa để admin nhìn qua nhanh; nếu chuẩn thì bấm lưu hoặc mở bài luôn.
    return redirect(url_for("admin_edit_answers", exam_id=exam_id))


@app.route("/admin/rename-exam/<int:exam_id>", methods=["POST"])
def admin_rename_exam(exam_id):
    require_admin()

    new_title = request.form.get("new_title", "").strip()
    if not new_title:
        return "Tên đề không được để trống", 400

    conn = db()
    exam = conn.execute("SELECT id FROM exams WHERE id=?", (exam_id,)).fetchone()
    if not exam:
        conn.close()
        abort(404)

    conn.execute("UPDATE exams SET title=? WHERE id=?", (new_title, exam_id))
    conn.execute("UPDATE submission_logs SET exam_title=? WHERE exam_id=?", (new_title, exam_id))
    conn.commit()
    conn.close()

    return redirect(url_for("admin_edit_answers", exam_id=exam_id))


@app.route("/admin/replace-exam-file/<int:exam_id>", methods=["POST"])
def admin_replace_exam_file(exam_id):
    require_admin()

    new_exam_file = request.files.get("new_exam_file")
    if not new_exam_file or not new_exam_file.filename:
        return "Chưa chọn file đề mới", 400

    exam_ext = Path(new_exam_file.filename).suffix.lower()
    if exam_ext not in ALLOWED_EXAM_EXT:
        return "File đề mới phải là PDF", 400

    conn = db()
    exam = conn.execute("SELECT * FROM exams WHERE id=?", (exam_id,)).fetchone()
    if not exam:
        conn.close()
        abort(404)

    saved_exam = secure_save_upload(new_exam_file, "exam")
    remove_answer_page = request.form.get("remove_answer_page") == "1"

    if remove_answer_page:
        try:
            saved_exam = remove_last_pdf_pages(saved_exam, pages_to_remove=1)
        except Exception as e:
            conn.close()
            return f"Xóa trang đáp án cuối PDF thất bại: {e}", 500

    old_exam_file = exam["exam_file"]

    conn.execute("UPDATE exams SET exam_file=? WHERE id=?", (saved_exam, exam_id))
    conn.commit()
    conn.close()

    try:
        os.remove(os.path.join(UPLOAD_DIR, old_exam_file))
    except OSError:
        pass

    return redirect(url_for("admin_edit_answers", exam_id=exam_id))


@app.route("/admin/edit/<int:exam_id>", methods=["GET", "POST"])
def admin_edit_answers(exam_id):
    require_admin()

    conn = db()
    exam = conn.execute("SELECT * FROM exams WHERE id=?", (exam_id,)).fetchone()

    if not exam:
        conn.close()
        abort(404)

    subject = exam["subject"]
    cfg = SUBJECTS[subject]
    data = json.loads(exam["data_json"])

    if request.method == "POST":
        new_answers = {"part1": {}, "part2": {}, "part3": {}}

        for n in range(1, cfg["part1_count"] + 1):
            value = request.form.get(f"p1_{n}", "").strip().upper()
            new_answers["part1"][str(n)] = value if value in {"A", "B", "C", "D"} else ""

        for n in range(1, cfg["part2_count"] + 1):
            new_answers["part2"][str(n)] = {}
            for letter in ["a", "b", "c", "d"]:
                value = request.form.get(f"p2_{n}_{letter}", "")
                if value == "true":
                    new_answers["part2"][str(n)][letter] = True
                elif value == "false":
                    new_answers["part2"][str(n)][letter] = False
                else:
                    new_answers["part2"][str(n)][letter] = None

        for n in range(1, cfg["part3_count"] + 1):
            new_answers["part3"][str(n)] = request.form.get(f"p3_{n}", "").strip()

        data["answers"] = new_answers
        conn.execute(
            "UPDATE exams SET data_json=? WHERE id=?",
            (json.dumps(data, ensure_ascii=False), exam_id),
        )
        conn.commit()
        conn.close()
        return redirect(url_for("admin_preview", exam_id=exam_id))

    conn.close()

    return render_template_string(
        EDIT_ANSWERS_HTML,
        exam=exam,
        cfg=cfg,
        data=data,
        gemini_raw_json=data.get("gemini_debug", {}).get("raw_json", ""),
        part1_numbers=range(1, cfg["part1_count"] + 1),
        part2_numbers=range(1, cfg["part2_count"] + 1),
        part3_numbers=range(1, cfg["part3_count"] + 1),
    )


@app.route("/admin/preview/<int:exam_id>")
def admin_preview(exam_id):
    require_admin()

    conn = db()
    exam = conn.execute("SELECT * FROM exams WHERE id=?", (exam_id,)).fetchone()
    conn.close()

    if not exam:
        abort(404)

    cfg = SUBJECTS[exam["subject"]]
    data = json.loads(exam["data_json"])
    data_pretty = json.dumps(data, ensure_ascii=False, indent=2)

    return render_template_string(
        PREVIEW_HTML,
        exam=exam,
        cfg=cfg,
        data_pretty=data_pretty,
    )


@app.route("/admin/exam-order/<int:exam_id>/<action>", methods=["POST"])
def admin_exam_order(exam_id, action):
    require_admin()

    if action not in {"top", "bottom", "up", "down"}:
        return "Hành động không hợp lệ", 400

    conn = db()
    exam = conn.execute("SELECT id, display_order FROM exams WHERE id=?", (exam_id,)).fetchone()
    if not exam:
        conn.close()
        abort(404)

    current_order = int(exam["display_order"] or 0)

    if action == "top":
        new_order = conn.execute("SELECT COALESCE(MAX(display_order), 0) + 1 AS v FROM exams").fetchone()["v"]
        conn.execute("UPDATE exams SET display_order=? WHERE id=?", (new_order, exam_id))

    elif action == "bottom":
        new_order = conn.execute("SELECT COALESCE(MIN(display_order), 0) - 1 AS v FROM exams").fetchone()["v"]
        conn.execute("UPDATE exams SET display_order=? WHERE id=?", (new_order, exam_id))

    elif action == "up":
        # Đổi chỗ với đề ngay phía trên.
        other = conn.execute(
            """
            SELECT id, display_order FROM exams
            WHERE display_order > ?
            ORDER BY display_order ASC, id ASC
            LIMIT 1
            """,
            (current_order,),
        ).fetchone()

        if other:
            conn.execute("UPDATE exams SET display_order=? WHERE id=?", (other["display_order"], exam_id))
            conn.execute("UPDATE exams SET display_order=? WHERE id=?", (current_order, other["id"]))
        else:
            new_order = conn.execute("SELECT COALESCE(MAX(display_order), 0) + 1 AS v FROM exams").fetchone()["v"]
            conn.execute("UPDATE exams SET display_order=? WHERE id=?", (new_order, exam_id))

    elif action == "down":
        # Đổi chỗ với đề ngay phía dưới.
        other = conn.execute(
            """
            SELECT id, display_order FROM exams
            WHERE display_order < ?
            ORDER BY display_order DESC, id DESC
            LIMIT 1
            """,
            (current_order,),
        ).fetchone()

        if other:
            conn.execute("UPDATE exams SET display_order=? WHERE id=?", (other["display_order"], exam_id))
            conn.execute("UPDATE exams SET display_order=? WHERE id=?", (current_order, other["id"]))
        else:
            new_order = conn.execute("SELECT COALESCE(MIN(display_order), 0) - 1 AS v FROM exams").fetchone()["v"]
            conn.execute("UPDATE exams SET display_order=? WHERE id=?", (new_order, exam_id))

    conn.commit()
    conn.close()

    return redirect(url_for("admin_dashboard"))


@app.route("/admin/delete/<int:exam_id>", methods=["POST"])
def admin_delete(exam_id):
    require_admin()

    conn = db()
    exam = conn.execute("SELECT exam_file, answer_file FROM exams WHERE id=?", (exam_id,)).fetchone()

    if exam:
        for col in ["exam_file", "answer_file"]:
            try:
                os.remove(os.path.join(UPLOAD_DIR, exam[col]))
            except OSError:
                pass
        conn.execute("DELETE FROM exams WHERE id=?", (exam_id,))
        conn.commit()

    conn.close()
    return redirect(url_for("admin_dashboard"))


@app.route("/superadmin/login", methods=["GET", "POST"])
def superadmin_login():
    error = None
    if request.method == "POST":
        password = request.form.get("password")
        if password == SUPERADMIN_PASSWORD:
            session["superadmin"] = True
            return redirect(url_for("superadmin_dashboard"))
        error = "Sai mật khẩu SuperAdmin."
    return render_template_string(SUPERADMIN_LOGIN_HTML, error=error)


@app.route("/superadmin/logout")
def superadmin_logout():
    session.pop("superadmin", None)
    return redirect(url_for("home"))


@app.route("/superadmin")
def superadmin_dashboard():
    require_superadmin()

    q = (request.args.get("q") or "").strip()
    subject = (request.args.get("subject") or "all").strip().lower()

    where = []
    params = []

    if subject in SUBJECTS:
        where.append("subject=?")
        params.append(subject)

    if q:
        where.append("(exam_title LIKE ? OR ip_address LIKE ? OR visitor_id LIKE ?)")
        like = f"%{q}%"
        params.extend([like, like, like])

    where_sql = "WHERE " + " AND ".join(where) if where else ""

    conn = db()
    logs = conn.execute(
        f"""
        SELECT * FROM submission_logs
        {where_sql}
        ORDER BY id DESC
        LIMIT 300
        """,
        params,
    ).fetchall()

    stats = conn.execute(
        """
        SELECT
            COUNT(*) AS total_submits,
            COUNT(DISTINCT visitor_id) AS total_visitors,
            ROUND(AVG(score), 2) AS avg_score
        FROM submission_logs
        """
    ).fetchone()

    by_exam = conn.execute(
        """
        SELECT exam_title, subject, COUNT(*) AS total, ROUND(AVG(score), 2) AS avg_score
        FROM submission_logs
        GROUP BY exam_id, exam_title, subject
        ORDER BY total DESC, exam_title ASC
        LIMIT 20
        """
    ).fetchall()

    conn.close()

    return render_template_string(
        SUPERADMIN_HTML,
        logs=logs,
        stats=stats,
        by_exam=by_exam,
        subjects=SUBJECTS,
        selected_subject=subject,
        q=q,
        format_datetime_display=format_datetime_display,
        format_duration=format_duration,
        format_score=format_score,
    )


@app.route("/superadmin/clear-logs", methods=["POST"])
def superadmin_clear_logs():
    require_superadmin()
    conn = db()
    conn.execute("DELETE FROM submission_logs")
    conn.commit()
    conn.close()
    return redirect(url_for("superadmin_dashboard"))


BASE_CSS = """
<!doctype html>
<html lang="vi">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>quép sai của Vũ</title>
    <meta name="description" content="Không có chi mô">
    <meta name="theme-color" content="#111827">

    <meta property="og:title" content="quép sai của Vũ">
    <meta property="og:description" content="Không có chi mô">
    <meta property="og:type" content="website">

    <link rel="icon" href="data:image/svg+xml,%3Csvg%20xmlns%3D%22http%3A//www.w3.org/2000/svg%22%20viewBox%3D%220%200%20100%20100%22%3E%0A%3Crect%20width%3D%22100%22%20height%3D%22100%22%20rx%3D%2222%22%20fill%3D%22%23111827%22/%3E%0A%3Ctext%20x%3D%2250%22%20y%3D%2262%22%20font-size%3D%2254%22%20text-anchor%3D%22middle%22%20font-family%3D%22Arial%2C%20sans-serif%22%20font-weight%3D%22700%22%20fill%3D%22%23ffffff%22%3EQ%3C/text%3E%0A%3Ccircle%20cx%3D%2274%22%20cy%3D%2228%22%20r%3D%2210%22%20fill%3D%22%232563eb%22/%3E%0A%3C/svg%3E">

    <script>
        (function () {
            const savedTheme = localStorage.getItem("theme") || "light";
            document.documentElement.setAttribute("data-theme", savedTheme);
        })();
    </script>
</head>
<body>

<style>
    * { box-sizing: border-box; }

    body {
        font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif;
        margin: 0;
        background: #f3f4f6;
        color: #111827;
    }

    a {
        color: #2563eb;
        text-decoration: none;
    }

    .nav {
        background: #111827;
        color: white;
        padding: 14px 28px;
        display: grid;
        grid-template-columns: 1fr auto 1fr;
        justify-content: space-between;
        align-items: center;
        gap: 16px;
    }

    .nav a {
        color: white;
        margin-left: 16px;
        font-weight: 800;
    }

    .nav > div:first-child {
        font-weight: 800;
        letter-spacing: .2px;
    }

    .nav > div:last-child {
        display: flex;
        justify-content: flex-end;
        align-items: center;
        gap: 10px;
    }

    .exam-countdown {
        justify-self: center;
        padding: 8px 14px;
        border: 1px solid rgba(148, 163, 184, .35);
        border-radius: 999px;
        background: rgba(15, 23, 42, .75);
        color: #e5e7eb;
        font-weight: 800;
        font-size: 14px;
        white-space: nowrap;
        box-shadow: inset 0 1px 0 rgba(255,255,255,.06);
    }

    .exam-countdown b {
        color: #ffffff;
    }

    .container {
        max-width: 1380px;
        margin: 20px auto;
        padding: 0 16px;
    }

    .card {
        background: white;
        border-radius: 16px;
        padding: 20px;
        margin-bottom: 16px;
        box-shadow: 0 8px 24px rgba(0,0,0,.06);
    }

    input, select, textarea, button {
        width: 100%;
        padding: 12px;
        border-radius: 10px;
        border: 1px solid #d1d5db;
        margin: 8px 0 14px;
        font-size: 15px;
        background: white;
        color: #111827;
    }

    button {
        background: #2563eb;
        color: white;
        border: none;
        cursor: pointer;
        font-weight: 700;
    }

    button:hover {
        background: #1d4ed8;
    }

    .danger {
        background: #dc2626;
    }

    .danger:hover {
        background: #b91c1c;
    }

    .grid {
        display: grid;
        grid-template-columns: minmax(560px, 1.08fr) minmax(440px, .92fr);
        gap: 16px;
        align-items: start;
    }

    .pdf {
        width: 100%;
        height: calc(100vh - 100px);
        min-height: 720px;
        border: 0;
        border-radius: 16px;
        background: white;
        position: sticky;
        top: 12px;
    }

    .muted {
        color: #6b7280;
    }

    pre {
        white-space: pre-wrap;
        background: #111827;
        color: #e5e7eb;
        padding: 16px;
        border-radius: 12px;
        overflow: auto;
    }

    .correct {
        background: #dcfce7;
        border-color: #22c55e;
    }

    .wrong {
        background: #fee2e2;
        border-color: #ef4444;
    }

    /* ===== Phiếu tô đáp án ===== */
    .sheet-card {
        padding: 18px;
    }

    .sheet-top {
        display: flex;
        justify-content: space-between;
        align-items: flex-start;
        gap: 12px;
        margin-bottom: 14px;
    }

    .sheet-top h1 {
        margin: 0 0 6px;
        font-size: 28px;
    }

    .timer {
        padding: 10px 14px;
        border-radius: 999px;
        background: #111827;
        color: white;
        font-weight: 700;
        white-space: nowrap;
        font-size: 15px;
    }

    .answer-sheet {
        border: 2px solid #111827;
        border-radius: 4px;
        background: #fff;
        overflow: hidden;
        margin-top: 14px;
    }

    .sheet-section {
        border-bottom: 2px solid #111827;
    }

    .sheet-section:last-of-type {
        border-bottom: 0;
    }

    .sheet-title {
        display: flex;
        align-items: center;
        justify-content: space-between;
        background: #f1f5f9;
        border-bottom: 1px solid #111827;
        padding: 8px 10px;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 0;
    }

    .scan-mark {
        width: 10px;
        height: 10px;
        background: #111827;
        display: inline-block;
        margin-left: 8px;
    }

    .sheet-caption {
        font-size: 12px;
        font-weight: 600;
        color: #475569;
        text-transform: none;
        letter-spacing: 0;
    }

    /* Part I: giống phiếu chia cột 1-10, 11-20... */
    .mc-columns {
        display: grid;
        grid-template-columns: repeat(2, 1fr);
        border-top: 0;
    }

    .mc-col {
        border-right: 1px solid #111827;
        padding: 8px;
    }

    .mc-col:last-child {
        border-right: 0;
    }

    .mc-header {
        display: grid;
        grid-template-columns: 34px repeat(4, 1fr);
        gap: 4px;
        margin-bottom: 4px;
        color: #64748b;
        font-size: 11px;
        text-align: center;
        font-weight: 700;
    }

    .mc-line {
        display: grid;
        grid-template-columns: 34px repeat(4, 1fr);
        gap: 4px;
        align-items: center;
        min-height: 26px;
    }

    .mc-num {
        text-align: right;
        padding-right: 5px;
        font-size: 13px;
        font-weight: 700;
        font-variant-numeric: tabular-nums;
    }

    .circle-choice input,
    .tf-choice input,
    .digit-choice input {
        display: none;
    }

    .circle-choice span,
    .tf-choice span,
    .digit-choice span {
        width: 18px;
        height: 18px;
        border: 1.6px solid #94a3b8;
        border-radius: 999px;
        display: flex;
        align-items: center;
        justify-content: center;
        margin: 0 auto;
        cursor: pointer;
        background: white;
        color: transparent;
        font-size: 10px;
        font-weight: 700;
        user-select: none;
        transition: .1s ease;
    }

    .circle-choice span:hover,
    .tf-choice span:hover,
    .digit-choice span:hover {
        border-color: #2563eb;
        background: #eff6ff;
    }

    .circle-choice input:checked + span,
    .digit-choice input:checked + span {
        background: #111827;
        border-color: #111827;
        color: white;
    }

    .circle-choice input:checked + span::after,
    .digit-choice input:checked + span::after {
        content: "";
        width: 8px;
        height: 8px;
        border-radius: 999px;
        background: white;
    }

    /* Part II */
    .tf-blocks {
        display: grid;
        grid-template-columns: repeat(2, 1fr);
        border-top: 0;
    }

    .tf-block {
        padding: 8px;
        border-right: 1px solid #111827;
        border-bottom: 1px solid #111827;
    }

    .tf-block:nth-child(2n) {
        border-right: 0;
    }

    .tf-block:nth-last-child(-n+2) {
        border-bottom: 0;
    }

    .tf-block-title {
        text-align: center;
        font-weight: 700;
        font-size: 13px;
        margin-bottom: 4px;
    }

    .tf-head {
        display: grid;
        grid-template-columns: 24px 1fr 1fr;
        gap: 4px;
        font-size: 11px;
        font-weight: 700;
        color: #64748b;
        text-align: center;
        margin-bottom: 2px;
    }

    .tf-line {
        display: grid;
        grid-template-columns: 24px 1fr 1fr;
        gap: 4px;
        align-items: center;
        min-height: 26px;
    }

    .tf-letter {
        font-size: 13px;
        font-weight: 700;
        text-align: center;
    }

    .tf-choice input:checked + span.true {
        background: #111827;
        border-color: #111827;
        color: white;
    }

    .tf-choice input:checked + span.false {
        background: #111827;
        border-color: #111827;
        color: white;
    }

    .tf-choice input:checked + span::after {
        content: "";
        width: 8px;
        height: 8px;
        border-radius: 999px;
        background: white;
    }

    /* Part III: dạng tô số giống phiếu thi thật */
    .short-columns {
        display: grid;
        grid-template-columns: repeat(2, 1fr);
    }

    .short-box {
        border-right: 1px solid #111827;
        border-bottom: 1px solid #111827;
        padding: 8px;
    }

    .short-box:nth-child(2n) {
        border-right: 0;
    }

    .short-box:nth-last-child(-n+2) {
        border-bottom: 0;
    }

    .short-title {
        font-weight: 700;
        text-align: center;
        font-size: 13px;
        margin-bottom: 6px;
    }

    .short-input {
        margin: 0 0 8px;
        padding: 8px 10px;
        text-align: center;
        font-weight: 700;
        border: 1px solid #94a3b8;
        border-radius: 8px;
        background: #fff;
    }

    .digit-grid {
        display: grid;
        grid-template-columns: repeat(6, 1fr);
        gap: 4px;
    }

    .digit-col {
        display: grid;
        gap: 3px;
    }

    .digit-label {
        text-align: center;
        font-size: 10px;
        color: #64748b;
        font-weight: 700;
        height: 14px;
    }

    .digit-choice {
        display: grid;
        grid-template-columns: 12px 1fr;
        align-items: center;
        gap: 2px;
        min-height: 19px;
    }

    .digit-choice small {
        font-size: 10px;
        color: #334155;
        text-align: right;
    }

    .digit-choice span {
        width: 15px;
        height: 15px;
    }

    .short-help {
        font-size: 11px;
        color: #64748b;
        text-align: center;
        margin-top: 6px;
    }

    .submit-bar {
        position: sticky;
        bottom: 12px;
        z-index: 10;
        margin-top: 14px;
        padding: 10px;
        background: rgba(255,255,255,.95);
        border: 1px solid #d1d5db;
        border-radius: 14px;
        box-shadow: 0 8px 24px rgba(0,0,0,.10);
        backdrop-filter: blur(8px);
    }

    .submit-bar button {
        margin: 0;
        height: 46px;
        border-radius: 10px;
        font-size: 16px;
    }

    /* Admin/result giữ như cũ */
    .admin-answer-grid {
        display: grid;
        grid-template-columns: repeat(6, 1fr);
        gap: 10px;
    }

    .mini {
        padding: 10px;
        border: 1px solid #e5e7eb;
        border-radius: 12px;
        background: #f9fafb;
    }

    .option {
        display: block;
        padding: 12px;
        border: 1px solid #e5e7eb;
        border-radius: 10px;
        margin: 6px 0;
        background: white;
    }

    .choices {
        display: grid;
        grid-template-columns: repeat(4, 1fr);
        gap: 8px;
    }


    /* Chỉ phiếu trả lời dùng Times New Roman cho giống phiếu thi thật */
    .answer-sheet,
    .answer-sheet input,
    .answer-sheet select,
    .answer-sheet button {
        font-family: "Times New Roman", Times, serif;
    }

    /* Đồng hồ dùng font số riêng cho dễ nhìn */
    .timer,
    #timer {
        font-family: "Segoe UI", system-ui, -apple-system, BlinkMacSystemFont, Arial, sans-serif;
        letter-spacing: .3px;
    }


    /* Result page */
    .result-hero {
        display: grid;
        grid-template-columns: 1fr auto;
        gap: 16px;
        align-items: center;
    }

    .score-badge {
        min-width: 160px;
        text-align: center;
        border: 1px solid #dbeafe;
        background: #eff6ff;
        color: #1e40af;
        border-radius: 16px;
        padding: 16px;
    }

    .score-badge .score-main {
        font-size: 34px;
        font-weight: 800;
        line-height: 1;
    }

    .score-badge .score-sub {
        font-size: 13px;
        margin-top: 6px;
        color: #475569;
    }

    .summary-grid {
        display: grid;
        grid-template-columns: repeat(3, minmax(0, 1fr));
        gap: 10px;
        margin-top: 14px;
    }

    .summary-box {
        border: 1px solid #e5e7eb;
        border-radius: 14px;
        padding: 12px;
        background: #f8fafc;
    }

    .summary-box b {
        display: block;
        font-size: 20px;
        margin-bottom: 4px;
    }

    .review-toolbar {
        display: flex;
        gap: 10px;
        flex-wrap: wrap;
        margin-top: 14px;
    }

    .review-toolbar a {
        padding: 10px 12px;
        border-radius: 10px;
        background: #eff6ff;
        border: 1px solid #bfdbfe;
        font-weight: 700;
    }

    .review-section {
        background: white;
        border-radius: 16px;
        padding: 18px;
        margin-bottom: 16px;
        box-shadow: 0 8px 24px rgba(0,0,0,.06);
    }

    .review-section h2 {
        margin: 0 0 12px;
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
    }

    .pill-count {
        font-size: 13px;
        padding: 6px 10px;
        border-radius: 999px;
        background: #f1f5f9;
        color: #475569;
        font-weight: 700;
    }

    .review-list {
        display: grid;
        gap: 10px;
    }

    .review-item {
        border: 1px solid #e5e7eb;
        border-radius: 14px;
        overflow: hidden;
        background: #fff;
    }

    .review-item.ok {
        border-color: #bbf7d0;
    }

    .review-item.bad {
        border-color: #fecaca;
    }

    .review-head {
        display: grid;
        grid-template-columns: 80px 1fr auto;
        gap: 12px;
        align-items: center;
        padding: 12px;
        background: #f8fafc;
        border-bottom: 1px solid #e5e7eb;
    }

    .review-item.bad .review-head {
        background: #fff1f2;
    }

    .review-q {
        font-weight: 800;
    }

    .review-status {
        font-weight: 700;
    }

    .review-status.bad {
        color: #dc2626;
    }

    .review-status.ok {
        color: #16a34a;
    }

    .review-point {
        font-weight: 800;
        color: #334155;
    }

    .answer-compare {
        display: grid;
        grid-template-columns: repeat(2, 1fr);
        gap: 10px;
        padding: 12px;
    }

    .answer-card {
        border: 1px solid #e5e7eb;
        border-radius: 12px;
        padding: 10px;
        background: white;
    }

    .answer-card .label {
        color: #64748b;
        font-size: 13px;
        margin-bottom: 4px;
    }

    .answer-card .value {
        font-size: 18px;
        font-weight: 800;
    }

    .value.good {
        color: #16a34a;
    }

    .value.bad {
        color: #dc2626;
    }

    .abc-strip {
        display: grid;
        grid-template-columns: repeat(4, 1fr);
        gap: 8px;
        padding: 0 12px 12px;
    }

    .abc-choice {
        border: 1px solid #e5e7eb;
        border-radius: 10px;
        padding: 10px;
        text-align: center;
        font-weight: 800;
        background: #fff;
        position: relative;
    }

    .abc-choice.right {
        background: #dcfce7;
        border-color: #22c55e;
        color: #166534;
    }

    .abc-choice.user-wrong {
        background: #fee2e2;
        border-color: #ef4444;
        color: #991b1b;
    }

    .abc-choice small {
        display: block;
        font-size: 11px;
        margin-top: 3px;
        font-weight: 700;
    }

    .tf-review-grid {
        display: grid;
        grid-template-columns: repeat(4, 1fr);
        gap: 8px;
        padding: 12px;
    }

    .tf-review-cell {
        border: 1px solid #e5e7eb;
        border-radius: 12px;
        padding: 10px;
        background: #fff;
    }

    .tf-review-cell.bad {
        background: #fee2e2;
        border-color: #ef4444;
    }

    .tf-review-cell.ok {
        background: #dcfce7;
        border-color: #22c55e;
    }

    .tf-review-cell b {
        display: block;
        margin-bottom: 6px;
    }

    .tf-review-cell span {
        display: block;
        font-size: 13px;
        color: #334155;
    }

    .only-wrong-note {
        color: #64748b;
        margin-top: -4px;
        margin-bottom: 14px;
    }

    @media (max-width: 760px) {
        .result-hero {
            grid-template-columns: 1fr;
        }
        .summary-grid,
        .answer-compare,
        .tf-review-grid {
            grid-template-columns: 1fr;
        }
        .review-head {
            grid-template-columns: 1fr;
        }
    }



    /* Simple result layout with PDF */
    .result-grid-simple {
        display: grid;
        grid-template-columns: minmax(560px, 1.08fr) minmax(420px, .92fr);
        gap: 16px;
        align-items: start;
    }

    .result-pdf {
        width: 100%;
        height: calc(100vh - 100px);
        min-height: 720px;
        border: 0;
        border-radius: 16px;
        background: white;
        position: sticky;
        top: 12px;
    }

    .simple-score {
        display: flex;
        justify-content: space-between;
        gap: 14px;
        align-items: center;
        border-bottom: 1px solid #e5e7eb;
        padding-bottom: 14px;
        margin-bottom: 14px;
    }

    .simple-score h1 {
        margin: 0 0 6px;
        font-size: 24px;
    }

    .score-number {
        font-size: 34px;
        font-weight: 800;
        color: #2563eb;
        white-space: nowrap;
    }

    .simple-stats {
        display: grid;
        grid-template-columns: repeat(3, 1fr);
        gap: 8px;
        margin-bottom: 14px;
    }

    .simple-stat {
        background: #f8fafc;
        border: 1px solid #e5e7eb;
        border-radius: 12px;
        padding: 10px;
        text-align: center;
    }

    .simple-stat b {
        display: block;
        font-size: 18px;
    }

    .wrong-list-simple {
        display: grid;
        gap: 10px;
    }

    .wrong-card-simple {
        border: 1px solid #fecaca;
        background: #fff7f7;
        border-radius: 12px;
        padding: 12px;
    }

    .wrong-card-simple h3 {
        margin: 0 0 8px;
        font-size: 17px;
        color: #991b1b;
    }

    .compare-simple {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 8px;
    }

    .compare-box {
        background: white;
        border: 1px solid #e5e7eb;
        border-radius: 10px;
        padding: 9px;
    }

    .compare-box span {
        display: block;
        color: #64748b;
        font-size: 13px;
        margin-bottom: 4px;
    }

    .compare-box b {
        font-size: 17px;
    }

    .tf-wrong-line {
        display: grid;
        gap: 6px;
        margin-top: 8px;
    }

    .tf-wrong-item {
        background: white;
        border: 1px solid #fecaca;
        border-radius: 10px;
        padding: 8px;
    }

    .quick-actions {
        display: flex;
        gap: 10px;
        flex-wrap: wrap;
        margin-top: 14px;
    }

    .quick-actions a {
        background: #eff6ff;
        border: 1px solid #bfdbfe;
        border-radius: 10px;
        padding: 9px 11px;
        font-weight: 700;
    }


    /* Ultra compact result layout */
    .result-compact-grid {
        display: grid;
        grid-template-columns: minmax(560px, 1.08fr) minmax(420px, .92fr);
        gap: 16px;
        align-items: start;
    }

    .result-compact-pdf {
        width: 100%;
        height: calc(100vh - 100px);
        min-height: 720px;
        border: 0;
        border-radius: 16px;
        background: white;
        position: sticky;
        top: 12px;
    }

    .result-panel {
        padding: 16px;
    }

    .result-line {
        display: flex;
        justify-content: space-between;
        gap: 12px;
        align-items: center;
        border-bottom: 1px solid #e5e7eb;
        padding-bottom: 10px;
        margin-bottom: 10px;
    }

    .result-line h1 {
        margin: 0;
        font-size: 22px;
        line-height: 1.2;
    }

    .result-line .score {
        font-size: 28px;
        font-weight: 800;
        color: #111827;
        white-space: nowrap;
    }

    .tiny-summary {
        display: flex;
        flex-wrap: wrap;
        gap: 8px;
        margin: 8px 0 12px;
    }

    .tiny-summary span {
        border: 1px solid #e5e7eb;
        background: #f9fafb;
        border-radius: 999px;
        padding: 6px 10px;
        font-size: 14px;
    }

    .wrong-compact {
        border: 1px solid #e5e7eb;
        border-radius: 12px;
        overflow: hidden;
        background: white;
    }

    .wrong-compact-title {
        padding: 10px 12px;
        background: #f9fafb;
        border-bottom: 1px solid #e5e7eb;
        font-weight: 800;
        display: flex;
        justify-content: space-between;
        gap: 10px;
    }

    .wrong-table {
        width: 100%;
        border-collapse: collapse;
        font-size: 15px;
    }

    .wrong-table th,
    .wrong-table td {
        padding: 9px 10px;
        border-bottom: 1px solid #e5e7eb;
        text-align: left;
        vertical-align: top;
    }

    .wrong-table th {
        background: #fff;
        color: #64748b;
        font-weight: 700;
        font-size: 13px;
    }

    .wrong-table tr:last-child td {
        border-bottom: 0;
    }

    .wrong-table .bad {
        color: #dc2626;
        font-weight: 800;
    }

    .wrong-table .good {
        color: #16a34a;
        font-weight: 800;
    }

    .wrong-table .qcol {
        white-space: nowrap;
        font-weight: 800;
    }

    .wrong-empty {
        padding: 14px 12px;
        color: #16a34a;
        font-weight: 700;
    }

    .result-actions {
        display: flex;
        gap: 8px;
        flex-wrap: wrap;
        margin-top: 12px;
    }

    .result-actions a {
        border: 1px solid #d1d5db;
        background: white;
        border-radius: 10px;
        padding: 8px 10px;
        font-weight: 700;
        color: #111827;
    }


    /* Result sheet review - scantron style, compact */
    .review-sheet-layout {
        display: grid;
        grid-template-columns: minmax(560px, 1.08fr) minmax(430px, .92fr);
        gap: 16px;
        align-items: start;
    }

    .review-pdf {
        width: 100%;
        height: calc(100vh - 100px);
        min-height: 720px;
        border: 0;
        border-radius: 16px;
        background: white;
        position: sticky;
        top: 12px;
    }

    .review-panel {
        padding: 14px;
    }

    .review-headline {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
        padding-bottom: 10px;
        border-bottom: 1px solid #e5e7eb;
        margin-bottom: 10px;
    }

    .review-headline h1 {
        margin: 0 0 4px;
        font-size: 22px;
        line-height: 1.2;
    }

    .review-score {
        font-size: 30px;
        font-weight: 800;
        white-space: nowrap;
    }

    .mini-stats {
        display: flex;
        flex-wrap: wrap;
        gap: 6px;
        margin: 8px 0 12px;
    }

    .mini-stats span {
        border: 1px solid #e5e7eb;
        background: #f8fafc;
        border-radius: 999px;
        padding: 5px 9px;
        font-size: 13px;
    }

    .result-sheet {
        border: 2px solid #111827;
        background: white;
        overflow: hidden;
        border-radius: 4px;
        margin-top: 10px;
        font-family: "Times New Roman", Times, serif;
    }

    .rs-section {
        border-bottom: 2px solid #111827;
    }

    .rs-section:last-child {
        border-bottom: 0;
    }

    .rs-title {
        padding: 7px 9px;
        background: #f1f5f9;
        border-bottom: 1px solid #111827;
        font-weight: 700;
        display: flex;
        justify-content: space-between;
        align-items: center;
        gap: 8px;
    }

    .rs-note {
        color: #475569;
        font-size: 12px;
        font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif;
        font-weight: 600;
    }

    .rs-mc-cols {
        display: grid;
        grid-template-columns: repeat(2, 1fr);
    }

    .rs-mc-col {
        border-right: 1px solid #111827;
        padding: 7px;
    }

    .rs-mc-col:last-child {
        border-right: 0;
    }

    .rs-mc-header,
    .rs-mc-line {
        display: grid;
        grid-template-columns: 34px repeat(4, 1fr);
        gap: 4px;
        align-items: center;
    }

    .rs-mc-header {
        color: #64748b;
        font-size: 11px;
        text-align: center;
        font-weight: 700;
        margin-bottom: 3px;
    }

    .rs-mc-line {
        min-height: 24px;
    }

    .rs-mc-line.unanswered,
    .rs-tf-line.unanswered {
        background: #f3f4f6;
        border: 1px dashed #cbd5e1;
        border-radius: 999px;
        padding-top: 1px;
        padding-bottom: 1px;
        margin: 1px 0;
    }

    .rs-num.unanswered,
    .rs-letter.unanswered {
        color: #6b7280;
    }


    .rs-num {
        text-align: right;
        padding-right: 5px;
        font-weight: 700;
        font-size: 13px;
    }

    .rs-circle {
        width: 18px;
        height: 18px;
        border: 1.6px solid #94a3b8;
        border-radius: 50%;
        display: block;
        margin: 0 auto;
        background: white;
        position: relative;
    }

    .rs-circle.user-correct {
        background: #111827;
        border-color: #111827;
    }

    .rs-circle.user-correct::after {
        content: "";
        position: absolute;
        width: 8px;
        height: 8px;
        border-radius: 50%;
        background: white;
        left: 50%;
        top: 50%;
        transform: translate(-50%, -50%);
    }

    .rs-circle.answer {
        border-color: #16a34a;
        background: #dcfce7;
    }

    .rs-circle.answer::after {
        content: "✓";
        position: absolute;
        color: #166534;
        font-size: 12px;
        font-weight: 800;
        left: 50%;
        top: 50%;
        transform: translate(-50%, -54%);
        font-family: Arial, sans-serif;
    }

    .rs-circle.user-wrong {
        border-color: #dc2626;
        background: #fee2e2;
    }

    .rs-circle.user-wrong::after {
        content: "×";
        position: absolute;
        color: #991b1b;
        font-size: 14px;
        font-weight: 800;
        left: 50%;
        top: 50%;
        transform: translate(-50%, -54%);
        font-family: Arial, sans-serif;
    }

    .rs-tf-blocks {
        display: grid;
        grid-template-columns: repeat(2, 1fr);
    }

    .rs-tf-block {
        border-right: 1px solid #111827;
        border-bottom: 1px solid #111827;
        padding: 7px;
    }

    .rs-tf-block:nth-child(2n) {
        border-right: 0;
    }

    .rs-tf-block:nth-last-child(-n+2) {
        border-bottom: 0;
    }

    .rs-tf-title {
        text-align: center;
        font-weight: 700;
        font-size: 13px;
        margin-bottom: 3px;
    }

    .rs-tf-head,
    .rs-tf-line {
        display: grid;
        grid-template-columns: 24px 1fr 1fr;
        gap: 4px;
        align-items: center;
    }

    .rs-tf-head {
        font-size: 11px;
        color: #64748b;
        text-align: center;
        font-weight: 700;
    }

    .rs-tf-line {
        min-height: 24px;
    }

    .rs-letter {
        font-size: 13px;
        font-weight: 700;
        text-align: center;
    }

    .rs-short-table {
        width: 100%;
        border-collapse: collapse;
        font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif;
        font-size: 14px;
    }

    .rs-short-table th,
    .rs-short-table td {
        padding: 7px 9px;
        border-bottom: 1px solid #e5e7eb;
        text-align: left;
    }

    .rs-short-table tr:last-child td {
        border-bottom: 0;
    }

    .rs-short-table .bad {
        color: #dc2626;
        font-weight: 800;
    }

    .rs-short-table .good {
        color: #16a34a;
        font-weight: 800;
    }

    .wrong-mini-table {
        width: 100%;
        border-collapse: collapse;
        font-size: 14px;
        margin-top: 10px;
    }

    .wrong-mini-table th,
    .wrong-mini-table td {
        border-bottom: 1px solid #e5e7eb;
        padding: 7px 8px;
        text-align: left;
    }

    .wrong-mini-table th {
        color: #64748b;
        font-size: 12px;
        background: #f8fafc;
    }

    .wrong-mini-table .bad {
        color: #dc2626;
        font-weight: 800;
    }

    .wrong-mini-table .good {
        color: #16a34a;
        font-weight: 800;
    }

    .result-actions-small {
        display: flex;
        gap: 8px;
        flex-wrap: wrap;
        margin-top: 10px;
    }

    .result-actions-small a {
        border: 1px solid #d1d5db;
        border-radius: 9px;
        padding: 7px 9px;
        font-weight: 700;
        color: #111827;
        background: white;
        font-size: 14px;
    }

    @media (max-width: 1080px) {
        .review-sheet-layout {
            grid-template-columns: 1fr;
        }
        .review-pdf {
            position: static;
            height: 560px;
            min-height: 520px;
        }
    }

    @media (max-width: 700px) {
        .review-headline {
            align-items: flex-start;
            flex-direction: column;
        }
        .rs-mc-cols,
        .rs-tf-blocks {
            grid-template-columns: 1fr;
        }
        .rs-mc-col,
        .rs-tf-block {
            border-right: 0 !important;
            border-bottom: 1px solid #111827;
        }
    }


    @media (max-width: 1080px) {
        .result-compact-grid {
            grid-template-columns: 1fr;
        }
        .result-compact-pdf {
            position: static;
            height: 560px;
            min-height: 520px;
        }
    }

    @media (max-width: 700px) {
        .result-line {
            align-items: flex-start;
            flex-direction: column;
        }
        .wrong-table {
            font-size: 14px;
        }
        .wrong-table th,
        .wrong-table td {
            padding: 8px 7px;
        }
    }


    @media (max-width: 1080px) {
        .result-grid-simple {
            grid-template-columns: 1fr;
        }
        .result-pdf {
            position: static;
            height: 560px;
            min-height: 520px;
        }
    }

    @media (max-width: 700px) {
        .simple-score {
            align-items: flex-start;
            flex-direction: column;
        }
        .simple-stats,
        .compare-simple {
            grid-template-columns: 1fr;
        }
    }


    @media (max-width: 1080px) {
        .grid { grid-template-columns: 1fr; }
        .pdf {
            position: static;
            height: 560px;
            min-height: 520px;
        }
    }

    @media (max-width: 700px) {
        .sheet-top {
            flex-direction: column;
        }
        .mc-columns,
        .tf-blocks,
        .short-columns {
            grid-template-columns: 1fr;
        }
        .mc-col,
        .tf-block,
        .short-box {
            border-right: 0 !important;
            border-bottom: 1px solid #111827;
        }
        .mc-col:last-child,
        .tf-block:last-child,
        .short-box:last-child {
            border-bottom: 0;
        }
        .admin-answer-grid {
            grid-template-columns: repeat(2, 1fr);
        }
    }

    /* ===== Result review final compact style ===== */
    .review-sheet-layout {
        display: grid;
        grid-template-columns: minmax(560px, 1.08fr) minmax(430px, .92fr);
        gap: 16px;
        align-items: start;
    }

    .review-pdf {
        width: 100%;
        height: calc(100vh - 100px);
        min-height: 720px;
        border: 0;
        border-radius: 16px;
        background: white;
        position: sticky;
        top: 12px;
    }

    .review-panel {
        padding: 14px;
    }

    .review-headline {
        display: flex;
        justify-content: space-between;
        align-items: center;
        gap: 12px;
        padding-bottom: 10px;
        margin-bottom: 10px;
        border-bottom: 1px solid #e5e7eb;
    }

    .review-headline h1 {
        margin: 0 0 4px;
        font-size: 22px;
        line-height: 1.2;
    }

    .review-score {
        font-size: 30px;
        font-weight: 800;
        color: #111827;
        white-space: nowrap;
    }

    .mini-stats {
        display: flex;
        flex-wrap: wrap;
        gap: 6px;
        margin: 8px 0 12px;
    }

    .mini-stats span {
        border: 1px solid #e5e7eb;
        background: #f8fafc;
        border-radius: 999px;
        padding: 5px 9px;
        font-size: 13px;
    }

    .result-sheet {
        border: 2px solid #111827;
        border-radius: 4px;
        overflow: hidden;
        background: white;
        margin-top: 10px;
        font-family: "Times New Roman", Times, serif;
    }

    .rs-section {
        border-bottom: 2px solid #111827;
    }

    .rs-section:last-child {
        border-bottom: 0;
    }

    .rs-title {
        padding: 7px 9px;
        background: #f1f5f9;
        border-bottom: 1px solid #111827;
        font-weight: 700;
        display: flex;
        justify-content: space-between;
        gap: 8px;
        align-items: center;
    }

    .rs-note {
        color: #475569;
        font-size: 12px;
        font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif;
        font-weight: 600;
    }

    .rs-mc-cols {
        display: grid;
        grid-template-columns: repeat(2, 1fr);
    }

    .rs-mc-col {
        border-right: 1px solid #111827;
        padding: 7px;
    }

    .rs-mc-col:last-child {
        border-right: 0;
    }

    .rs-mc-header,
    .rs-mc-line {
        display: grid;
        grid-template-columns: 34px repeat(4, 1fr);
        gap: 4px;
        align-items: center;
    }

    .rs-mc-header {
        color: #64748b;
        font-size: 11px;
        text-align: center;
        font-weight: 700;
        margin-bottom: 3px;
    }

    .rs-mc-line {
        min-height: 24px;
    }

    .rs-num {
        text-align: right;
        padding-right: 5px;
        font-weight: 700;
        font-size: 13px;
    }

    .rs-circle {
        width: 20px;
        height: 20px;
        border: 1.6px solid #94a3b8;
        border-radius: 50%;
        display: block;
        margin: 0 auto;
        background: #f8fafc;
        position: relative;
    }

    .rs-circle.correct {
        border-color: #16a34a;
        background: #dcfce7;
    }

    .rs-circle.correct::after {
        content: "✓";
        position: absolute;
        left: 50%;
        top: 50%;
        transform: translate(-50%, -56%);
        font-size: 14px;
        font-weight: 800;
        color: #15803d;
        font-family: Arial, sans-serif;
    }

    .rs-circle.wrong {
        border-color: #dc2626;
        background: #fee2e2;
    }

    .rs-circle.wrong::after {
        content: "×";
        position: absolute;
        left: 50%;
        top: 50%;
        transform: translate(-50%, -56%);
        font-size: 14px;
        font-weight: 800;
        color: #b91c1c;
        font-family: Arial, sans-serif;
    }

    .rs-circle.empty {
        border-color: #94a3b8;
        background: #fff;
    }

    .rs-tf-blocks {
        display: grid;
        grid-template-columns: repeat(2, 1fr);
    }

    .rs-tf-block {
        border-right: 1px solid #111827;
        border-bottom: 1px solid #111827;
        padding: 7px;
    }

    .rs-tf-block:nth-child(2n) {
        border-right: 0;
    }

    .rs-tf-block:nth-last-child(-n+2) {
        border-bottom: 0;
    }

    .rs-tf-title {
        text-align: center;
        font-weight: 700;
        font-size: 13px;
        margin-bottom: 3px;
    }

    .rs-tf-head,
    .rs-tf-line {
        display: grid;
        grid-template-columns: 24px 1fr 1fr;
        gap: 4px;
        align-items: center;
    }

    .rs-tf-head {
        font-size: 11px;
        color: #64748b;
        text-align: center;
        font-weight: 700;
    }

    .rs-tf-line {
        min-height: 24px;
    }

    .rs-letter {
        font-size: 13px;
        font-weight: 700;
        text-align: center;
    }

    .rs-short-table {
        width: 100%;
        border-collapse: collapse;
        font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif;
        font-size: 14px;
    }

    .rs-short-table th,
    .rs-short-table td {
        padding: 7px 9px;
        border-bottom: 1px solid #e5e7eb;
        text-align: left;
    }

    .rs-short-table tr:last-child td {
        border-bottom: 0;
    }

    .rs-short-table .bad {
        color: #dc2626;
        font-weight: 800;
    }

    .rs-short-table .good {
        color: #16a34a;
        font-weight: 800;
    }

    .result-actions-small {
        display: flex;
        gap: 8px;
        flex-wrap: wrap;
        margin-top: 10px;
    }

    .result-actions-small a {
        border: 1px solid #d1d5db;
        border-radius: 9px;
        padding: 7px 9px;
        font-weight: 700;
        color: #111827;
        background: white;
        font-size: 14px;
    }

    @media (max-width: 1080px) {
        .review-sheet-layout {
            grid-template-columns: 1fr;
        }
        .review-pdf {
            position: static;
            height: 560px;
            min-height: 520px;
        }
    }

    @media (max-width: 700px) {
        .review-headline {
            flex-direction: column;
            align-items: flex-start;
        }
        .rs-mc-cols,
        .rs-tf-blocks {
            grid-template-columns: 1fr;
        }
        .rs-mc-col,
        .rs-tf-block {
            border-right: 0 !important;
            border-bottom: 1px solid #111827;
        }
    }


    .digit-choice span {
        cursor: pointer;
    }

    .digit-choice input:not(:disabled) + span:hover {
        border-color: #2563eb;
        background: #eff6ff;
    }

    .digit-choice input:not(:disabled):checked + span {
        background: #111827;
        border-color: #111827;
    }

    .digit-choice input:not(:disabled):checked + span::after {
        content: "";
        width: 7px;
        height: 7px;
        border-radius: 999px;
        background: white;
    }

    .short-input.filled-by-bubbles {
        border-color: #111827;
        font-weight: 700;
    }



    .short-input-row {
        display: grid;
        grid-template-columns: 1fr auto;
        gap: 8px;
        align-items: center;
        margin-bottom: 8px;
    }

    .short-input-row .short-input {
        margin: 0;
    }

    .clear-short-btn {
        width: auto;
        margin: 0;
        padding: 8px 10px;
        border-radius: 8px;
        border: 1px solid #d1d5db;
        background: #f8fafc;
        color: #111827;
        cursor: pointer;
        font-weight: 700;
        font-size: 13px;
        white-space: nowrap;
    }

    .clear-short-btn:hover {
        background: #fee2e2;
        border-color: #fca5a5;
        color: #991b1b;
    }



    .digit-omr {
        display: grid;
        gap: 3px;
        margin-top: 8px;
    }

    .digit-omr-row {
        display: grid;
        grid-template-columns: 22px repeat(6, 1fr);
        gap: 6px;
        align-items: center;
        min-height: 22px;
    }

    .digit-omr-head {
        font-size: 12px;
        color: #64748b;
        font-weight: 700;
        text-align: center;
        margin-bottom: 2px;
    }

    .digit-omr-label {
        font-size: 12px;
        color: #334155;
        text-align: right;
        padding-right: 3px;
    }

    .digit-placeholder {
        width: 16px;
        height: 16px;
        display: block;
        margin: 0 auto;
    }



    .digit-special {
        display: grid;
        grid-template-columns: auto auto;
        gap: 4px;
        align-items: center;
        justify-content: center;
    }

    .digit-special small {
        font-size: 12px;
        color: #334155;
        font-weight: 700;
        line-height: 1;
    }



    .digit-omr-row .digit-choice {
        justify-content: center;
    }

    .digit-omr-row .digit-placeholder {
        margin: 0 auto;
    }



    /* Làm ô nhập Phần III và nút Xóa nhẹ hơn */
    .short-input-row {
        display: grid;
        grid-template-columns: 1fr auto;
        gap: 8px;
        align-items: center;
        margin-bottom: 10px;
    }

    .short-input-row .short-input {
        margin: 0;
        height: 40px;
        max-width: 260px;
        width: 100%;
        justify-self: start;
        border: 1px solid #cbd5e1;
        border-radius: 12px;
        background: #f8fafc;
        color: #111827;
        font-weight: 700;
        text-align: center;
        box-shadow: inset 0 1px 2px rgba(15, 23, 42, .04);
    }

    .short-input-row .short-input::placeholder {
        color: #64748b;
        font-weight: 600;
    }

    .short-input-row .short-input:focus {
        border-color: #2563eb;
        background: #fff;
        outline: none;
        box-shadow: 0 0 0 3px rgba(37, 99, 235, .12);
    }

    .clear-short-btn {
        width: auto;
        height: 40px;
        margin: 0;
        padding: 0 12px;
        border-radius: 12px;
        border: 1px solid #fecaca;
        background: #fff7f7;
        color: #b91c1c;
        cursor: pointer;
        font-weight: 700;
        font-size: 14px;
        white-space: nowrap;
        box-shadow: none;
    }

    .clear-short-btn:hover {
        background: #fee2e2;
        border-color: #fca5a5;
        color: #991b1b;
    }

    .clear-short-btn:active {
        transform: translateY(1px);
    }



    /* Home page */
    .page-head {
        display: flex;
        justify-content: space-between;
        align-items: end;
        gap: 16px;
        margin-bottom: 18px;
        flex-wrap: wrap;
    }

    .page-head h1 {
        margin: 0 0 6px;
        font-size: 28px;
    }

    .filters {
        display: flex;
        gap: 8px;
        flex-wrap: wrap;
    }

    .filter-chip {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        min-width: 76px;
        padding: 9px 14px;
        border-radius: 999px;
        border: 1px solid #d1d5db;
        color: #374151;
        background: white;
        font-weight: 700;
        font-size: 14px;
    }

    .filter-chip.active {
        background: #0f172a;
        color: white;
        border-color: #0f172a;
    }

    .exam-list-card {
        padding: 20px 22px;
    }

    .exam-card-row {
        display: grid;
        grid-template-columns: 58px 1fr;
        gap: 16px;
        align-items: start;
    }

    .subject-icon {
        width: 58px;
        height: 58px;
        border-radius: 16px;
        display: flex;
        align-items: center;
        justify-content: center;
        font-size: 28px;
        font-weight: 800;
        background: #eef2ff;
        color: #3730a3;
        border: 1px solid #c7d2fe;
        flex-shrink: 0;
    }

    .subject-icon.physics {
        background: #ecfeff;
        color: #155e75;
        border-color: #a5f3fc;
    }

    .subject-icon.cs {
        background: #ecfdf5;
        color: #166534;
        border-color: #bbf7d0;
    }

    .exam-card-top {
        display: flex;
        justify-content: space-between;
        gap: 12px;
        align-items: start;
        flex-wrap: wrap;
    }

    .exam-card-top h2 {
        margin: 0 0 8px;
        font-size: 24px;
    }

    .subject-pill {
        display: inline-flex;
        align-items: center;
        padding: 6px 11px;
        border-radius: 999px;
        background: #eef2ff;
        color: #3730a3;
        font-size: 13px;
        font-weight: 800;
        border: 1px solid #c7d2fe;
        white-space: nowrap;
    }

    .subject-pill.physics {
        background: #ecfeff;
        color: #155e75;
        border-color: #a5f3fc;
    }

    .subject-pill.cs {
        background: #ecfdf5;
        color: #166534;
        border-color: #bbf7d0;
    }

    .exam-meta {
        margin: 0 0 12px;
    }

    .exam-start-link {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        font-weight: 800;
    }

    @media (max-width: 700px) {
        .page-head {
            align-items: start;
        }

        .exam-card-row {
            grid-template-columns: 1fr;
        }

        .subject-icon {
            width: 48px;
            height: 48px;
            font-size: 22px;
        }

        .exam-card-top h2 {
            font-size: 21px;
        }
    }



    /* ===== Dark / Light mode ===== */
    .theme-toggle {
        width: auto;
        margin: 0 0 0 12px;
        padding: 7px 12px;
        border-radius: 999px;
        border: 1px solid rgba(255,255,255,.22);
        background: rgba(255,255,255,.10);
        color: #fff;
        font-weight: 800;
        font-size: 14px;
        cursor: pointer;
        white-space: nowrap;
    }

    .theme-toggle:hover {
        background: rgba(255,255,255,.18);
    }

    html[data-theme="dark"] body {
        background: #0f172a;
        color: #e5e7eb;
    }

    html[data-theme="dark"] .nav {
        background: #020617;
        border-bottom: 1px solid #1e293b;
    }

    html[data-theme="dark"] .card,
    html[data-theme="dark"] .review-section,
    html[data-theme="dark"] .review-panel,
    html[data-theme="dark"] .result-panel,
    html[data-theme="dark"] .sheet-card {
        background: #111827;
        color: #e5e7eb;
        box-shadow: 0 8px 24px rgba(0,0,0,.35);
        border: 1px solid #1f2937;
    }

    html[data-theme="dark"] .muted,
    html[data-theme="dark"] .sheet-caption,
    html[data-theme="dark"] .rs-note,
    html[data-theme="dark"] .digit-label,
    html[data-theme="dark"] .short-help {
        color: #94a3b8;
    }

    html[data-theme="dark"] a {
        color: #60a5fa;
    }

    html[data-theme="dark"] input,
    html[data-theme="dark"] select,
    html[data-theme="dark"] textarea {
        background: #020617;
        color: #e5e7eb;
        border-color: #334155;
    }

    html[data-theme="dark"] input::placeholder,
    html[data-theme="dark"] textarea::placeholder {
        color: #64748b;
    }

    html[data-theme="dark"] .mini,
    html[data-theme="dark"] .option,
    html[data-theme="dark"] .summary-box,
    html[data-theme="dark"] .simple-stat,
    html[data-theme="dark"] .compare-box,
    html[data-theme="dark"] .wrong-compact,
    html[data-theme="dark"] .answer-card,
    html[data-theme="dark"] .review-item,
    html[data-theme="dark"] .tf-review-cell,
    html[data-theme="dark"] .exam-list-card {
        background: #0b1220;
        border-color: #1f2937;
        color: #e5e7eb;
    }

    html[data-theme="dark"] .answer-sheet,
    html[data-theme="dark"] .result-sheet {
        background: #f8fafc;
        color: #111827;
    }

    html[data-theme="dark"] .pdf,
    html[data-theme="dark"] .review-pdf,
    html[data-theme="dark"] .result-pdf,
    html[data-theme="dark"] .result-compact-pdf {
        background: #111827;
        border: 1px solid #1f2937;
    }

    html[data-theme="dark"] .submit-bar {
        background: rgba(15,23,42,.95);
        border-color: #334155;
    }

    html[data-theme="dark"] .quick-actions a,
    html[data-theme="dark"] .result-actions a,
    html[data-theme="dark"] .result-actions-small a,
    html[data-theme="dark"] .review-toolbar a,
    html[data-theme="dark"] .filter-chip {
        background: #0b1220;
        color: #e5e7eb;
        border-color: #334155;
    }

    html[data-theme="dark"] .filter-chip.active {
        background: #2563eb;
        color: white;
        border-color: #2563eb;
    }

    html[data-theme="dark"] .subject-icon,
    html[data-theme="dark"] .subject-pill {
        filter: brightness(.92);
    }

    html[data-theme="dark"] pre {
        background: #020617;
        color: #dbeafe;
        border: 1px solid #1f2937;
    }

    html[data-theme="dark"] .theme-toggle {
        background: #f8fafc;
        color: #111827;
        border-color: #f8fafc;
    }



    /* ===== Dark mode cho phiếu trả lời / phiếu kết quả ===== */
    html[data-theme="dark"] .answer-sheet,
    html[data-theme="dark"] .result-sheet {
        background: #111827;
        color: #e5e7eb;
        border-color: #475569;
        box-shadow: 0 12px 32px rgba(0,0,0,.35);
    }

    html[data-theme="dark"] .sheet-section,
    html[data-theme="dark"] .rs-section {
        border-bottom-color: #475569;
    }

    html[data-theme="dark"] .sheet-title,
    html[data-theme="dark"] .rs-title {
        background: #1e293b;
        color: #f8fafc;
        border-bottom-color: #475569;
    }

    html[data-theme="dark"] .scan-mark {
        background: #f8fafc;
    }

    html[data-theme="dark"] .mc-col,
    html[data-theme="dark"] .tf-block,
    html[data-theme="dark"] .short-box,
    html[data-theme="dark"] .rs-mc-col,
    html[data-theme="dark"] .rs-tf-block {
        border-color: #475569;
        background: #0f172a;
    }

    html[data-theme="dark"] .mc-header,
    html[data-theme="dark"] .tf-head,
    html[data-theme="dark"] .rs-mc-header,
    html[data-theme="dark"] .rs-tf-head {
        color: #cbd5e1;
    }

    html[data-theme="dark"] .mc-num,
    html[data-theme="dark"] .tf-letter,
    html[data-theme="dark"] .short-title,
    html[data-theme="dark"] .rs-num,
    html[data-theme="dark"] .rs-letter,
    html[data-theme="dark"] .rs-tf-title {
        color: #f8fafc;
    }

    html[data-theme="dark"] .circle-choice span,
    html[data-theme="dark"] .tf-choice span,
    html[data-theme="dark"] .digit-choice span,
    html[data-theme="dark"] .rs-circle {
        background: #020617;
        border-color: #94a3b8;
    }

    html[data-theme="dark"] .circle-choice span:hover,
    html[data-theme="dark"] .tf-choice span:hover,
    html[data-theme="dark"] .digit-choice span:hover {
        background: #172554;
        border-color: #60a5fa;
    }

    html[data-theme="dark"] .circle-choice input:checked + span,
    html[data-theme="dark"] .tf-choice input:checked + span.true,
    html[data-theme="dark"] .tf-choice input:checked + span.false,
    html[data-theme="dark"] .digit-choice input:checked + span {
        background: #e5e7eb;
        border-color: #e5e7eb;
    }

    html[data-theme="dark"] .circle-choice input:checked + span::after,
    html[data-theme="dark"] .tf-choice input:checked + span::after,
    html[data-theme="dark"] .digit-choice input:checked + span::after {
        background: #111827;
    }

    html[data-theme="dark"] .short-input-row .short-input,
    html[data-theme="dark"] .short-input {
        background: #020617;
        color: #f8fafc;
        border-color: #475569;
        box-shadow: inset 0 1px 2px rgba(0,0,0,.35);
    }

    html[data-theme="dark"] .clear-short-btn {
        background: #2b1111;
        color: #fecaca;
        border-color: #7f1d1d;
    }

    html[data-theme="dark"] .clear-short-btn:hover {
        background: #3b1111;
        border-color: #991b1b;
    }

    html[data-theme="dark"] .sheet-caption,
    html[data-theme="dark"] .rs-note,
    html[data-theme="dark"] .digit-label,
    html[data-theme="dark"] .digit-choice small {
        color: #cbd5e1;
    }

    html[data-theme="dark"] .rs-circle.answer {
        border-color: #22c55e;
        background: #14532d;
    }

    html[data-theme="dark"] .rs-circle.answer::after {
        color: #bbf7d0;
    }

    html[data-theme="dark"] .rs-circle.user-wrong {
        border-color: #ef4444;
        background: #7f1d1d;
    }

    html[data-theme="dark"] .rs-circle.user-wrong::after {
        color: #fecaca;
    }

    html[data-theme="dark"] .rs-circle.user-correct,
    html[data-theme="dark"] .rs-circle.correct {
        background: #e5e7eb;
        border-color: #e5e7eb;
    }

    html[data-theme="dark"] .rs-circle.user-correct::after {
        background: #111827;
    }

    html[data-theme="dark"] .rs-mc-line.unanswered,
    html[data-theme="dark"] .rs-tf-line.unanswered {
        background: #1f2937;
        border-color: #64748b;
    }

    html[data-theme="dark"] .rs-short-table {
        color: #e5e7eb;
    }

    html[data-theme="dark"] .rs-short-table th,
    html[data-theme="dark"] .rs-short-table td {
        border-bottom-color: #334155;
    }



    /* ===== Nav actions đẹp hơn ===== */
    .nav > div:last-child {
        display: flex;
        align-items: center;
        gap: 10px;
    }

    .nav > div:last-child a,
    .theme-toggle {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        height: 42px;
        padding: 0 16px;
        border-radius: 999px;
        font-weight: 800;
        font-size: 15px;
        text-decoration: none;
        transition: all .18s ease;
    }

    .nav > div:last-child a {
        border: 1px solid rgba(255,255,255,.14);
        background: rgba(255,255,255,.06);
        color: #dbeafe;
        box-shadow: inset 0 1px 0 rgba(255,255,255,.04);
    }

    .nav > div:last-child a:hover {
        background: rgba(255,255,255,.12);
        color: #ffffff;
        border-color: rgba(255,255,255,.22);
        transform: translateY(-1px);
    }

    .theme-toggle {
        min-width: 96px;
        margin: 0;
        padding: 0 16px;
        border: 1px solid rgba(255,255,255,.16);
        background: linear-gradient(180deg, rgba(255,255,255,.16), rgba(255,255,255,.08));
        color: #ffffff;
        box-shadow: inset 0 1px 0 rgba(255,255,255,.08), 0 4px 14px rgba(0,0,0,.18);
    }

    .theme-toggle:hover {
        background: linear-gradient(180deg, rgba(255,255,255,.24), rgba(255,255,255,.12));
        transform: translateY(-1px);
    }

    .theme-toggle:active,
    .nav > div:last-child a:active {
        transform: translateY(0);
    }

    html[data-theme="dark"] .nav > div:last-child a {
        background: rgba(15,23,42,.92);
        color: #e5e7eb;
        border-color: #334155;
    }

    html[data-theme="dark"] .nav > div:last-child a:hover {
        background: #162033;
        color: #ffffff;
        border-color: #475569;
    }

    html[data-theme="dark"] .theme-toggle {
        background: linear-gradient(180deg, #f8fafc, #e2e8f0);
        color: #0f172a;
        border-color: #e2e8f0;
        box-shadow: 0 4px 14px rgba(0,0,0,.28);
    }

    html[data-theme="dark"] .theme-toggle:hover {
        background: linear-gradient(180deg, #ffffff, #e5e7eb);
    }

    @media (max-width: 640px) {
        .nav {
            gap: 10px;
            flex-wrap: wrap;
        }

        .nav > div:last-child {
            width: 100%;
            justify-content: flex-end;
        }

        .nav > div:last-child a,
        .theme-toggle {
            height: 38px;
            padding: 0 14px;
            font-size: 14px;
        }
    }



    .subject-icon.english {
        background: #fff7ed;
        color: #9a3412;
        border-color: #fed7aa;
        font-size: 22px;
        letter-spacing: -1px;
    }

    .subject-pill.english {
        background: #fff7ed;
        color: #9a3412;
        border-color: #fed7aa;
    }



    /* Font riêng cho ô trả lời ngắn: không dùng Times New Roman */
    .answer-sheet .short-input-row,
    .answer-sheet .short-input,
    .answer-sheet .clear-short-btn,
    .answer-sheet .digit-omr,
    .answer-sheet .digit-omr * {
        font-family: "Segoe UI", system-ui, -apple-system, BlinkMacSystemFont, Arial, sans-serif !important;
    }

    .short-input-row .short-input,
    .answer-sheet .short-input {
        font-weight: 700;
        letter-spacing: .1px;
    }

    .clear-short-btn {
        font-weight: 800;
    }



    /* Nút xóa phần trả lời ngắn: mềm và hiện đại hơn */
    .clear-short-btn {
        min-width: 74px;
        height: 46px;
        padding: 0 18px;
        border-radius: 14px;
        border: 1px solid #f3b5b5;
        background: linear-gradient(180deg, #fff6f6 0%, #ffecec 100%);
        color: #c24141;
        font-family: "Segoe UI", system-ui, -apple-system, BlinkMacSystemFont, Arial, sans-serif !important;
        font-weight: 800;
        font-size: 18px;
        letter-spacing: .1px;
        box-shadow: 0 8px 18px rgba(220, 38, 38, .08), inset 0 1px 0 rgba(255,255,255,.75);
        transition: all .18s ease;
    }

    .clear-short-btn:hover {
        background: linear-gradient(180deg, #fff1f1 0%, #ffe3e3 100%);
        border-color: #ee9b9b;
        color: #b91c1c;
        box-shadow: 0 10px 22px rgba(220, 38, 38, .12), inset 0 1px 0 rgba(255,255,255,.85);
        transform: translateY(-1px);
    }

    .clear-short-btn:active {
        transform: translateY(0);
        box-shadow: 0 5px 12px rgba(220, 38, 38, .10), inset 0 1px 0 rgba(255,255,255,.55);
    }

    html[data-theme="dark"] .clear-short-btn {
        border-color: #7f1d1d;
        background: linear-gradient(180deg, #2e1212 0%, #411515 100%);
        color: #fecaca;
        box-shadow: 0 8px 18px rgba(0,0,0,.22), inset 0 1px 0 rgba(255,255,255,.03);
    }

    html[data-theme="dark"] .clear-short-btn:hover {
        background: linear-gradient(180deg, #3a1717 0%, #521919 100%);
        border-color: #991b1b;
        color: #ffe4e6;
        box-shadow: 0 10px 22px rgba(0,0,0,.28), inset 0 1px 0 rgba(255,255,255,.04);
    }


    /* Nút xóa trả lời ngắn: bản nhỏ chỉ icon X */
    .clear-short-btn {
        width: 38px !important;
        min-width: 38px !important;
        height: 38px !important;
        padding: 0 !important;
        margin: 0 !important;
        border-radius: 50% !important;
        border: 1px solid #fca5a5 !important;
        background: #fff5f5 !important;
        color: #dc2626 !important;
        font-family: "Segoe UI", system-ui, -apple-system, BlinkMacSystemFont, Arial, sans-serif !important;
        font-size: 22px !important;
        line-height: 1 !important;
        font-weight: 700 !important;
        display: inline-flex !important;
        align-items: center !important;
        justify-content: center !important;
        box-shadow: none !important;
        transition: .15s ease !important;
    }

    .clear-short-btn:hover {
        background: #fee2e2 !important;
        border-color: #ef4444 !important;
        color: #b91c1c !important;
        transform: none !important;
        box-shadow: none !important;
    }

    .clear-short-btn:active {
        transform: scale(.94) !important;
    }

    html[data-theme="dark"] .clear-short-btn {
        background: #2a1111 !important;
        border-color: #7f1d1d !important;
        color: #fecaca !important;
        box-shadow: none !important;
    }

    html[data-theme="dark"] .clear-short-btn:hover {
        background: #3b1515 !important;
        border-color: #991b1b !important;
        color: #fee2e2 !important;
    }


    /* Nav link trong dark mode: chữ trắng rõ hơn */
    html[data-theme="dark"] .nav a,
    html[data-theme="dark"] .nav > div:last-child a {
        color: #ffffff !important;
    }


    /* Thanh chỉnh kích thước file đề PDF */
    .pdf-tools {
        display: flex;
        align-items: center;
        justify-content: flex-end;
        gap: 8px;
        margin-bottom: 10px;
        padding: 8px 10px;
        border: 1px solid #e5e7eb;
        border-radius: 14px;
        background: rgba(255,255,255,.88);
        box-shadow: 0 6px 18px rgba(15,23,42,.06);
    }

    .pdf-tools span {
        margin-right: auto;
        color: #64748b;
        font-size: 14px;
        font-weight: 700;
    }

    .pdf-size-btn {
        width: auto;
        height: 34px;
        margin: 0;
        padding: 0 12px;
        border-radius: 999px;
        border: 1px solid #cbd5e1;
        background: #ffffff;
        color: #334155;
        font-size: 13px;
        font-weight: 800;
        cursor: pointer;
    }

    .pdf-size-btn:hover {
        background: #eff6ff;
        border-color: #93c5fd;
        color: #1d4ed8;
    }

    .pdf-size-btn.active {
        background: #2563eb;
        border-color: #2563eb;
        color: #ffffff;
    }

    html[data-theme="dark"] .pdf-tools {
        background: #111827;
        border-color: #334155;
        box-shadow: 0 8px 24px rgba(0,0,0,.24);
    }

    html[data-theme="dark"] .pdf-tools span {
        color: #cbd5e1;
    }

    html[data-theme="dark"] .pdf-size-btn {
        background: #020617;
        border-color: #475569;
        color: #e5e7eb;
    }

    html[data-theme="dark"] .pdf-size-btn:hover {
        background: #172554;
        border-color: #60a5fa;
        color: #ffffff;
    }

    html[data-theme="dark"] .pdf-size-btn.active {
        background: #2563eb;
        border-color: #2563eb;
        color: #ffffff;
    }

    @media (max-width: 1080px) {
        .pdf-tools {
            justify-content: flex-start;
            flex-wrap: wrap;
        }
        .pdf-tools span {
            width: 100%;
            margin-right: 0;
        }
    }


    /* Bộ chỉnh độ rộng đề: chuyển sang dạng nổi, không chiếm diện tích đọc PDF */
    .pdf-floating-tools {
        position: fixed;
        left: 22px;
        top: 86px;
        z-index: 50;
        display: flex;
        align-items: center;
        gap: 6px;
        padding: 6px;
        border-radius: 999px;
        background: rgba(255,255,255,.82);
        border: 1px solid rgba(203,213,225,.8);
        box-shadow: 0 10px 26px rgba(15,23,42,.14);
        backdrop-filter: blur(10px);
    }

    .pdf-floating-tools .pdf-size-btn {
        width: auto;
        height: 30px;
        margin: 0;
        padding: 0 10px;
        border-radius: 999px;
        border: 1px solid transparent;
        background: transparent;
        color: #334155;
        font-size: 12px;
        font-weight: 800;
        cursor: pointer;
        box-shadow: none;
    }

    .pdf-floating-tools .pdf-size-btn:hover {
        background: #eff6ff;
        border-color: #bfdbfe;
        color: #1d4ed8;
    }

    .pdf-floating-tools .pdf-size-btn.active {
        background: #2563eb;
        border-color: #2563eb;
        color: #ffffff;
    }

    html[data-theme="dark"] .pdf-floating-tools {
        background: rgba(15,23,42,.78);
        border-color: rgba(71,85,105,.85);
        box-shadow: 0 10px 26px rgba(0,0,0,.28);
    }

    html[data-theme="dark"] .pdf-floating-tools .pdf-size-btn {
        color: #cbd5e1;
    }

    html[data-theme="dark"] .pdf-floating-tools .pdf-size-btn:hover {
        background: #172554;
        border-color: #3b82f6;
        color: #ffffff;
    }

    html[data-theme="dark"] .pdf-floating-tools .pdf-size-btn.active {
        background: #2563eb;
        border-color: #2563eb;
        color: #ffffff;
    }

    @media (max-width: 1080px) {
        .pdf-floating-tools {
            position: sticky;
            top: 8px;
            left: auto;
            width: fit-content;
            margin: 0 0 8px auto;
        }
    }


    /* Nút chỉnh độ rộng đề đặt ở panel phải, không che PDF */
    .sheet-top-actions,
    .result-head-side {
        display: flex;
        align-items: center;
        justify-content: flex-end;
        gap: 12px;
        flex-wrap: wrap;
    }

    .pdf-inline-tools {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        padding: 4px;
        border-radius: 999px;
        border: 1px solid #dbe4f0;
        background: rgba(255,255,255,.85);
    }

    .pdf-inline-tools .pdf-size-btn {
        width: auto;
        height: 30px;
        margin: 0;
        padding: 0 10px;
        border-radius: 999px;
        border: 1px solid transparent;
        background: transparent;
        color: #475569;
        font-size: 12px;
        font-weight: 800;
        cursor: pointer;
        box-shadow: none;
    }

    .pdf-inline-tools .pdf-size-btn:hover {
        background: #eff6ff;
        border-color: #bfdbfe;
        color: #1d4ed8;
    }

    .pdf-inline-tools .pdf-size-btn.active {
        background: #2563eb;
        border-color: #2563eb;
        color: #ffffff;
    }

    html[data-theme="dark"] .pdf-inline-tools {
        background: rgba(15,23,42,.75);
        border-color: #334155;
    }

    html[data-theme="dark"] .pdf-inline-tools .pdf-size-btn {
        color: #cbd5e1;
    }

    html[data-theme="dark"] .pdf-inline-tools .pdf-size-btn:hover {
        background: #172554;
        border-color: #3b82f6;
        color: #ffffff;
    }

    html[data-theme="dark"] .pdf-inline-tools .pdf-size-btn.active {
        background: #2563eb;
        border-color: #2563eb;
        color: #ffffff;
    }

    /* vô hiệu block floating cũ nếu còn */
    .pdf-floating-tools {
        display: none !important;
    }

    @media (max-width: 1080px) {
        .sheet-top-actions,
        .result-head-side {
            width: 100%;
            justify-content: space-between;
        }
    }


    /* Fix header phiếu trả lời + nút độ rộng đề không tràn chữ */
    .sheet-top,
    .review-headline {
        display: grid;
        grid-template-columns: minmax(0, 1fr) auto;
        align-items: start;
        gap: 14px;
    }

    .sheet-top-actions,
    .result-head-side {
        display: flex;
        align-items: center;
        justify-content: flex-end;
        gap: 10px;
        flex-wrap: nowrap;
        flex-shrink: 0;
    }

    .pdf-inline-tools {
        flex-shrink: 0;
        display: inline-flex;
        align-items: center;
        gap: 6px;
        padding: 4px;
        border-radius: 999px;
    }

    .pdf-inline-tools .pdf-size-btn {
        white-space: nowrap;
        word-break: keep-all;
        min-width: 58px;
        height: 32px;
        padding: 0 12px;
        font-size: 13px;
        line-height: 1;
        font-family: "Segoe UI", system-ui, -apple-system, BlinkMacSystemFont, Arial, sans-serif !important;
        letter-spacing: 0;
    }

    .timer,
    .review-score {
        flex-shrink: 0;
        white-space: nowrap;
    }

    /* Giảm khoảng trống thừa và giúp PDF dễ đọc hơn */
    .grid {
        grid-template-columns: minmax(0, 1.22fr) minmax(390px, 470px);
        gap: 14px;
    }

    .review-sheet-layout {
        grid-template-columns: minmax(0, 1.22fr) minmax(390px, 470px);
        gap: 14px;
    }

    .pdf,
    .review-pdf {
        display: block;
        width: 100%;
        height: calc(100vh - 92px);
        min-height: 700px;
    }

    /* Ở màn hình vừa thì xếp dọc để tránh vùng trống / lệch layout */
    @media (max-width: 1360px) {
        .grid,
        .review-sheet-layout {
            grid-template-columns: 1fr;
        }

        .pdf,
        .review-pdf {
            height: 72vh;
            min-height: 620px;
        }
    }

    @media (max-width: 900px) {
        .sheet-top,
        .review-headline {
            grid-template-columns: 1fr;
        }

        .sheet-top-actions,
        .result-head-side {
            width: 100%;
            justify-content: space-between;
            flex-wrap: wrap;
        }

        .pdf-inline-tools {
            order: 2;
        }

        .timer,
        .review-score {
            order: 1;
        }
    }

    @media (max-width: 560px) {
        .pdf-inline-tools {
            gap: 4px;
            padding: 3px;
        }

        .pdf-inline-tools .pdf-size-btn {
            min-width: 52px;
            height: 30px;
            padding: 0 10px;
            font-size: 12px;
        }
    }


    /* Fix layout PDF/phiếu: không bóp phiếu trả lời khi phóng đề */
    .grid,
    .review-sheet-layout {
        grid-template-columns: minmax(0, 1fr) minmax(520px, 540px);
        gap: 16px;
    }

    .grid > div,
    .review-sheet-layout > div {
        min-width: 0;
    }

    .pdf,
    .review-pdf {
        display: block;
        width: 100%;
        height: calc(100vh - 96px);
        min-height: 720px;
    }

    .pdf-stacked-mode .pdf,
    .pdf-stacked-mode .review-pdf {
        height: 82vh;
        min-height: 760px;
    }

    .sheet-top,
    .review-headline {
        display: flex !important;
        align-items: flex-start !important;
        justify-content: space-between !important;
        gap: 14px !important;
        flex-wrap: wrap !important;
    }

    .sheet-top > div:first-child,
    .review-headline > div:first-child {
        min-width: 260px;
        flex: 1 1 260px;
    }

    .sheet-top h1,
    .review-headline h1 {
        word-break: normal !important;
        overflow-wrap: normal !important;
        line-height: 1.15 !important;
    }

    .sheet-top-actions,
    .result-head-side {
        display: flex !important;
        align-items: center !important;
        justify-content: flex-end !important;
        gap: 10px !important;
        flex: 0 0 auto !important;
        flex-wrap: nowrap !important;
    }

    .pdf-inline-tools {
        flex: 0 0 auto !important;
        white-space: nowrap !important;
    }

    .pdf-inline-tools .pdf-size-btn {
        min-width: 64px !important;
        height: 32px !important;
        padding: 0 12px !important;
        white-space: nowrap !important;
        word-break: keep-all !important;
        overflow-wrap: normal !important;
        line-height: 1 !important;
        font-size: 13px !important;
        font-family: "Segoe UI", system-ui, -apple-system, BlinkMacSystemFont, Arial, sans-serif !important;
    }

    .timer,
    .review-score {
        flex: 0 0 auto !important;
        white-space: nowrap !important;
    }

    /* Ẩn style floating cũ nếu còn sót */
    .pdf-floating-tools {
        display: none !important;
    }

    @media (max-width: 1180px) {
        .grid,
        .review-sheet-layout {
            grid-template-columns: 1fr !important;
        }

        .pdf,
        .review-pdf {
            height: 74vh;
            min-height: 640px;
        }
    }

    @media (max-width: 620px) {
        .sheet-top-actions,
        .result-head-side {
            width: 100% !important;
            justify-content: space-between !important;
            flex-wrap: wrap !important;
        }

        .sheet-top > div:first-child,
        .review-headline > div:first-child {
            min-width: 0;
            flex-basis: 100%;
        }

        .pdf-inline-tools .pdf-size-btn {
            min-width: 56px !important;
            padding: 0 10px !important;
            font-size: 12px !important;
        }
    }


    /* Resize kéo thả giữa đề PDF và phiếu trả lời */
    .grid,
    .review-sheet-layout {
        grid-template-columns: minmax(420px, var(--pdf-col, 1fr)) 10px minmax(520px, 540px) !important;
        gap: 12px !important;
        align-items: start;
    }

    .pdf-pane,
    .answer-pane {
        min-width: 0;
    }

    .answer-pane {
        min-width: 520px;
    }

    .split-resizer {
        width: 10px;
        min-height: calc(100vh - 110px);
        border-radius: 999px;
        cursor: col-resize;
        background: transparent;
        position: sticky;
        top: 90px;
        transition: background .15s ease, box-shadow .15s ease;
    }

    .split-resizer::before {
        content: "";
        display: block;
        width: 4px;
        height: 72px;
        margin: 40vh auto 0;
        border-radius: 999px;
        background: #cbd5e1;
        opacity: .45;
    }

    .split-resizer:hover,
    body.resizing-pdf .split-resizer {
        background: rgba(37,99,235,.08);
        box-shadow: inset 0 0 0 1px rgba(37,99,235,.15);
    }

    .split-resizer:hover::before,
    body.resizing-pdf .split-resizer::before {
        background: #2563eb;
        opacity: 1;
    }

    html[data-theme="dark"] .split-resizer::before {
        background: #64748b;
    }

    html[data-theme="dark"] .split-resizer:hover,
    html[data-theme="dark"] body.resizing-pdf .split-resizer {
        background: rgba(96,165,250,.10);
        box-shadow: inset 0 0 0 1px rgba(96,165,250,.22);
    }

    html[data-theme="dark"] .split-resizer:hover::before,
    html[data-theme="dark"] body.resizing-pdf .split-resizer::before {
        background: #60a5fa;
    }

    .pdf,
    .review-pdf {
        width: 100%;
        height: calc(100vh - 96px);
        min-height: 720px;
    }

    /* Nút Vừa/To/Rất to giờ là preset nhanh, còn kéo thả là chính */
    .pdf-inline-tools .pdf-size-btn {
        white-space: nowrap !important;
        word-break: keep-all !important;
        overflow-wrap: normal !important;
        min-width: 58px !important;
        height: 32px !important;
        padding: 0 12px !important;
        font-family: "Segoe UI", system-ui, -apple-system, BlinkMacSystemFont, Arial, sans-serif !important;
        font-size: 13px !important;
        line-height: 1 !important;
    }

    .sheet-top,
    .review-headline {
        display: flex !important;
        align-items: flex-start !important;
        justify-content: space-between !important;
        gap: 14px !important;
        flex-wrap: wrap !important;
    }

    .sheet-top > div:first-child,
    .review-headline > div:first-child {
        min-width: 260px;
        flex: 1 1 260px;
    }

    .sheet-top h1,
    .review-headline h1 {
        word-break: normal !important;
        overflow-wrap: normal !important;
        line-height: 1.15 !important;
    }

    @media (max-width: 1180px) {
        .grid,
        .review-sheet-layout {
            grid-template-columns: 1fr !important;
        }

        .split-resizer {
            display: none;
        }

        .answer-pane {
            min-width: 0;
        }

        .pdf,
        .review-pdf {
            height: 74vh;
            min-height: 640px;
        }
    }


    /* ===== Fix cuối: bỏ khoảng trống dư, PDF bám trái, kéo không làm nát layout ===== */
    .container:has(.grid),
    .container:has(.review-sheet-layout) {
        max-width: none !important;
        width: 100% !important;
        margin: 0 !important;
        padding: 14px !important;
    }

    .grid,
    .review-sheet-layout {
        width: 100% !important;
        max-width: none !important;
        grid-template-columns: minmax(520px, var(--pdf-col, calc(100vw - 610px))) 10px minmax(520px, 560px) !important;
        gap: 12px !important;
        align-items: start !important;
        justify-content: start !important;
    }

    .pdf-pane,
    .answer-pane {
        min-width: 0 !important;
        width: 100% !important;
    }

    .answer-pane {
        min-width: 520px !important;
    }

    .pdf,
    .review-pdf {
        width: 100% !important;
        max-width: none !important;
        display: block !important;
        margin: 0 !important;
        height: calc(100vh - 92px) !important;
        min-height: 720px !important;
        border-radius: 16px !important;
    }

    .split-resizer {
        align-self: stretch !important;
        min-height: calc(100vh - 92px) !important;
        position: sticky !important;
        top: 82px !important;
    }

    .sheet-top,
    .review-headline {
        display: flex !important;
        align-items: flex-start !important;
        justify-content: space-between !important;
        gap: 12px !important;
        flex-wrap: wrap !important;
    }

    .sheet-top > div:first-child,
    .review-headline > div:first-child {
        min-width: 0 !important;
        flex: 1 1 240px !important;
    }

    .sheet-top h1,
    .review-headline h1 {
        word-break: normal !important;
        overflow-wrap: normal !important;
        line-height: 1.15 !important;
    }

    .sheet-top-actions,
    .result-head-side {
        flex: 0 0 auto !important;
        display: flex !important;
        align-items: center !important;
        justify-content: flex-end !important;
        gap: 10px !important;
        flex-wrap: nowrap !important;
    }

    .pdf-inline-tools,
    .timer,
    .review-score {
        flex-shrink: 0 !important;
        white-space: nowrap !important;
    }

    .pdf-inline-tools .pdf-size-btn {
        white-space: nowrap !important;
        word-break: keep-all !important;
        min-width: 58px !important;
    }

    @media (max-width: 1180px) {
        .grid,
        .review-sheet-layout {
            grid-template-columns: 1fr !important;
            gap: 12px !important;
        }

        .split-resizer {
            display: none !important;
        }

        .answer-pane {
            min-width: 0 !important;
        }

        .pdf,
        .review-pdf {
            height: 76vh !important;
            min-height: 620px !important;
        }
    }

    @media (max-width: 640px) {
        .container:has(.grid),
        .container:has(.review-sheet-layout) {
            padding: 8px !important;
        }

        .sheet-top-actions,
        .result-head-side {
            width: 100% !important;
            justify-content: space-between !important;
            flex-wrap: wrap !important;
        }
    }


    /* Bản mới: chỉ kéo thanh giữa, bỏ nút Vừa/To/Rất to */
    .pdf-inline-tools,
    .pdf-floating-tools,
    .pdf-tools {
        display: none !important;
    }

    .container:has(.grid),
    .container:has(.review-sheet-layout) {
        max-width: none !important;
        width: 100% !important;
        margin: 0 !important;
        padding: 14px !important;
    }

    .grid,
    .review-sheet-layout {
        width: 100% !important;
        max-width: none !important;
        grid-template-columns: minmax(520px, var(--pdf-col, calc(100vw - 640px))) 10px minmax(560px, 590px) !important;
        gap: 12px !important;
        align-items: start !important;
        justify-content: start !important;
    }

    .pdf-pane,
    .answer-pane {
        min-width: 0 !important;
        width: 100% !important;
    }

    .answer-pane {
        min-width: 560px !important;
    }

    .pdf,
    .review-pdf {
        width: 100% !important;
        max-width: none !important;
        display: block !important;
        margin: 0 !important;
        height: calc(100vh - 92px) !important;
        min-height: 720px !important;
        border-radius: 16px !important;
    }

    .split-resizer {
        display: block !important;
        width: 10px !important;
        align-self: stretch !important;
        min-height: calc(100vh - 92px) !important;
        position: sticky !important;
        top: 82px !important;
        cursor: col-resize !important;
    }

    .sheet-top-actions,
    .result-head-side {
        flex: 0 0 auto !important;
        display: flex !important;
        align-items: center !important;
        justify-content: flex-end !important;
        gap: 10px !important;
        flex-wrap: nowrap !important;
    }

    .timer,
    .review-score {
        flex-shrink: 0 !important;
        white-space: nowrap !important;
    }

    @media (max-width: 1180px) {
        .grid,
        .review-sheet-layout {
            grid-template-columns: 1fr !important;
            gap: 12px !important;
        }

        .split-resizer {
            display: none !important;
        }

        .answer-pane {
            min-width: 0 !important;
        }

        .pdf,
        .review-pdf {
            height: 76vh !important;
            min-height: 620px !important;
        }
    }


    /* Bản chốt: bỏ thanh kéo, layout cố định gọn */
    .split-resizer {
        display: none !important;
    }

    .container:has(.grid),
    .container:has(.review-sheet-layout) {
        max-width: none !important;
        width: 100% !important;
        margin: 0 !important;
        padding: 14px !important;
    }

    .grid,
    .review-sheet-layout {
        width: 100% !important;
        max-width: none !important;
        display: grid !important;
        grid-template-columns: minmax(0, 1fr) minmax(560px, 590px) !important;
        gap: 14px !important;
        align-items: start !important;
    }

    .pdf-pane,
    .answer-pane {
        min-width: 0 !important;
        width: 100% !important;
    }

    .answer-pane {
        min-width: 560px !important;
    }

    .pdf,
    .review-pdf {
        width: 100% !important;
        max-width: none !important;
        display: block !important;
        margin: 0 !important;
        height: calc(100vh - 92px) !important;
        min-height: 720px !important;
        border-radius: 16px !important;
    }

    .sheet-top,
    .review-headline {
        display: flex !important;
        align-items: flex-start !important;
        justify-content: space-between !important;
        gap: 12px !important;
        flex-wrap: wrap !important;
    }

    .sheet-top > div:first-child,
    .review-headline > div:first-child {
        min-width: 0 !important;
        flex: 1 1 240px !important;
    }

    .sheet-top h1,
    .review-headline h1 {
        word-break: normal !important;
        overflow-wrap: normal !important;
        line-height: 1.15 !important;
    }

    .sheet-top-actions,
    .result-head-side {
        flex: 0 0 auto !important;
        display: flex !important;
        align-items: center !important;
        justify-content: flex-end !important;
        gap: 10px !important;
        flex-wrap: nowrap !important;
    }

    .pdf-inline-tools,
    .pdf-floating-tools,
    .pdf-tools {
        display: none !important;
    }

    .timer,
    .review-score {
        flex-shrink: 0 !important;
        white-space: nowrap !important;
    }

    @media (max-width: 1180px) {
        .grid,
        .review-sheet-layout {
            grid-template-columns: 1fr !important;
            gap: 12px !important;
        }

        .answer-pane {
            min-width: 0 !important;
        }

        .pdf,
        .review-pdf {
            height: 76vh !important;
            min-height: 620px !important;
        }
    }


    /* ===== Fix riêng badge + điểm trong dark mode, không đổi giao diện phiếu ===== */
    [data-theme="dark"] .mini-stats span,
    [data-theme="dark"] .tiny-summary span,
    [data-theme="dark"] .pill-count {
        background: #1e293b !important;
        color: #ffffff !important;
        border-color: #475569 !important;
    }

    [data-theme="dark"] .mini-stats span b,
    [data-theme="dark"] .tiny-summary span b,
    [data-theme="dark"] .pill-count b {
        color: #ffffff !important;
    }

    [data-theme="dark"] .review-score,
    [data-theme="dark"] .score-number,
    [data-theme="dark"] .score-badge .score-main,
    [data-theme="dark"] .score-main,
    [data-theme="dark"] .result-line .score {
        color: #60a5fa !important;
        text-shadow: 0 0 10px rgba(96,165,250,.16);
    }

    [data-theme="dark"] .score-sub,
    [data-theme="dark"] .mini-stats,
    [data-theme="dark"] .tiny-summary {
        color: #cbd5e1 !important;
    }


    /* ===== Layout chốt: bỏ nút resize/thanh kéo, PDF trái + phiếu phải ổn định ===== */
    .split-resizer,
    .pdf-inline-tools,
    .pdf-floating-tools,
    .pdf-tools {
        display: none !important;
    }

    .container:has(.grid),
    .container:has(.review-sheet-layout) {
        max-width: none !important;
        width: 100% !important;
        margin: 0 !important;
        padding: 14px !important;
    }

    .grid,
    .review-sheet-layout {
        width: 100% !important;
        max-width: none !important;
        display: grid !important;
        grid-template-columns: minmax(0, 1fr) minmax(560px, 590px) !important;
        gap: 14px !important;
        align-items: start !important;
    }

    .pdf-pane,
    .answer-pane {
        min-width: 0 !important;
        width: 100% !important;
    }

    .answer-pane {
        min-width: 560px !important;
    }

    .pdf,
    .review-pdf {
        width: 100% !important;
        max-width: none !important;
        display: block !important;
        margin: 0 !important;
        height: calc(100vh - 92px) !important;
        min-height: 720px !important;
        border-radius: 16px !important;
    }

    .timer,
    .review-score {
        flex-shrink: 0 !important;
        white-space: nowrap !important;
    }

    @media (max-width: 1180px) {
        .grid,
        .review-sheet-layout {
            grid-template-columns: 1fr !important;
            gap: 12px !important;
        }

        .answer-pane {
            min-width: 0 !important;
        }

        .pdf,
        .review-pdf {
            height: 76vh !important;
            min-height: 620px !important;
        }
    }


    /* Nav chữ đậm hơn + countdown responsive */
    .nav a,
    .nav .nav-home-link {
        font-weight: 850 !important;
        letter-spacing: .15px;
    }

    [data-theme="dark"] .exam-countdown {
        background: #111827 !important;
        color: #e5e7eb !important;
        border-color: #334155 !important;
    }

    [data-theme="light"] .exam-countdown {
        background: #f8fafc !important;
        color: #111827 !important;
        border-color: #d1d5db !important;
    }

    [data-theme="light"] .exam-countdown b {
        color: #111827 !important;
    }

    @media (max-width: 760px) {
        .nav {
            grid-template-columns: 1fr !important;
            text-align: center !important;
        }

        .nav > div:last-child {
            justify-content: center !important;
            flex-wrap: wrap !important;
        }

        .exam-countdown {
            font-size: 13px !important;
            padding: 7px 11px !important;
        }
    }


    /* ===== Countdown THPTQG góc phải nav ===== */
    .nav {
        display: flex !important;
        justify-content: space-between !important;
        align-items: center !important;
        gap: 16px !important;
    }

    .nav > b,
    .nav > div:first-child {
        font-weight: 850 !important;
        letter-spacing: .15px;
        flex: 0 0 auto;
    }

    .nav-actions,
    .nav > div:last-child {
        margin-left: auto !important;
        display: flex !important;
        justify-content: flex-end !important;
        align-items: center !important;
        gap: 10px !important;
        flex-wrap: wrap !important;
    }

    .nav a {
        font-weight: 850 !important;
    }

    .exam-countdown {
        display: inline-flex !important;
        align-items: center !important;
        justify-content: center !important;
        justify-self: auto !important;
        padding: 8px 13px !important;
        border-radius: 999px !important;
        border: 1px solid rgba(148, 163, 184, .35) !important;
        background: rgba(15, 23, 42, .72) !important;
        color: #e5e7eb !important;
        font-weight: 850 !important;
        font-size: 14px !important;
        line-height: 1 !important;
        white-space: nowrap !important;
        margin: 0 !important;
        box-shadow: inset 0 1px 0 rgba(255,255,255,.06);
    }

    .exam-countdown b {
        color: #ffffff !important;
        margin: 0 3px;
    }

    [data-theme="light"] .exam-countdown {
        background: #f8fafc !important;
        color: #111827 !important;
        border-color: #d1d5db !important;
    }

    [data-theme="light"] .exam-countdown b {
        color: #111827 !important;
    }

    [data-theme="dark"] .exam-countdown {
        background: #111827 !important;
        color: #e5e7eb !important;
        border-color: #334155 !important;
    }

    @media (max-width: 760px) {
        .nav {
            flex-wrap: wrap !important;
            text-align: left !important;
        }

        .nav-actions,
        .nav > div:last-child {
            width: 100% !important;
            justify-content: flex-start !important;
        }

        .exam-countdown {
            font-size: 13px !important;
            padding: 7px 11px !important;
        }
    }


    /* ===== SuperAdmin logs ===== */
    .super-stats {
        display: grid;
        grid-template-columns: repeat(3, minmax(0, 1fr));
        gap: 14px;
        margin-bottom: 16px;
    }

    .super-stat {
        background: white;
        border: 1px solid #e5e7eb;
        border-radius: 16px;
        padding: 16px;
        box-shadow: 0 8px 24px rgba(0,0,0,.06);
    }

    .super-stat span {
        color: #64748b;
        font-weight: 700;
        font-size: 14px;
    }

    .super-stat b {
        display: block;
        font-size: 30px;
        margin-top: 6px;
    }

    .super-filter {
        display: grid;
        grid-template-columns: 220px 1fr auto;
        gap: 12px;
        align-items: end;
    }

    .super-filter input,
    .super-filter select,
    .super-filter button {
        margin-bottom: 0;
    }

    .super-filter-actions {
        display: flex;
        gap: 8px;
        align-items: center;
    }

    .link-btn {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        min-height: 44px;
        padding: 0 14px;
        border-radius: 10px;
        border: 1px solid #d1d5db;
        background: white;
        color: #111827;
        font-weight: 800;
        white-space: nowrap;
    }

    .super-head-row {
        display: flex;
        justify-content: space-between;
        gap: 14px;
        align-items: flex-start;
    }

    .super-head-row form {
        flex: 0 0 auto;
    }

    .super-table-wrap {
        overflow: auto;
        border: 1px solid #e5e7eb;
        border-radius: 14px;
    }

    .super-table {
        width: 100%;
        border-collapse: collapse;
        min-width: 980px;
        background: white;
    }

    .super-table th,
    .super-table td {
        padding: 11px 12px;
        border-bottom: 1px solid #e5e7eb;
        text-align: left;
        vertical-align: top;
        font-size: 14px;
    }

    .super-table th {
        background: #f8fafc;
        color: #475569;
        font-weight: 800;
        white-space: nowrap;
    }

    .super-table tr:last-child td {
        border-bottom: 0;
    }

    .score-cell {
        font-weight: 900;
        color: #2563eb;
        white-space: nowrap;
    }

    .nowrap {
        white-space: nowrap;
    }

    .ua-cell {
        max-width: 360px;
        color: #64748b;
        font-size: 12px !important;
        word-break: break-word;
    }

    [data-theme="dark"] .super-stat,
    [data-theme="dark"] .super-table,
    [data-theme="dark"] .link-btn {
        background: #0f172a !important;
        color: #e5e7eb !important;
        border-color: #334155 !important;
    }

    [data-theme="dark"] .super-stat span,
    [data-theme="dark"] .ua-cell {
        color: #94a3b8 !important;
    }

    [data-theme="dark"] .super-table-wrap {
        border-color: #334155 !important;
    }

    [data-theme="dark"] .super-table th {
        background: #111827 !important;
        color: #cbd5e1 !important;
        border-color: #334155 !important;
    }

    [data-theme="dark"] .super-table td {
        border-color: #334155 !important;
    }

    [data-theme="dark"] .score-cell {
        color: #60a5fa !important;
    }

    @media (max-width: 800px) {
        .super-stats {
            grid-template-columns: 1fr;
        }

        .super-filter {
            grid-template-columns: 1fr;
        }

        .super-head-row {
            flex-direction: column;
        }
    }


    /* ===== Tinh chỉnh header phiếu trả lời ===== */
    .exam-sheet-top {
        margin-bottom: 6px !important;
        gap: 10px !important;
        align-items: center !important;
    }

    .exam-sheet-top .sheet-title-row {
        min-width: 0 !important;
        flex: 1 1 auto !important;
    }

    .exam-sheet-top h1 {
        margin: 0 !important;
        font-size: clamp(24px, 2vw, 30px) !important;
        line-height: 1.12 !important;
        font-weight: 850 !important;
        letter-spacing: -0.01em;
    }

    .exam-sheet-info {
        display: block !important;
        margin: 0 0 10px 0 !important;
        font-size: 15px !important;
        line-height: 1.45 !important;
        max-width: none !important;
        white-space: normal !important;
    }

    .answer-sheet {
        margin-top: 8px !important;
    }

    @media (max-width: 1180px) {
        .exam-sheet-top {
            align-items: flex-start !important;
        }

        .exam-sheet-top h1 {
            font-size: 25px !important;
        }

        .exam-sheet-info {
            margin-bottom: 8px !important;
        }
    }

    @media (max-width: 640px) {
        .exam-sheet-top h1 {
            font-size: 23px !important;
        }

        .exam-sheet-info {
            font-size: 14px !important;
            line-height: 1.4 !important;
        }
    }


    /* ===== Giảm size chữ Phiếu trả lời lần 2 ===== */
    .exam-sheet-top h1,
    .sheet-top.exam-sheet-top h1 {
        font-size: 24px !important;
        line-height: 1.12 !important;
        font-weight: 850 !important;
        margin: 0 !important;
    }

    @media (max-width: 640px) {
        .exam-sheet-top h1,
        .sheet-top.exam-sheet-top h1 {
            font-size: 22px !important;
        }
    }


    /* ===== Admin edit gọn hơn ===== */
    .admin-edit-layout {
        display: grid;
        grid-template-columns: minmax(0, 1fr);
        gap: 14px;
    }

    .admin-tools-grid {
        display: grid;
        grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
        gap: 14px;
        align-items: stretch;
    }

    .compact-admin-card {
        padding: 16px !important;
    }

    .compact-admin-card h2,
    .compact-admin-card h3 {
        margin-top: 0;
        margin-bottom: 10px;
    }

    .replace-file-row {
        display: grid;
        grid-template-columns: minmax(0, 1fr) auto;
        gap: 10px;
        align-items: end;
    }

    .replace-file-row input,
    .replace-file-row button {
        margin-bottom: 0;
    }

    .admin-answer-section {
        border: 1px solid #e5e7eb;
        border-radius: 16px;
        padding: 14px;
        margin-bottom: 14px;
        background: #f8fafc;
    }

    .admin-answer-section h2 {
        margin: 0 0 12px;
        font-size: 20px;
    }

    .admin-answer-grid.compact {
        grid-template-columns: repeat(auto-fill, minmax(112px, 1fr));
        gap: 8px;
    }

    .admin-answer-grid.compact .mini {
        padding: 8px;
        border-radius: 10px;
    }

    .admin-answer-grid.compact .mini b {
        font-size: 13px;
    }

    .admin-answer-grid.compact select,
    .admin-answer-grid.compact input {
        padding: 8px 9px;
        margin: 6px 0 0;
        font-size: 14px;
        border-radius: 9px;
    }

    .admin-tf-grid {
        display: grid;
        grid-template-columns: repeat(auto-fill, minmax(210px, 1fr));
        gap: 8px;
    }

    .admin-tf-card {
        border: 1px solid #e5e7eb;
        border-radius: 12px;
        padding: 9px;
        background: white;
    }

    .admin-tf-card b {
        display: block;
        margin-bottom: 7px;
    }

    .admin-tf-row {
        display: grid;
        grid-template-columns: 24px 1fr;
        gap: 6px;
        align-items: center;
        margin-bottom: 5px;
    }

    .admin-tf-row span {
        font-weight: 800;
        text-align: center;
    }

    .admin-tf-row select {
        margin: 0;
        padding: 7px 8px;
        font-size: 13px;
        border-radius: 8px;
    }

    details.gemini-details {
        border: 1px solid #e5e7eb;
        border-radius: 14px;
        background: #f8fafc;
        overflow: hidden;
    }

    details.gemini-details summary {
        cursor: pointer;
        list-style: none;
        padding: 13px 14px;
        font-weight: 850;
        display: flex;
        justify-content: space-between;
        align-items: center;
        gap: 10px;
    }

    details.gemini-details summary::-webkit-details-marker {
        display: none;
    }

    details.gemini-details summary::after {
        content: "Mở";
        font-size: 13px;
        padding: 5px 9px;
        border-radius: 999px;
        background: #e0ecff;
        color: #1d4ed8;
    }

    details.gemini-details[open] summary::after {
        content: "Đóng";
    }

    details.gemini-details pre {
        margin: 0;
        border-radius: 0;
        max-height: 360px;
    }

    [data-theme="dark"] .admin-answer-section,
    [data-theme="dark"] details.gemini-details {
        background: #0f172a !important;
        border-color: #334155 !important;
    }

    [data-theme="dark"] .admin-tf-card,
    [data-theme="dark"] .admin-answer-grid.compact .mini {
        background: #111827 !important;
        border-color: #334155 !important;
    }

    [data-theme="dark"] details.gemini-details summary::after {
        background: #1e293b;
        color: #bfdbfe;
    }

    @media (max-width: 860px) {
        .admin-tools-grid,
        .replace-file-row {
            grid-template-columns: 1fr;
        }
    }


    /* ===== Đánh dấu đề đã làm ở trang chủ ===== */
    .exam-card-done {
        position: relative;
        border: 1px solid #bbf7d0 !important;
        background: linear-gradient(180deg, #ffffff 0%, #f0fdf4 100%) !important;
    }

    .exam-row-head {
        display: flex;
        align-items: flex-start;
        justify-content: space-between;
        gap: 12px;
    }

    .exam-row-main {
        min-width: 0;
        flex: 1 1 auto;
    }

    .done-badge {
        flex: 0 0 auto;
        display: inline-flex;
        align-items: center;
        gap: 6px;
        padding: 7px 10px;
        border-radius: 999px;
        background: #dcfce7;
        color: #166534;
        border: 1px solid #86efac;
        font-size: 13px;
        font-weight: 900;
        white-space: nowrap;
    }

    .done-meta {
        margin-top: 8px;
        display: flex;
        flex-wrap: wrap;
        gap: 7px;
    }

    .done-meta span {
        display: inline-flex;
        align-items: center;
        padding: 5px 8px;
        border-radius: 999px;
        background: #ecfdf5;
        color: #166534;
        border: 1px solid #bbf7d0;
        font-size: 12px;
        font-weight: 800;
    }

    [data-theme="dark"] .exam-card-done {
        background: linear-gradient(180deg, #0f172a 0%, #052e16 100%) !important;
        border-color: #166534 !important;
    }

    [data-theme="dark"] .done-badge {
        background: #052e16 !important;
        color: #86efac !important;
        border-color: #16a34a !important;
    }

    [data-theme="dark"] .done-meta span {
        background: #052e16 !important;
        color: #bbf7d0 !important;
        border-color: #166534 !important;
    }

    @media (max-width: 640px) {
        .exam-row-head {
            flex-direction: column;
            align-items: flex-start;
        }
    }


    /* ===== Admin đổi tên đề ===== */
    .rename-title-form {
        display: grid;
        grid-template-columns: minmax(0, 1fr) auto;
        gap: 10px;
        align-items: end;
        margin-top: 12px;
    }

    .rename-title-form input,
    .rename-title-form button {
        margin-bottom: 0;
    }

    .rename-title-form button {
        white-space: nowrap;
    }

    @media (max-width: 700px) {
        .rename-title-form {
            grid-template-columns: 1fr;
        }
    }


    /* ===== Visitor count trang chủ ===== */
    .home-stats {
        display: flex;
        flex-wrap: wrap;
        gap: 10px;
        margin-top: 14px;
    }

    .home-stat-pill {
        display: inline-flex;
        align-items: center;
        gap: 7px;
        padding: 8px 12px;
        border-radius: 999px;
        background: #f8fafc;
        border: 1px solid #e5e7eb;
        color: #334155;
        font-weight: 850;
        font-size: 14px;
    }

    .home-stat-pill b {
        color: #111827;
        font-size: 15px;
    }

    [data-theme="dark"] .home-stat-pill {
        background: #111827 !important;
        border-color: #334155 !important;
        color: #cbd5e1 !important;
    }

    [data-theme="dark"] .home-stat-pill b {
        color: #f8fafc !important;
    }


    /* ===== Mobile first-visit modal ===== */
    .mobile-tip-backdrop {
        position: fixed;
        inset: 0;
        z-index: 9999;
        display: none;
        align-items: center;
        justify-content: center;
        padding: 18px;
        background: rgba(15, 23, 42, .62);
        backdrop-filter: blur(8px);
    }

    .mobile-tip-backdrop.show {
        display: flex;
    }

    .mobile-tip-box {
        width: min(420px, 100%);
        border-radius: 22px;
        background: white;
        color: #111827;
        padding: 22px;
        box-shadow: 0 24px 80px rgba(0,0,0,.28);
        border: 1px solid #e5e7eb;
        animation: mobileTipIn .18s ease-out;
    }

    @keyframes mobileTipIn {
        from {
            opacity: 0;
            transform: translateY(10px) scale(.98);
        }
        to {
            opacity: 1;
            transform: translateY(0) scale(1);
        }
    }

    .mobile-tip-icon {
        width: 48px;
        height: 48px;
        border-radius: 16px;
        display: grid;
        place-items: center;
        background: #eff6ff;
        color: #2563eb;
        font-size: 25px;
        margin-bottom: 12px;
    }

    .mobile-tip-box h2 {
        margin: 0 0 8px;
        font-size: 22px;
        line-height: 1.2;
    }

    .mobile-tip-box p {
        margin: 0;
        color: #475569;
        line-height: 1.55;
        font-size: 15px;
    }

    .mobile-tip-actions {
        display: grid;
        grid-template-columns: 1fr;
        gap: 8px;
        margin-top: 16px;
    }

    .mobile-tip-actions button {
        margin: 0;
    }

    .mobile-tip-secondary {
        background: transparent !important;
        color: #475569 !important;
        border: 1px solid #d1d5db !important;
    }

    .mobile-tip-secondary:hover {
        background: #f8fafc !important;
    }

    [data-theme="dark"] .mobile-tip-box {
        background: #0f172a !important;
        color: #f8fafc !important;
        border-color: #334155 !important;
    }

    [data-theme="dark"] .mobile-tip-box p {
        color: #cbd5e1 !important;
    }

    [data-theme="dark"] .mobile-tip-icon {
        background: #1e293b !important;
        color: #93c5fd !important;
    }

    [data-theme="dark"] .mobile-tip-secondary {
        color: #cbd5e1 !important;
        border-color: #475569 !important;
    }

    [data-theme="dark"] .mobile-tip-secondary:hover {
        background: #111827 !important;
    }


    /* ===== Local done marker fallback ===== */
    .local-done-badge {
        flex: 0 0 auto;
        display: inline-flex;
        align-items: center;
        gap: 6px;
        padding: 7px 10px;
        border-radius: 999px;
        background: #dcfce7;
        color: #166534;
        border: 1px solid #86efac;
        font-size: 13px;
        font-weight: 900;
        white-space: nowrap;
        margin-left: 8px;
    }

    .local-done-meta {
        margin-top: 8px;
        display: flex;
        flex-wrap: wrap;
        gap: 7px;
    }

    .local-done-meta span {
        display: inline-flex;
        align-items: center;
        padding: 5px 8px;
        border-radius: 999px;
        background: #ecfdf5;
        color: #166534;
        border: 1px solid #bbf7d0;
        font-size: 12px;
        font-weight: 800;
    }

    [data-theme="dark"] .local-done-badge {
        background: #052e16 !important;
        color: #86efac !important;
        border-color: #16a34a !important;
    }

    [data-theme="dark"] .local-done-meta span {
        background: #052e16 !important;
        color: #bbf7d0 !important;
        border-color: #166534 !important;
    }


    /* ===== Admin sắp xếp thứ tự đề ===== */
    .order-actions {
        display: flex;
        gap: 6px;
        flex-wrap: wrap;
        margin-top: 10px;
    }

    .order-actions form {
        margin: 0;
    }

    .order-actions button {
        margin: 0;
        width: auto;
        padding: 7px 10px;
        border-radius: 999px;
        font-size: 13px;
        background: #eef2ff;
        color: #3730a3;
        border: 1px solid #c7d2fe;
    }

    .order-actions button:hover {
        background: #e0e7ff;
    }

    [data-theme="dark"] .order-actions button {
        background: #1e293b !important;
        color: #c7d2fe !important;
        border-color: #475569 !important;
    }

    [data-theme="dark"] .order-actions button:hover {
        background: #334155 !important;
    }


    /* ===== Best seller exam badge ===== */
    .best-seller-badge {
        flex: 0 0 auto;
        display: inline-flex;
        align-items: center;
        gap: 6px;
        padding: 7px 10px;
        border-radius: 999px;
        background: linear-gradient(135deg, #f59e0b, #ef4444);
        color: #ffffff;
        border: 1px solid rgba(255,255,255,.25);
        font-size: 12px;
        font-weight: 950;
        letter-spacing: .25px;
        white-space: nowrap;
        box-shadow: 0 8px 22px rgba(245, 158, 11, .22);
    }

    .best-seller-card {
        border-color: rgba(245, 158, 11, .55) !important;
        background:
            radial-gradient(circle at top right, rgba(245, 158, 11, .12), transparent 32%),
            var(--card-bg, white) !important;
    }

    .exam-popularity {
        margin-top: 8px;
        display: inline-flex;
        align-items: center;
        gap: 6px;
        padding: 5px 8px;
        border-radius: 999px;
        background: #fff7ed;
        color: #9a3412;
        border: 1px solid #fed7aa;
        font-size: 12px;
        font-weight: 850;
    }

    [data-theme="dark"] .best-seller-card {
        background:
            radial-gradient(circle at top right, rgba(245, 158, 11, .16), transparent 35%),
            #0f172a !important;
        border-color: rgba(245, 158, 11, .45) !important;
    }

    [data-theme="dark"] .exam-popularity {
        background: #431407 !important;
        color: #fed7aa !important;
        border-color: #9a3412 !important;
    }


    /* ===== Fix: đề đã làm vẫn giữ nền xanh kể cả khi là BEST SELLER ===== */
    .exam-card-done.best-seller-card {
        border-color: #166534 !important;
        background:
            radial-gradient(circle at top right, rgba(245, 158, 11, .16), transparent 30%),
            linear-gradient(180deg, #ffffff 0%, #f0fdf4 100%) !important;
    }

    [data-theme="dark"] .exam-card-done.best-seller-card {
        border-color: #166534 !important;
        background:
            radial-gradient(circle at top right, rgba(245, 158, 11, .18), transparent 34%),
            linear-gradient(180deg, #0f172a 0%, #052e16 100%) !important;
    }

    .exam-card-done .done-badge,
    .exam-card-done .done-meta span,
    .exam-card-done .local-done-badge,
    .exam-card-done .local-done-meta span {
        box-shadow: 0 0 0 1px rgba(34, 197, 94, .12);
    }


    /* ===== FINAL OVERRIDE: giữ nền xanh cho đề đã làm ===== */
    .card.exam-card-done,
    .exam-card.exam-card-done,
    .card.exam-card-done.best-seller-card,
    .exam-card.exam-card-done.best-seller-card {
        border-color: #166534 !important;
        background:
            radial-gradient(circle at top right, rgba(245, 158, 11, .14), transparent 32%),
            linear-gradient(180deg, #0f172a 0%, #052e16 100%) !important;
    }

    [data-theme="light"] .card.exam-card-done,
    [data-theme="light"] .exam-card.exam-card-done,
    [data-theme="light"] .card.exam-card-done.best-seller-card,
    [data-theme="light"] .exam-card.exam-card-done.best-seller-card {
        border-color: #86efac !important;
        background:
            radial-gradient(circle at top right, rgba(245, 158, 11, .12), transparent 32%),
            linear-gradient(180deg, #ffffff 0%, #f0fdf4 100%) !important;
    }

    [data-theme="dark"] .card.exam-card-done,
    [data-theme="dark"] .exam-card.exam-card-done,
    [data-theme="dark"] .card.exam-card-done.best-seller-card,
    [data-theme="dark"] .exam-card.exam-card-done.best-seller-card {
        border-color: #166534 !important;
        background:
            radial-gradient(circle at top right, rgba(245, 158, 11, .16), transparent 34%),
            linear-gradient(180deg, #0f172a 0%, #052e16 100%) !important;
    }


    /* ===== FORCE: card có badge Đã làm luôn xanh ===== */
    .card.exam-card-done {
        border-color: #166534 !important;
        background:
            radial-gradient(circle at top right, rgba(34, 197, 94, .10), transparent 35%),
            linear-gradient(180deg, #0f172a 0%, #052e16 100%) !important;
    }

    .card.exam-card-done.best-seller-card {
        border-color: #166534 !important;
        background:
            radial-gradient(circle at top right, rgba(245, 158, 11, .16), transparent 34%),
            linear-gradient(180deg, #0f172a 0%, #052e16 100%) !important;
    }

    [data-theme="light"] .card.exam-card-done {
        border-color: #86efac !important;
        background:
            radial-gradient(circle at top right, rgba(34, 197, 94, .10), transparent 35%),
            linear-gradient(180deg, #ffffff 0%, #f0fdf4 100%) !important;
    }

    [data-theme="light"] .card.exam-card-done.best-seller-card {
        border-color: #86efac !important;
        background:
            radial-gradient(circle at top right, rgba(245, 158, 11, .12), transparent 32%),
            linear-gradient(180deg, #ffffff 0%, #f0fdf4 100%) !important;
    }

    [data-theme="dark"] .card.exam-card-done {
        border-color: #166534 !important;
        background:
            radial-gradient(circle at top right, rgba(34, 197, 94, .10), transparent 35%),
            linear-gradient(180deg, #0f172a 0%, #052e16 100%) !important;
    }

    [data-theme="dark"] .card.exam-card-done.best-seller-card {
        border-color: #166534 !important;
        background:
            radial-gradient(circle at top right, rgba(245, 158, 11, .16), transparent 34%),
            linear-gradient(180deg, #0f172a 0%, #052e16 100%) !important;
    }


    /* ===== Fix Admin: không áp dụng nền "Đã làm" trong trang admin ===== */
    .admin-page .card.exam-card-done,
    .admin-page .card.best-seller-card,
    .admin-page .card.exam-card-done.best-seller-card {
        background: #0f172a !important;
        border-color: #334155 !important;
    }

    [data-theme="light"] .admin-page .card.exam-card-done,
    [data-theme="light"] .admin-page .card.best-seller-card,
    [data-theme="light"] .admin-page .card.exam-card-done.best-seller-card {
        background: #ffffff !important;
        border-color: #e5e7eb !important;
    }

    .admin-page .done-badge,
    .admin-page .done-meta,
    .admin-page .local-done-badge,
    .admin-page .local-done-meta,
    .admin-page .best-seller-badge,
    .admin-page .exam-popularity {
        display: none !important;
    }


    /* ===== Home card: kéo badge "Đã làm" lại gần tên đề ===== */
    .exam-row-head {
        display: flex;
        align-items: center;
        justify-content: flex-start;
        gap: 12px;
        flex-wrap: wrap;
    }

    .exam-row-main {
        min-width: 0;
        flex: 0 1 auto;
        max-width: 100%;
    }

    .exam-row-main h2 {
        margin: 0;
    }

    .exam-row-badges {
        display: inline-flex;
        align-items: center;
        gap: 8px;
        flex-wrap: wrap;
    }

    @media (max-width: 900px) {
        .exam-row-head {
            align-items: flex-start;
        }

        .exam-row-badges {
            width: 100%;
        }
    }


    /* ===== Admin order hint ===== */
    .order-hint {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        margin-top: 8px;
        padding: 6px 9px;
        border-radius: 999px;
        background: #eff6ff;
        border: 1px solid #bfdbfe;
        color: #1d4ed8;
        font-size: 12px;
        font-weight: 800;
    }

    [data-theme="dark"] .order-hint {
        background: #0f172a !important;
        border-color: #334155 !important;
        color: #93c5fd !important;
    }

</style>

<script>
    (function () {
        function closePdfSidebarByDefault() {
            const fragment = "#navpanes=0&pagemode=none&view=FitH";
            document.querySelectorAll("iframe.pdf, iframe.review-pdf, iframe.result-pdf, iframe.result-compact-pdf").forEach(function (frame) {
                const src = frame.getAttribute("src") || "";
                if (!src || src.includes("navpanes=0") || src.includes("pagemode=none")) return;

                const clean = src.split("#")[0];
                frame.setAttribute("src", clean + fragment);
            });
        }

        if (document.readyState === "loading") {
            document.addEventListener("DOMContentLoaded", closePdfSidebarByDefault);
        } else {
            closePdfSidebarByDefault();
        }
    })();
</script>


<script>
    (function () {
        function forceDoneCardGreen() {
            // Chỉ áp dụng ở trang chủ. Không áp dụng trong Admin/SuperAdmin.
            if (document.querySelector(".admin-page") || location.pathname.startsWith("/admin") || location.pathname.startsWith("/superadmin")) {
                return;
            }

            document.querySelectorAll(".done-badge, .local-done-badge").forEach(function (badge) {
                const card = badge.closest(".card");
                if (card) {
                    card.classList.add("exam-card-done");
                }
            });
        }

        if (document.readyState === "loading") {
            document.addEventListener("DOMContentLoaded", forceDoneCardGreen);
        } else {
            forceDoneCardGreen();
        }
    })();
</script>


<script>
    (function () {
        const DONE_KEY = "quizDoneByExamV1";

        function readDoneMap() {
            try {
                return JSON.parse(localStorage.getItem(DONE_KEY) || "{}") || {};
            } catch (e) {
                return {};
            }
        }

        function writeDoneMap(map) {
            try {
                localStorage.setItem(DONE_KEY, JSON.stringify(map));
            } catch (e) {}
        }

        function formatDateTime(value) {
            if (!value) return "";
            return String(value).replace("T", " ").slice(0, 19);
        }

        function saveCurrentResult() {
            const resultRoot = document.querySelector("[data-result-exam-id]");
            if (!resultRoot) return;

            const examId = resultRoot.getAttribute("data-result-exam-id");
            if (!examId) return;

            const title = resultRoot.getAttribute("data-result-title") || "";
            const score = resultRoot.getAttribute("data-result-score") || "";
            const maxScore = resultRoot.getAttribute("data-result-max-score") || "";
            const now = new Date().toISOString().slice(0, 19);

            const map = readDoneMap();
            const old = map[examId] || {};
            const oldBest = parseFloat(String(old.bestScore || "").replace(",", "."));
            const newScore = parseFloat(String(score || "").replace(",", "."));

            let bestScore = score;
            let bestMaxScore = maxScore;
            if (!Number.isNaN(oldBest) && !Number.isNaN(newScore) && oldBest > newScore) {
                bestScore = old.bestScore;
                bestMaxScore = old.maxScore || maxScore;
            }

            map[examId] = {
                title: title,
                bestScore: bestScore,
                maxScore: bestMaxScore,
                lastDoneAt: now,
                attemptCount: Number(old.attemptCount || 0) + 1
            };
            writeDoneMap(map);
        }

        function markHomeCards() {
            // Chỉ đánh dấu đề đã làm ở trang chủ, không đụng vào giao diện Admin.
            if (location.pathname !== "/") return;

            const map = readDoneMap();
            if (!map || Object.keys(map).length === 0) return;

            document.querySelectorAll('a[href^="/exam/"]').forEach(link => {
                const href = link.getAttribute("href") || "";
                const parts = href.split("/exam/");
                if (parts.length < 2) return;

                const examId = (parts[1] || "").split(/[?#]/)[0];
                if (!examId) return;
                const done = map[examId];
                if (!done) return;

                const card = link.closest(".card");
                if (!card) return;

                card.classList.add("exam-card-done");

                if (!card.querySelector(".done-badge, .local-done-badge")) {
                    const badge = document.createElement("span");
                    badge.className = "local-done-badge";
                    badge.textContent = "✓ Đã làm";

                    const title = card.querySelector("h2, h3");
                    if (title) {
                        title.insertAdjacentElement("afterend", badge);
                    } else {
                        card.insertAdjacentElement("afterbegin", badge);
                    }
                }

                if (!card.querySelector(".done-meta, .local-done-meta")) {
                    const meta = document.createElement("div");
                    meta.className = "local-done-meta";

                    const score = document.createElement("span");
                    score.textContent = "Điểm cao nhất: " + (done.bestScore || "?") + "/" + (done.maxScore || "?");

                    const time = document.createElement("span");
                    time.textContent = "Lần gần nhất: " + formatDateTime(done.lastDoneAt);

                    const attempts = document.createElement("span");
                    attempts.textContent = "Số lần: " + (done.attemptCount || 1);

                    meta.appendChild(score);
                    meta.appendChild(time);
                    meta.appendChild(attempts);

                    const title = card.querySelector("h2, h3");
                    if (title) {
                        title.insertAdjacentElement("afterend", meta);
                    } else {
                        card.appendChild(meta);
                    }
                }
            });
        }

        if (document.readyState === "loading") {
            document.addEventListener("DOMContentLoaded", function () {
                saveCurrentResult();
                markHomeCards();
            });
        } else {
            saveCurrentResult();
            markHomeCards();
        }
    })();
</script>


<div class="mobile-tip-backdrop" id="mobileTipModal" aria-hidden="true">
    <div class="mobile-tip-box" role="dialog" aria-modal="true" aria-labelledby="mobileTipTitle">
        <div class="mobile-tip-icon">💻</div>
        <h2 id="mobileTipTitle">Nên dùng máy tính để làm bài</h2>
        <p>
            Web vẫn dùng được trên điện thoại, nhưng đề PDF và phiếu trả lời sẽ dễ thao tác hơn nhiều khi dùng laptop/máy tính,
            đặc biệt với đề dài hoặc nhiều câu.
        </p>
        <div class="mobile-tip-actions">
            <button type="button" id="mobileTipOk">Đã hiểu</button>
            <button type="button" class="mobile-tip-secondary" id="mobileTipNever">Không nhắc lại</button>
        </div>
    </div>
</div>

<script>
    (function () {
        const KEY = "mobileComputerTipSeen";

        function isMobileDevice() {
            const smallScreen = window.matchMedia("(max-width: 760px)").matches;
            const coarsePointer = window.matchMedia("(pointer: coarse)").matches;
            return smallScreen && coarsePointer;
        }

        function closeModal(save) {
            const modal = document.getElementById("mobileTipModal");
            if (!modal) return;
            modal.classList.remove("show");
            modal.setAttribute("aria-hidden", "true");
            document.body.style.overflow = "";
            if (save) {
                localStorage.setItem(KEY, "1");
            }
        }

        function showModal() {
            if (!isMobileDevice()) return;
            if (localStorage.getItem(KEY) === "1") return;

            const modal = document.getElementById("mobileTipModal");
            if (!modal) return;

            modal.classList.add("show");
            modal.setAttribute("aria-hidden", "false");
            document.body.style.overflow = "hidden";

            const okBtn = document.getElementById("mobileTipOk");
            const neverBtn = document.getElementById("mobileTipNever");

            if (okBtn) okBtn.addEventListener("click", function () {
                closeModal(true);
            });

            if (neverBtn) neverBtn.addEventListener("click", function () {
                closeModal(true);
            });

            modal.addEventListener("click", function (event) {
                if (event.target === modal) {
                    closeModal(true);
                }
            });
        }

        if (document.readyState === "loading") {
            document.addEventListener("DOMContentLoaded", showModal);
        } else {
            showModal();
        }
    })();
</script>


<script>
    (function () {
        function ensureNavActions() {
            const nav = document.querySelector(".nav");
            if (!nav) return null;

            let actions = nav.querySelector(":scope > .nav-actions");
            if (actions) return actions;

            actions = document.createElement("div");
            actions.className = "nav-actions";

            const children = Array.from(nav.children);
            children.slice(1).forEach(child => actions.appendChild(child));
            nav.appendChild(actions);

            return actions;
        }

        function updateCountdownText(el) {
            // Mốc thi THPTQG 2026: 11/06/2026
            const target = new Date(2026, 5, 11, 0, 0, 0);
            const now = new Date();
            const days = Math.ceil((target - now) / (24 * 60 * 60 * 1000));

            if (days > 0) {
                el.innerHTML = 'THPTQG 2026: còn <b>' + days + '</b> ngày';
            } else if (days === 0) {
                el.innerHTML = 'THPTQG 2026: <b>hôm nay thi</b>';
            } else {
                el.innerHTML = 'THPTQG 2026: <b>đã diễn ra</b>';
            }
        }

        function initCountdownRight() {
            const actions = ensureNavActions();
            if (!actions) return;

            let el = document.getElementById("examCountdown");
            if (!el) {
                el = document.createElement("div");
                el.id = "examCountdown";
                el.className = "exam-countdown";
                actions.insertBefore(el, actions.firstChild);
            } else if (el.parentElement !== actions) {
                actions.insertBefore(el, actions.firstChild);
            }

            updateCountdownText(el);
            setInterval(function () {
                updateCountdownText(el);
            }, 60 * 60 * 1000);
        }

        if (document.readyState === "loading") {
            document.addEventListener("DOMContentLoaded", initCountdownRight);
        } else {
            initCountdownRight();
        }
    })();
</script>


<script>
    (function () {
        function updateExamCountdown() {
            const el = document.getElementById("examCountdown");
            if (!el) return;

            // Ngày thi THPTQG 2026: 11/06/2026
            const target = new Date(2026, 5, 11, 0, 0, 0);
            const now = new Date();
            const msPerDay = 24 * 60 * 60 * 1000;
            const days = Math.ceil((target - now) / msPerDay);

            if (days > 0) {
                el.innerHTML = 'THPTQG 2026: còn <b>' + days + '</b> ngày';
            } else if (days === 0) {
                el.innerHTML = 'THPTQG 2026: <b>hôm nay thi</b>';
            } else {
                el.innerHTML = 'THPTQG 2026: <b>đã diễn ra</b>';
            }
        }

        updateExamCountdown();
        setInterval(updateExamCountdown, 60 * 60 * 1000);
    })();
</script>


<script>
    (function () {
        try {
            localStorage.removeItem("pdfSize");
            localStorage.removeItem("pdfDragWidth");
            localStorage.removeItem("pdfDragWidthV2");
        } catch (e) {}
    })();
</script>













<script>
    (function () {
        function applyTheme(theme) {
            document.documentElement.setAttribute("data-theme", theme);
            localStorage.setItem("theme", theme);

            const btn = document.getElementById("themeToggle");
            if (btn) {
                btn.textContent = theme === "dark" ? "☀ Light" : "🌙 Dark";
                btn.title = theme === "dark" ? "Đổi sang light mode" : "Đổi sang dark mode";
                btn.setAttribute("aria-label", btn.title);
            }
        }

        function initThemeToggle() {
            const nav = document.querySelector(".nav");
            if (!nav || document.getElementById("themeToggle")) return;

            let rightArea = nav.querySelector("div:last-child");
            if (!rightArea || rightArea === nav) {
                rightArea = document.createElement("div");
                nav.appendChild(rightArea);
            }

            const btn = document.createElement("button");
            btn.type = "button";
            btn.id = "themeToggle";
            btn.className = "theme-toggle";
            rightArea.appendChild(btn);

            const currentTheme = localStorage.getItem("theme") || "light";
            applyTheme(currentTheme);

            btn.addEventListener("click", function () {
                const now = document.documentElement.getAttribute("data-theme") || "light";
                applyTheme(now === "dark" ? "light" : "dark");
            });
        }

        if (document.readyState === "loading") {
            document.addEventListener("DOMContentLoaded", initThemeToggle);
        } else {
            initThemeToggle();
        }
    })();
</script>

"""


SUPERADMIN_LOGIN_HTML = BASE_CSS + """
<div class="nav">
    <div><b>SuperAdmin</b></div>
    <div>
        <a href="{{ url_for('home') }}">Trang chủ</a>
    </div>
</div>

<div class="container" style="max-width:520px">
    <div class="card">
        <h1>SuperAdmin Login</h1>
        <p class="muted">Trang xem log lượt làm bài.</p>

        {% if error %}
            <div class="card wrong">{{ error }}</div>
        {% endif %}

        <form method="post">
            <label>Mật khẩu SuperAdmin</label>
            <input type="password" name="password" placeholder="Nhập mật khẩu" autofocus>
            <button type="submit">Đăng nhập</button>
        </form>
    </div>
</div>
"""


SUPERADMIN_HTML = BASE_CSS + """
<div class="nav">
    <div><b>SuperAdmin Logs</b></div>
    <div>
        <a href="{{ url_for('home') }}">Trang chủ</a>
        <a href="{{ url_for('superadmin_logout') }}">Đăng xuất</a>
    </div>
</div>

<div class="container admin-page">
    <div class="super-stats">
        <div class="super-stat">
            <span>Tổng lượt nộp</span>
            <b>{{ stats.total_submits or 0 }}</b>
        </div>
        <div class="super-stat">
            <span>Thiết bị/người ước tính</span>
            <b>{{ stats.total_visitors or 0 }}</b>
        </div>
        <div class="super-stat">
            <span>Điểm trung bình</span>
            <b>{{ format_score(stats.avg_score or 0) }}</b>
        </div>
    </div>

    <div class="card">
        <h2>Bộ lọc</h2>
        <form method="get" class="super-filter">
            <div>
                <label>Môn</label>
                <select name="subject">
                    <option value="all" {% if selected_subject == "all" %}selected{% endif %}>Tất cả</option>
                    {% for key, sub in subjects.items() %}
                        <option value="{{ key }}" {% if selected_subject == key %}selected{% endif %}>{{ sub.label }}</option>
                    {% endfor %}
                </select>
            </div>
            <div>
                <label>Tìm theo đề / IP / visitor</label>
                <input name="q" value="{{ q }}" placeholder="Ví dụ: Tây Ninh, 113.160..., v2026...">
            </div>
            <div class="super-filter-actions">
                <button type="submit">Lọc</button>
                <a class="link-btn" href="{{ url_for('superadmin_dashboard') }}">Xóa lọc</a>
            </div>
        </form>
    </div>

    <div class="card">
        <h2>Top đề có lượt làm nhiều</h2>
        <div class="super-table-wrap">
            <table class="super-table">
                <thead>
                    <tr>
                        <th>Đề</th>
                        <th>Môn</th>
                        <th>Lượt</th>
                        <th>Điểm TB</th>
                    </tr>
                </thead>
                <tbody>
                    {% for row in by_exam %}
                    <tr>
                        <td>{{ row.exam_title }}</td>
                        <td>{{ subjects[row.subject].label if row.subject in subjects else row.subject }}</td>
                        <td>{{ row.total }}</td>
                        <td>{{ format_score(row.avg_score or 0) }}</td>
                    </tr>
                    {% else %}
                    <tr><td colspan="4" class="muted">Chưa có dữ liệu.</td></tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
    </div>

    <div class="card">
        <div class="super-head-row">
            <div>
                <h2>Log lượt làm bài</h2>
                <p class="muted">Không cần tài khoản. Hệ thống ghi theo IP, trình duyệt, visitor_id trong session và thời gian nộp.</p>
            </div>
            <form method="post" action="{{ url_for('superadmin_clear_logs') }}" onsubmit="return confirm('Xóa toàn bộ log?')">
                <button class="danger" type="submit">Xóa log</button>
            </form>
        </div>

        <div class="super-table-wrap">
            <table class="super-table">
                <thead>
                    <tr>
                        <th>Thời gian nộp</th>
                        <th>Đề</th>
                        <th>Môn</th>
                        <th>Điểm</th>
                        <th>Sai/cần xem</th>
                        <th>Thời gian làm</th>
                        <th>IP</th>
                        <th>Visitor</th>
                        <th>Thiết bị</th>
                    </tr>
                </thead>
                <tbody>
                    {% for log in logs %}
                    <tr>
                        <td class="nowrap">{{ format_datetime_display(log.submitted_at) }}</td>
                        <td>{{ log.exam_title }}</td>
                        <td>{{ subjects[log.subject].label if log.subject in subjects else log.subject }}</td>
                        <td class="score-cell">{{ format_score(log.score) }}/{{ format_score(log.max_score) }}</td>
                        <td>{{ log.wrong_total }}</td>
                        <td class="nowrap">{{ format_duration(log.duration_seconds) or "Không rõ" }}</td>
                        <td class="nowrap">{{ log.ip_address or "" }}</td>
                        <td class="nowrap">{{ log.visitor_id or "" }}</td>
                        <td class="ua-cell">{{ log.user_agent or "" }}</td>
                    </tr>
                    {% else %}
                    <tr><td colspan="9" class="muted">Chưa có lượt nộp bài nào.</td></tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
    </div>
</div>
"""



HOME_HTML = BASE_CSS + """
<div class="nav">
    <b>Thi thử THPTQG</b>
    <div><a href="/admin/login">Admin</a></div>
</div>

<div class="container">
    <div class="page-head">
        <div>
            <h1>Danh sách bài thi</h1>
            <p class="muted">Chọn một đề để đọc PDF và làm quiz.</p>
        <div class="home-stats">
            <span class="home-stat-pill">👥 Visitor: <b>{{ visitor_count }}</b></span>
            <span class="home-stat-pill">📝 Lượt nộp: <b>{{ submit_count }}</b></span>
        </div>

        </div>

        <div class="filters">
            <a class="filter-chip {% if selected_subject == 'all' %}active{% endif %}" href="/">Tất cả</a>
            <a class="filter-chip {% if selected_subject == 'math' %}active{% endif %}" href="/?subject=math">Toán</a>
            <a class="filter-chip {% if selected_subject == 'physics' %}active{% endif %}" href="/?subject=physics">Lí</a>
            <a class="filter-chip {% if selected_subject == 'cs' %}active{% endif %}" href="/?subject=cs">Tin</a>
            <a class="filter-chip {% if selected_subject == 'english' %}active{% endif %}" href="/?subject=english">Tiếng Anh</a>
        </div>
    </div>

    {% if exams %}
        {% for exam in exams %}
                {% set done = done_map.get(exam.id) %}
            <div class="card exam-list-card">
                <div class="exam-card-row">
                    <div class="subject-icon {{ exam.subject }}">{{ subjects[exam.subject].icon }}</div>

                    <div>
                        <div class="exam-card-top">
                            <div>
                                <div class="exam-row-head">
                            <div class="exam-row-main">
                                <h2>{{ exam.title }}</h2>
                            </div>
                            <div class="exam-row-badges">
                                {% if exam.submit_count and exam.submit_count == best_seller_count and best_seller_count > 0 %}
                                    <div class="best-seller-badge">🔥 BEST SELLER</div>
                                {% endif %}
                                {% if done %}
                                    <div class="done-badge">✓ Đã làm</div>
                                {% endif %}
                            </div>
                        </div>
                        {% if done %}
                            <div class="done-meta">
                                <span>Điểm cao nhất: {{ format_score(done.best_score) }}/{{ format_score(done.max_score) }}</span>
                                <span>Lần gần nhất: {{ format_datetime_display(done.last_done_at) }}</span>
                                <span>Số lần: {{ done.attempt_count }}</span>
                            </div>
                        {% endif %}
                                <p class="muted exam-meta">Ngày tạo: {{ format_datetime_display(exam.created_at) }}</p>
                            </div>

                            <span class="subject-pill {{ exam.subject }}">{{ subjects[exam.subject].label }}</span>
                        </div>

                        <a class="exam-start-link" href="/exam/{{ exam.id }}">Vào làm bài →</a>
                    </div>
                </div>
            </div>
        {% endfor %}
    {% else %}
        <div class="card exam-card {% if done %}exam-card-done{% endif %} {% if exam.submit_count and exam.submit_count == best_seller_count and best_seller_count > 0 %}best-seller-card{% endif %}">
            <h2 style="margin-top:0">Chưa có đề phù hợp</h2>
            <p class="muted" style="margin-bottom:0">Hiện chưa có đề nào trong bộ lọc bạn đang chọn.</p>
        </div>
    {% endif %}
</div>
"""


LOGIN_HTML = BASE_CSS + """
<div class="nav">
    <b>Admin Login</b>
    <a href="/">Trang chủ</a>
</div>

<div class="container">
    <div class="card">
        <h1>Đăng nhập admin</h1>

        {% if error %}
            <p style="color:red">{{ error }}</p>
        {% endif %}

        <form method="post">
            <label>Mật khẩu</label>
            <input type="password" name="password" placeholder="Nhập mật khẩu admin">
            <button>Đăng nhập</button>
        </form>
    </div>
</div>
"""


ADMIN_HTML = BASE_CSS + """
<div class="nav">
    <b>Admin Dashboard</b>
    <div>
        <a href="/">Trang chủ</a>
        <a href="/admin/logout">Đăng xuất</a>
    </div>
</div>

<div class="container">
    <div class="card">
        <h1>Upload đề + đáp án Gemini tự đọc</h1>
        <p class="muted">
            Admin chỉ cần upload file đề PDF và file đáp án ảnh/PDF. Gemini sẽ tự trích đáp án.
            Model: <b>{{ model }}</b>
        </p>

        <form method="post" action="/admin/upload" enctype="multipart/form-data">
            <label>Tiêu đề đề thi</label>
            <input name="title" placeholder="VD: Đề minh họa Toán THPTQG 2025" required>

            <label>Môn</label>
            <select name="subject" required>
                {% for key, s in subjects.items() %}
                    <option value="{{ key }}">{{ s.label }}</option>
                {% endfor %}
            </select>

            <label>File đề PDF</label>
            <input type="file" name="exam_file" accept="application/pdf" required>

            <label style="display:flex;align-items:center;gap:10px;margin:6px 0 14px;cursor:pointer;">
                <input type="checkbox" name="remove_answer_page" value="1" style="width:auto;margin:0;">
                <span>Xóa trang đáp án cuối khỏi file đề hiển thị cho người làm</span>
            </label>
            <p class="muted" style="margin-top:-6px;">
                Bật mục này nếu PDF tải về có trang đáp án ở cuối. Hệ thống sẽ tạo bản PDF mới bỏ trang cuối, còn file đáp án upload riêng vẫn dùng để Gemini đọc.
            </p>

            <label>File đáp án ảnh/PDF</label>
            <input type="file" name="answer_file" accept="application/pdf,image/png,image/jpeg,image/webp" required>

            <button>Upload và Gemini đọc đáp án</button>
        </form>
    </div>

    <div class="card">
        <h2>Đề đã upload</h2>

        {% for exam in exams %}
            <div style="border-top:1px solid #e5e7eb;padding:12px 0">
                <b>{{ exam.title }}</b><br>
                <span class="muted">Môn: {{ subjects[exam.subject].label }} · {{ exam.created_at }}</span><br><br>

                <a href="/exam/{{ exam.id }}">Xem quiz</a>
                ·
                <a href="/admin/edit/{{ exam.id }}">Sửa đáp án</a>

                    <div class="order-hint">↕ Thứ tự này áp dụng ngoài trang chủ</div>
                    <div class="order-actions">
                        <form method="post" action="{{ url_for('admin_exam_order', exam_id=exam.id, action='top') }}">
                            <button type="submit">Đưa lên đầu</button>
                        </form>
                        <form method="post" action="{{ url_for('admin_exam_order', exam_id=exam.id, action='up') }}">
                            <button type="submit">Lên 1 bậc</button>
                        </form>
                        <form method="post" action="{{ url_for('admin_exam_order', exam_id=exam.id, action='down') }}">
                            <button type="submit">Xuống 1 bậc</button>
                        </form>
                        <form method="post" action="{{ url_for('admin_exam_order', exam_id=exam.id, action='bottom') }}">
                            <button type="submit">Xuống cuối</button>
                        </form>
                    </div>

                ·
                <a href="/admin/preview/{{ exam.id }}">Xem JSON</a>

                <form method="post" action="/admin/delete/{{ exam.id }}" style="margin-top:8px">
                    <button class="danger" onclick="return confirm('Xóa đề này?')">Xóa</button>
                </form>
            </div>
        {% else %}
            <p>Chưa có đề nào.</p>
        {% endfor %}
    </div>
</div>
"""


EDIT_ANSWERS_HTML = BASE_CSS + """
<div class="nav">
    <b>Sửa đáp án</b>
    <div>
        <a href="{{ url_for('admin_dashboard') }}">Admin</a>
        <a href="{{ url_for('home') }}">Trang chủ</a>
    </div>
</div>

<div class="container">
    <div class="admin-edit-layout">
        <div class="card compact-admin-card">
            <h1>{{ exam.title }}</h1>
            <p class="muted" style="margin-bottom:0">
                Môn: {{ cfg.label }} · Nếu Gemini đọc sai thì sửa nhanh bên dưới rồi lưu.
            </p>

            <form class="rename-title-form" method="post" action="{{ url_for('admin_rename_exam', exam_id=exam.id) }}">
                <div>
                    <label>Đổi tên đề</label>
                    <input name="new_title" value="{{ exam.title }}" placeholder="Nhập tên đề mới" required>
                </div>
                <button type="submit">Đổi tên</button>
            </form>
        </div>

        <div class="admin-tools-grid">
            <div class="card compact-admin-card">
                <h2>Thay file đề PDF</h2>
                <p class="muted">Dùng khi file đề bị mờ/sai. Chỉ thay PDF đề, không đổi đáp án đã đọc.</p>

                <form method="post" enctype="multipart/form-data" action="{{ url_for('admin_replace_exam_file', exam_id=exam.id) }}">
                    <div class="replace-file-row">
                        <div>
                            <label>File đề mới (.pdf)</label>
                            <input type="file" name="new_exam_file" accept="application/pdf" required>
                        </div>
                        <button type="submit">Thay đề</button>
                    </div>

                    <label class="option" style="margin-top:10px">
                        <input type="checkbox" name="remove_answer_page" value="1" style="width:auto; margin-right:8px">
                        Xóa trang đáp án cuối nếu file đề mới có đáp án ở cuối
                    </label>
                </form>
            </div>

            <div class="card compact-admin-card">
                <h2>JSON Gemini</h2>
                <p class="muted">Thu gọn để đỡ rối. Bấm mở khi cần debug.</p>

                <details class="gemini-details">
                    <summary>Xem JSON Gemini trả về</summary>
                    <pre>{{ gemini_raw_json or data|tojson(indent=2) }}</pre>
                </details>
            </div>
        </div>

        <form method="post" class="card compact-admin-card">
            <div class="admin-answer-section">
                <h2>Phần I - Trắc nghiệm</h2>
                <div class="admin-answer-grid compact">
                    {% for n in part1_numbers %}
                        <div class="mini">
                            <b>Câu {{ n }}</b>
                            <select name="p1_{{ n }}">
                                <option value="">?</option>
                                {% for opt in ["A","B","C","D"] %}
                                    <option value="{{ opt }}" {% if data.answers.part1[n|string] == opt %}selected{% endif %}>{{ opt }}</option>
                                {% endfor %}
                            </select>
                        </div>
                    {% endfor %}
                </div>
            </div>

            {% if cfg.part2_count > 0 %}
            <div class="admin-answer-section">
                <h2>Phần II - Đúng / Sai</h2>
                <div class="admin-tf-grid">
                    {% for n in part2_numbers %}
                        <div class="admin-tf-card">
                            <b>Câu {{ n }}</b>
                            {% for letter in ["a","b","c","d"] %}
                                <div class="admin-tf-row">
                                    <span>{{ letter }})</span>
                                    <select name="p2_{{ n }}_{{ letter }}">
                                        <option value="" {% if data.answers.part2[n|string][letter] is none %}selected{% endif %}>?</option>
                                        <option value="true" {% if data.answers.part2[n|string][letter] is sameas true %}selected{% endif %}>Đúng</option>
                                        <option value="false" {% if data.answers.part2[n|string][letter] is sameas false %}selected{% endif %}>Sai</option>
                                    </select>
                                </div>
                            {% endfor %}
                        </div>
                    {% endfor %}
                </div>
            </div>
            {% endif %}

            {% if cfg.part3_count > 0 %}
            <div class="admin-answer-section">
                <h2>Phần III - Trả lời ngắn</h2>
                <div class="admin-answer-grid compact">
                    {% for n in part3_numbers %}
                        <div class="mini">
                            <b>Câu {{ n }}</b>
                            <input name="p3_{{ n }}" value="{{ data.answers.part3[n|string] }}">
                        </div>
                    {% endfor %}
                </div>
            </div>
            {% endif %}

            <button type="submit">Lưu đáp án</button>
        </form>
    </div>
</div>
"""


PREVIEW_HTML = BASE_CSS + """
<div class="nav">
    <b>Preview dữ liệu</b>
    <a href="/admin">Admin</a>
</div>

<div class="container">
    <div class="card">
        <h1>{{ exam.title }}</h1>
        <p>Môn: {{ cfg.label }}</p>
        <p><a href="/admin/edit/{{ exam.id }}">Sửa đáp án</a> · <a href="/exam/{{ exam.id }}">Mở bài làm</a></p>
    </div>

    <div class="card">
        <h2>JSON đã lưu</h2>
        <pre>{{ data_pretty }}</pre>
    </div>
</div>
"""


TAKE_EXAM_HTML = BASE_CSS + """
<div class="nav">
    <b>{{ exam.title }}</b>
    <a href="/">Trang chủ</a>
</div>

<div class="container">
    <div class="grid">
        <div class="pdf-pane">
            <iframe class="pdf" src="/uploads/{{ exam.exam_file }}#navpanes=0&pagemode=none&view=FitH"></iframe>
        </div>

        <div class="answer-pane">
            <form id="quizForm" class="card sheet-card" method="post" action="/exam/{{ exam.id }}/submit">
                <div class="sheet-top exam-sheet-top">
                    <div class="sheet-title-row">
                        <h1>Phiếu trả lời</h1>
                    </div>
                    <div class="sheet-top-actions">
                        <div class="timer">Còn lại: <span id="timer"></span></div>
                    </div>
                </div>

                <p class="muted exam-sheet-info">
                    {{ cfg.label }} - Thời gian làm bài {{ cfg.duration_minutes }} phút, không kể thời gian phát đề
                </p>

                <div class="answer-sheet">
                    <div class="sheet-section">
                        <div class="sheet-title">
                            <span>PHẦN I</span>
                            <span class="sheet-caption">Tô một đáp án đúng <i class="scan-mark"></i></span>
                        </div>

                        <div class="mc-columns">
                            {% for col in range(0, (cfg.part1_count + 9) // 10) %}
                                <div class="mc-col">
                                    <div class="mc-header">
                                        <div></div>
                                        <div>A</div>
                                        <div>B</div>
                                        <div>C</div>
                                        <div>D</div>
                                    </div>

                                    {% for n in range(col * 10 + 1, [col * 10 + 10, cfg.part1_count]|min + 1) %}
                                        <div class="mc-line">
                                            <div class="mc-num">{{ n }}</div>
                                            {% for key in ['A', 'B', 'C', 'D'] %}
                                                <label class="circle-choice">
                                                    <input type="radio" name="p1_{{ n }}" value="{{ key }}">
                                                    <span title="Câu {{ n }} - {{ key }}"></span>
                                                </label>
                                            {% endfor %}
                                        </div>
                                    {% endfor %}
                                </div>
                            {% endfor %}
                        </div>
                    </div>

                    {% if cfg.part2_count > 0 %}
                        <div class="sheet-section">
                            <div class="sheet-title">
                                <span>PHẦN II</span>
                                <span class="sheet-caption">Đúng / Sai <i class="scan-mark"></i></span>
                            </div>

                            <div class="tf-blocks">
                                {% for n in part2_numbers %}
                                    <div class="tf-block">
                                        <div class="tf-block-title">Câu {{ n }}</div>
                                        <div class="tf-head">
                                            <div></div>
                                            <div>Đúng</div>
                                            <div>Sai</div>
                                        </div>

                                        {% for letter in ['a', 'b', 'c', 'd'] %}
                                            <div class="tf-line">
                                                <div class="tf-letter">{{ letter }})</div>
                                                <label class="tf-choice">
                                                    <input type="radio" name="p2_{{ n }}_{{ letter }}" value="true">
                                                    <span class="true" title="Câu {{ n }}{{ letter }} đúng"></span>
                                                </label>
                                                <label class="tf-choice">
                                                    <input type="radio" name="p2_{{ n }}_{{ letter }}" value="false">
                                                    <span class="false" title="Câu {{ n }}{{ letter }} sai"></span>
                                                </label>
                                            </div>
                                        {% endfor %}
                                    </div>
                                {% endfor %}
                            </div>
                        </div>
                    {% endif %}

                    {% if cfg.part3_count > 0 %}
                        <div class="sheet-section">
                            <div class="sheet-title">
                                <span>PHẦN III</span>
                                <span class="sheet-caption">Nhập đáp án, có mô phỏng tô số <i class="scan-mark"></i></span>
                            </div>

                            <div class="short-columns">
                                {% for n in part3_numbers %}
                                    <div class="short-box">
                                        <div class="short-title">Câu {{ n }}</div>
                                        <div class="short-input-row">
                                            <input class="short-input" name="p3_{{ n }}" placeholder="VD: 4,9 hoặc 0.08">
                                            <button type="button" class="clear-short-btn" data-clear-short="p3_{{ n }}" title="Xóa đáp án" aria-label="Xóa đáp án">×</button>
                                        </div>

                                        <div class="digit-omr" data-short-question="{{ n }}">
                                            <div class="digit-omr-row">
                                                <div></div>
                                                {% for col in range(1, 7) %}
                                                    <div class="digit-omr-head">{{ col }}</div>
                                                {% endfor %}
                                            </div>

                                            <div class="digit-omr-row">
                                                <div class="digit-omr-label">-</div>
                                                {% for col in range(1, 7) %}
                                                    {% if col == 1 %}
                                                        <label class="digit-choice">
                                                            <input type="radio"
                                                                   name="p3bubble_{{ n }}_{{ col }}"
                                                                   value="-"
                                                                   data-target="p3_{{ n }}"
                                                                   data-col="{{ col }}">
                                                            <span></span>
                                                        </label>
                                                    {% else %}
                                                        <span class="digit-placeholder"></span>
                                                    {% endif %}
                                                {% endfor %}
                                            </div>

                                            <div class="digit-omr-row">
                                                <div class="digit-omr-label">,</div>
                                                {% for col in range(1, 7) %}
                                                    {% if col == 2 or col == 3 %}
                                                        <label class="digit-choice">
                                                            <input type="radio"
                                                                   name="p3bubble_{{ n }}_{{ col }}"
                                                                   value=","
                                                                   data-target="p3_{{ n }}"
                                                                   data-col="{{ col }}">
                                                            <span></span>
                                                        </label>
                                                    {% else %}
                                                        <span class="digit-placeholder"></span>
                                                    {% endif %}
                                                {% endfor %}
                                            </div>

                                            {% for digit in ['0', '1', '2', '3', '4', '5', '6', '7', '8', '9'] %}
                                                <div class="digit-omr-row">
                                                    <div class="digit-omr-label">{{ digit }}</div>
                                                    {% for col in range(1, 7) %}
                                                        <label class="digit-choice">
                                                            <input type="radio"
                                                                   name="p3bubble_{{ n }}_{{ col }}"
                                                                   value="{{ digit }}"
                                                                   data-target="p3_{{ n }}"
                                                                   data-col="{{ col }}">
                                                            <span></span>
                                                        </label>
                                                    {% endfor %}
                                                </div>
                                            {% endfor %}
                                        </div>

                                    </div>
                                {% endfor %}
                            </div>
                        </div>
                    {% endif %}
                </div>

                <div class="submit-bar">
                    <button>Nộp bài</button>
                </div>
            </form>
        </div>
    </div>
</div>

<script>
    const durationSeconds = {{ cfg.duration_minutes }} * 60;
    const examKey = "exam_{{ exam.id }}_deadline";
    let deadline = localStorage.getItem(examKey);

    if (!deadline) {
        deadline = Date.now() + durationSeconds * 1000;
        localStorage.setItem(examKey, deadline);
    } else {
        deadline = parseInt(deadline);
    }

    const timerEl = document.getElementById("timer");
    const form = document.getElementById("quizForm");

    // Lưu đáp án đang làm để reload trang không mất bài.
    // Sau khi đã nộp bài, quay lại trang làm bài sẽ tự xóa sạch nháp.
    const draftKey = "exam_{{ exam.id }}_draft";
    const submittedKey = "exam_{{ exam.id }}_submitted";

    function clearForm() {
        form.querySelectorAll("input").forEach(function(input) {
            if (input.disabled) return;

            if (input.type === "radio" || input.type === "checkbox") {
                input.checked = false;
            } else {
                input.value = "";
            }
        });
    }

    function clearDraft() {
        localStorage.removeItem(draftKey);
        localStorage.removeItem(examKey);
    }

    // Nếu trước đó đã nộp bài rồi mà quay lại /exam/id thì bắt đầu trắng lại.
    if (localStorage.getItem(submittedKey) === "1") {
        clearDraft();
        clearForm();
        localStorage.removeItem(submittedKey);
    }

    function saveDraft() {
        if (localStorage.getItem(submittedKey) === "1") return;

        const data = {};

        form.querySelectorAll("input").forEach(function(input) {
            if (!input.name || input.disabled) return;

            if (input.type === "radio") {
                if (input.checked) {
                    data[input.name] = input.value;
                }
            } else {
                data[input.name] = input.value;
            }
        });

        localStorage.setItem(draftKey, JSON.stringify(data));
    }

    function restoreDraft() {
        const raw = localStorage.getItem(draftKey);
        if (!raw) return;

        let data = {};
        try {
            data = JSON.parse(raw);
        } catch (e) {
            return;
        }

        Object.keys(data).forEach(function(name) {
            const value = data[name];
            const inputs = form.querySelectorAll(`[name="${CSS.escape(name)}"]`);

            inputs.forEach(function(input) {
                if (input.type === "radio") {
                    input.checked = input.value === value;
                } else {
                    input.value = value;
                }
            });
        });
    }

    restoreDraft();

    form.querySelectorAll(".digit-omr").forEach(function(grid) {
        const n = grid.dataset.shortQuestion;
        if (n) updateShortAnswerFromBubbles("p3_" + n);
    });

    form.addEventListener("change", saveDraft);
    form.addEventListener("input", saveDraft);

    // Bấm các ô tô số ở Phần III thì tự ghép đáp án vào ô nhập phía trên.
    function updateShortAnswerFromBubbles(targetName) {
        const input = form.querySelector(`[name="${CSS.escape(targetName)}"]`);
        if (!input) return;

        const picked = [];
        form.querySelectorAll(`input[data-target="${CSS.escape(targetName)}"]:checked`).forEach(function(radio) {
            picked.push({
                col: parseInt(radio.dataset.col || "0"),
                value: radio.value
            });
        });

        picked.sort(function(a, b) {
            return a.col - b.col;
        });

        let value = picked.map(function(x) { return x.value; }).join("");
        value = value.replace(/^-+$/, "");
        value = value.replace(/^-/, "-");
        value = value.replace(/,+/g, ",");
        value = value.replace(/-,/, "-");

        input.value = value;
        input.classList.add("filled-by-bubbles");
        saveDraft();
    }

    form.querySelectorAll("input[data-target]").forEach(function(radio) {
        radio.addEventListener("change", function() {
            updateShortAnswerFromBubbles(radio.dataset.target);
        });
    });

    // Nếu người dùng nhập tay thì bỏ chọn các ô tô số của câu đó để tránh lệch.
    form.querySelectorAll(".short-input").forEach(function(input) {
        input.addEventListener("input", function() {
            if (!input.name) return;
            input.classList.remove("filled-by-bubbles");

            form.querySelectorAll(`input[data-target="${CSS.escape(input.name)}"]`).forEach(function(radio) {
                radio.checked = false;
            });
        });
    });

    // Nút xóa cho từng câu trả lời ngắn nếu bấm nhầm/thừa số.
    form.querySelectorAll("[data-clear-short]").forEach(function(button) {
        button.addEventListener("click", function() {
            const targetName = button.dataset.clearShort;
            const input = form.querySelector(`[name="${CSS.escape(targetName)}"]`);

            if (input) {
                input.value = "";
                input.classList.remove("filled-by-bubbles");
            }

            form.querySelectorAll(`input[data-target="${CSS.escape(targetName)}"]`).forEach(function(radio) {
                radio.checked = false;
            });

            saveDraft();
        });
    });


    // Lưu thêm trước khi reload/đóng tab, nhưng không lưu lại sau khi đã bấm nộp.
    window.addEventListener("beforeunload", function() {
        if (localStorage.getItem(submittedKey) !== "1") {
            saveDraft();
        }
    });

    // Xử lý trường hợp browser back/forward cache: quay lại từ trang kết quả vẫn phải trắng.
    window.addEventListener("pageshow", function(event) {
        if (localStorage.getItem(submittedKey) === "1") {
            clearDraft();
            clearForm();
            localStorage.removeItem(submittedKey);
        }
    });

    function updateTimer() {
        const remaining = Math.max(0, Math.floor((deadline - Date.now()) / 1000));
        const minutes = Math.floor(remaining / 60);
        const seconds = remaining % 60;

        timerEl.textContent = String(minutes).padStart(2, "0") + ":" + String(seconds).padStart(2, "0");

        if (remaining <= 0) {
            localStorage.setItem(submittedKey, "1");
            clearDraft();
            form.submit();
        }
    }

    form.addEventListener("submit", function() {
        // Nộp bài xong thì xóa nháp. Nếu bấm Back về trang làm bài, form sẽ trắng.
        localStorage.setItem(submittedKey, "1");
        clearDraft();
    });

    updateTimer();
    setInterval(updateTimer, 1000);
</script>
"""


RESULT_HTML = BASE_CSS + """
<div class="nav">
    <b>Kết quả</b>
    <a href="/">Trang chủ</a>
</div>

<div class="container" data-result-exam-id="{{ exam.id }}" data-result-title="{{ exam.title }}" data-result-score="{{ score_text }}" data-result-max-score="{{ max_score_text }}">
    <div class="review-sheet-layout">
        <div class="pdf-pane">
            <iframe class="review-pdf" src="/uploads/{{ exam.exam_file }}#navpanes=0&pagemode=none&view=FitH"></iframe>
        </div>

        <div class="answer-pane">
            <div class="card review-panel">
                <div class="review-headline">
                    <div>
                        <h1>{{ exam.title }}</h1>
                        <p class="muted" style="margin:0">{{ cfg.label }} - Đối chiếu với đề bên trái</p>
                    </div>
                    <div class="result-head-side">
                        <div class="review-score">{{ score_text }}/{{ max_score_text }}</div>
                    </div>
                </div>

                <div class="mini-stats">
                    <span>TN <b>{{ summary.part1_correct }}/{{ summary.part1_total }}</b></span>
                    {% if cfg.part2_count > 0 %}
                        <span>Đ/S <b>{{ summary.part2_point_text }}đ</b></span>
                    {% endif %}
                    {% if summary.part3_total > 0 %}
                        <span>TLN <b>{{ summary.part3_correct }}/{{ summary.part3_total }}</b></span>
                    {% endif %}
                    <span>Cần xem <b>{{ summary.wrong_total }}</b></span>
                </div>

                <p class="muted" style="margin:0 0 8px; font-size:13px;">
                    Dòng nền xám = bạn bỏ trống. Nếu chọn sai sẽ có ô đỏ, đáp án đúng luôn hiện màu xanh.
                </p>

                <div class="result-sheet">
                    <div class="rs-section">
                        <div class="rs-title">
                            <span>PHẦN I</span>
                            <span class="rs-note">Xám nền dòng: bỏ trống · Đỏ: chọn sai · Xanh: đáp án đúng</span>
                        </div>

                        <div class="rs-mc-cols">
                            {% for col in range(0, (summary.part1_total + 9) // 10) %}
                                <div class="rs-mc-col">
                                    <div class="rs-mc-header">
                                        <div></div>
                                        <div>A</div>
                                        <div>B</div>
                                        <div>C</div>
                                        <div>D</div>
                                    </div>

                                    {% for n in range(col * 10 + 1, [col * 10 + 10, summary.part1_total]|min + 1) %}
                                        {% set r = results.part1[n - 1] %}
                                        <div class="rs-mc-line {% if not r.user %}unanswered{% endif %}">
                                            <div class="rs-num {% if not r.user %}unanswered{% endif %}">{{ n }}</div>
                                            {% for key in ['A', 'B', 'C', 'D'] %}
                                                {% if key == r.right %}
                                                    <span class="rs-circle correct"></span>
                                                {% elif r.user and key == r.user and r.user != r.right %}
                                                    <span class="rs-circle wrong"></span>
                                                {% else %}
                                                    <span class="rs-circle empty"></span>
                                                {% endif %}
                                            {% endfor %}
                                        </div>
                                    {% endfor %}
                                </div>
                            {% endfor %}
                        </div>
                    </div>

                    {% if cfg.part2_count > 0 %}
                        <div class="rs-section">
                            <div class="rs-title">
                                <span>PHẦN II</span>
                                <span class="rs-note">Xám nền dòng: bỏ trống · Đỏ: chọn sai · Xanh: đáp án đúng</span>
                            </div>

                            <div class="rs-tf-blocks">
                                {% for r in results.part2 %}
                                    <div class="rs-tf-block">
                                        <div class="rs-tf-title">Câu {{ r.number }}</div>
                                        <div class="rs-tf-head">
                                            <div></div>
                                            <div>Đúng</div>
                                            <div>Sai</div>
                                        </div>

                                        {% for item in r['items'] %}
                                            <div class="rs-tf-line {% if item.user is none %}unanswered{% endif %}">
                                                <div class="rs-letter {% if item.user is none %}unanswered{% endif %}">{{ item.letter }})</div>

                                                {% if item.right == true %}
                                                    <span class="rs-circle correct"></span>
                                                {% elif item.user == true and item.user != item.right %}
                                                    <span class="rs-circle wrong"></span>
                                                {% else %}
                                                    <span class="rs-circle empty"></span>
                                                {% endif %}

                                                {% if item.right == false %}
                                                    <span class="rs-circle correct"></span>
                                                {% elif item.user == false and item.user != item.right %}
                                                    <span class="rs-circle wrong"></span>
                                                {% else %}
                                                    <span class="rs-circle empty"></span>
                                                {% endif %}
                                            </div>
                                        {% endfor %}
                                    </div>
                                {% endfor %}
                            </div>
                        </div>
                    {% endif %}

                    {% if results.part3 %}
                        <div class="rs-section">
                            <div class="rs-title">
                                <span>PHẦN III</span>
                                <span class="rs-note">Đỏ: bạn nhập sai / chưa nhập · Xanh: đáp án đúng</span>
                            </div>

                            <table class="rs-short-table">
                                <thead>
                                    <tr>
                                        <th>Câu</th>
                                        <th>Bạn</th>
                                        <th>Đáp án</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {% for r in results.part3 %}
                                        <tr>
                                            <td><b>{{ r.number }}</b></td>
                                            <td class="{% if r.ok %}good{% else %}bad{% endif %}">{{ r.user or 'Chưa nhập' }}</td>
                                            <td class="good">{{ r.right or 'Chưa có' }}</td>
                                        </tr>
                                    {% endfor %}
                                </tbody>
                            </table>
                        </div>
                    {% endif %}
                </div>

                <div class="result-actions-small">
                    <a href="/exam/{{ exam.id }}">Làm lại</a>
                    <a href="/">Danh sách đề</a>
                </div>
            </div>
        </div>
    </div>
</div>
"""




migrate_old_db_if_needed()
init_db()
ensure_display_order_column()


if __name__ == "__main__":
    migrate_old_db_if_needed()
    init_db()
    ensure_display_order_column()
    app.run(debug=True)
