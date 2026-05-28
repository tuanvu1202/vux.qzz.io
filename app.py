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


APP_SECRET = "doi-secret-nay-di"
ADMIN_PASSWORD = "admin123"
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
            created_at TEXT NOT NULL
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
            pix = page.get_pixmap(matrix=fitz.Matrix(2.5, 2.5), alpha=False)
            parts.append(types.Part.from_bytes(data=pix.tobytes("png"), mime_type="image/png"))
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
        return [types.Part.from_bytes(data=f.read(), mime_type=mime)]


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
Bạn là hệ thống đọc đáp án đề thi THPTQG từ ảnh/PDF đáp án.

Môn: {cfg["label"]}

Cấu trúc bắt buộc:
- Phần I có đúng {cfg["part1_count"]} câu trắc nghiệm A/B/C/D.
- Phần II có đúng {cfg["part2_count"]} câu đúng/sai, mỗi câu có 4 ý a,b,c,d.
- Phần III có đúng {cfg["part3_count"]} câu trả lời ngắn. Nếu môn không có phần III thì part3 là object rỗng.

Yêu cầu đọc:
- Đọc theo bố cục bảng trong ảnh, không đọc lẫn cột.
- Nếu ảnh đáp án dạng cột dọc, hãy lấy theo thứ tự từ trên xuống dưới:
  đầu tiên là Phần I, tiếp theo là Phần II, cuối cùng là Phần III.
- Không lấy mã đề làm đáp án.
- Không lấy số thứ tự câu làm đáp án trả lời ngắn.
- Phần I chỉ được A/B/C/D.
- Phần II: Đúng = true, Sai = false. Các ký hiệu Đ, D, Đúng, Dung là true. S, Sai là false.
- Phần III: giữ nguyên đáp án dạng chuỗi, ví dụ "4,9", "0,08", "3200".
- Nếu không chắc, điền "" ở phần I/III hoặc null ở phần II.

Trả về DUY NHẤT JSON theo schema này, không giải thích:
{{
  "part1": {{"1": "B", "2": "D"}},
  "part2": {{
    "1": {{"a": true, "b": false, "c": true, "d": true}}
  }},
  "part3": {{"1": "4,9", "2": "43"}}
}}

Với môn hiện tại, bắt buộc trả đủ:
- part1: từ "1" đến "{cfg["part1_count"]}"
- part2: từ "1" đến "{cfg["part2_count"]}", mỗi câu có a,b,c,d
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

    conn = db()
    if selected_subject in SUBJECTS:
        exams = conn.execute(
            "SELECT id, title, subject, created_at FROM exams WHERE subject=? ORDER BY id DESC",
            (selected_subject,)
        ).fetchall()
    else:
        selected_subject = "all"
        exams = conn.execute(
            "SELECT id, title, subject, created_at FROM exams ORDER BY id DESC"
        ).fetchall()
    conn.close()

    return render_template_string(
        HOME_HTML,
        exams=exams,
        subjects=SUBJECTS,
        selected_subject=selected_subject,
        format_datetime_display=format_datetime_display,
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
    exams = conn.execute("SELECT id, title, subject, created_at FROM exams ORDER BY id DESC").fetchall()
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
        INSERT INTO exams (title, subject, exam_file, answer_file, data_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
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


BASE_CSS = """
<!doctype html>
<html lang="vi">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Web của Vũ</title>
    <meta name="description" content="Không có chi mô">
    <meta name="theme-color" content="#111827">

    <meta property="og:title" content="Web của Vũ">
    <meta property="og:description" content="Không có chi mô">
    <meta property="og:type" content="website">

    <link rel="icon" href="data:image/svg+xml,%3Csvg%20xmlns%3D%22http%3A//www.w3.org/2000/svg%22%20viewBox%3D%220%200%20100%20100%22%3E%0A%3Crect%20width%3D%22100%22%20height%3D%22100%22%20rx%3D%2222%22%20fill%3D%22%23111827%22/%3E%0A%3Ctext%20x%3D%2250%22%20y%3D%2262%22%20font-size%3D%2254%22%20text-anchor%3D%22middle%22%20font-family%3D%22Arial%2C%20sans-serif%22%20font-weight%3D%22700%22%20fill%3D%22%23ffffff%22%3EQ%3C/text%3E%0A%3Ccircle%20cx%3D%2274%22%20cy%3D%2228%22%20r%3D%2210%22%20fill%3D%22%232563eb%22/%3E%0A%3C/svg%3E">
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
        display: flex;
        justify-content: space-between;
        align-items: center;
    }

    .nav a {
        color: white;
        margin-left: 16px;
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


</style>
"""


HOME_HTML = BASE_CSS + """
<div class="nav">
    <b>THPTQG PDF Quiz</b>
    <div><a href="/admin/login">Admin</a></div>
</div>

<div class="container">
    <div class="page-head">
        <div>
            <h1>Danh sách bài thi</h1>
            <p class="muted">Chọn một đề để đọc PDF và làm quiz.</p>
        </div>

        <div class="filters">
            <a class="filter-chip {% if selected_subject == 'all' %}active{% endif %}" href="/">Tất cả</a>
            <a class="filter-chip {% if selected_subject == 'math' %}active{% endif %}" href="/?subject=math">Toán</a>
            <a class="filter-chip {% if selected_subject == 'physics' %}active{% endif %}" href="/?subject=physics">Lí</a>
            <a class="filter-chip {% if selected_subject == 'cs' %}active{% endif %}" href="/?subject=cs">Tin</a>
        </div>
    </div>

    {% if exams %}
        {% for exam in exams %}
            <div class="card exam-list-card">
                <div class="exam-card-row">
                    <div class="subject-icon {{ exam.subject }}">{{ subjects[exam.subject].icon }}</div>

                    <div>
                        <div class="exam-card-top">
                            <div>
                                <h2>{{ exam.title }}</h2>
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
        <div class="card">
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
        <a href="/admin">Admin</a>
        <a href="/exam/{{ exam.id }}">Xem quiz</a>
    </div>
</div>

<div class="container">
    <form method="post" class="card">
        <h1>{{ exam.title }}</h1>
        <p class="muted">Môn: {{ cfg.label }}. Gemini đã tự đọc đáp án; nếu sai thì sửa nhanh rồi lưu.</p>

        <h2 class="section-title">Phần I - Trắc nghiệm</h2>
        <div class="admin-answer-grid">
            {% for n in part1_numbers %}
                <div class="mini">
                    <b>Câu {{ n }}</b>
                    <select name="p1_{{ n }}">
                        <option value="">--</option>
                        {% for c in ['A', 'B', 'C', 'D'] %}
                            <option value="{{ c }}" {% if data.answers.part1.get(n|string) == c %}selected{% endif %}>{{ c }}</option>
                        {% endfor %}
                    </select>
                </div>
            {% endfor %}
        </div>

        <h2 class="section-title">Phần II - Đúng/Sai</h2>
        {% for n in part2_numbers %}
            <div class="question">
                <h3>Câu {{ n }}</h3>
                <div class="admin-answer-grid">
                    {% for letter in ['a', 'b', 'c', 'd'] %}
                        <div class="mini">
                            <b>{{ letter }})</b>
                            <select name="p2_{{ n }}_{{ letter }}">
                                <option value="">--</option>
                                <option value="true" {% if data.answers.part2.get(n|string, {}).get(letter) == true %}selected{% endif %}>Đúng</option>
                                <option value="false" {% if data.answers.part2.get(n|string, {}).get(letter) == false %}selected{% endif %}>Sai</option>
                            </select>
                        </div>
                    {% endfor %}
                </div>
            </div>
        {% endfor %}

        {% if cfg.part3_count > 0 %}
            <h2 class="section-title">Phần III - Trả lời ngắn</h2>
            <div class="admin-answer-grid">
                {% for n in part3_numbers %}
                    <div class="mini">
                        <b>Câu {{ n }}</b>
                        <input name="p3_{{ n }}" value="{{ data.answers.part3.get(n|string, '') }}">
                    </div>
                {% endfor %}
            </div>
        {% endif %}

        <button style="margin-top:24px">Lưu đáp án</button>
    </form>

    <div class="card">
        <h2>JSON Gemini trả về</h2>
        <pre>{{ data.gemini_debug.raw_json }}</pre>
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
        <div>
            <iframe class="pdf" src="/uploads/{{ exam.exam_file }}"></iframe>
        </div>

        <div>
            <form id="quizForm" class="card sheet-card" method="post" action="/exam/{{ exam.id }}/submit">
                <div class="sheet-top">
                    <div>
                        <h1>Phiếu trả lời</h1>
                        <p class="muted">
                            {{ cfg.label }} - Thời gian làm bài {{ cfg.duration_minutes }} phút, không kể thời gian phát đề
                        </p>
                    </div>
                    <div class="timer">Còn lại: <span id="timer"></span></div>
                </div>

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
                                            <button type="button" class="clear-short-btn" data-clear-short="p3_{{ n }}">Xóa</button>
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

<div class="container">
    <div class="review-sheet-layout">
        <div>
            <iframe class="review-pdf" src="/uploads/{{ exam.exam_file }}"></iframe>
        </div>

        <div>
            <div class="card review-panel">
                <div class="review-headline">
                    <div>
                        <h1>{{ exam.title }}</h1>
                        <p class="muted" style="margin:0">{{ cfg.label }} - Đối chiếu với đề bên trái</p>
                    </div>
                    <div class="review-score">{{ score_text }}/{{ max_score_text }}</div>
                </div>

                <div class="mini-stats">
                    <span>TN <b>{{ summary.part1_correct }}/{{ summary.part1_total }}</b></span>
                    <span>Đ/S <b>{{ summary.part2_point_text }}đ</b></span>
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


if __name__ == "__main__":
    migrate_old_db_if_needed()
    init_db()
    app.run(debug=True)
