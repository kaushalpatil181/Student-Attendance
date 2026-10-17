
import os
import csv
import sqlite3
import secrets
import io


from datetime import datetime
from pathlib import Path
from functools import wraps

import qrcode
from PIL import Image, ImageDraw, ImageFont

from flask import (
    Flask, render_template, request, redirect,
    url_for, flash, send_file, abort, jsonify, session,
    make_response
)

from werkzeug.security import (
    generate_password_hash, check_password_hash
)


app = Flask(__name__)
app.secret_key = os.environ.get(
    "FLASK_SECRET_KEY",
    "college-demo-change-this-secret-before-deployment"
)

DATABASE = Path(__file__).resolve().parent / "attendance.db"


# ---------------- DATABASE ----------------

def get_db():
    conn = sqlite3.connect(str(DATABASE))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def create_database():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS students (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                roll_no TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                department TEXT NOT NULL,
                qr_token TEXT UNIQUE
            )
        """)

        columns = {
            row["name"]
            for row in conn.execute(
                "PRAGMA table_info(students)"
            ).fetchall()
        }

        if "qr_token" not in columns:
            conn.execute(
                "ALTER TABLE students ADD COLUMN qr_token TEXT"
            )

        old_students = conn.execute("""
            SELECT id FROM students
            WHERE qr_token IS NULL OR qr_token = ''
        """).fetchall()

        for student in old_students:
            token = secrets.token_urlsafe(32)

            while conn.execute(
                "SELECT 1 FROM students WHERE qr_token = ?",
                (token,)
            ).fetchone():
                token = secrets.token_urlsafe(32)

            conn.execute(
                "UPDATE students SET qr_token = ? WHERE id = ?",
                (token, student["id"])
            )

        conn.execute("""
            CREATE TABLE IF NOT EXISTS attendance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                student_id INTEGER NOT NULL,
                attendance_date TEXT NOT NULL,
                attendance_time TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'Present',
                UNIQUE(student_id, attendance_date),
                FOREIGN KEY(student_id) REFERENCES students(id)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL
            )
        """)

        admin = conn.execute(
            "SELECT id FROM admins WHERE username = ?",
            ("admin",)
        ).fetchone()

        if admin is None:
            conn.execute("""
                INSERT INTO admins (username, password_hash)
                VALUES (?, ?)
            """, (
                "admin",
                generate_password_hash("ChangeMe@123")
            ))


# ---------------- LOGIN PROTECTION ----------------

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "admin_id" not in session:
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


# ---------------- LOGIN ----------------

@app.route("/login", methods=["GET", "POST"])
def login():
    if "admin_id" in session:
        return redirect(url_for("home"))

    error = None

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        with get_db() as conn:
            admin = conn.execute("""
                SELECT id, username, password_hash
                FROM admins
                WHERE username = ?
            """, (username,)).fetchone()

        if admin and check_password_hash(
            admin["password_hash"], password
        ):
            session.clear()
            session["admin_id"] = admin["id"]
            session["admin_username"] = admin["username"]
            return redirect(url_for("home"))

        error = "Invalid username or password."

    return render_template("login.html", error=error)


# ---------------- LOGOUT ----------------

@app.route("/logout", methods=["POST"])
@login_required
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------- HOME ----------------

@app.route("/")
@login_required
def home():
    return """
    <!DOCTYPE html>
    <html lang="en">
    <head>
      <meta charset="UTF-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>Student Attendance System</title>
      <style>
        body {
          font-family: Arial, sans-serif;
          background: #eef2ff;
          text-align: center;
          padding: 40px 15px;
          color: #1e293b;
        }
        .card {
          background: white;
          padding: 30px;
          max-width: 700px;
          margin: auto;
          border-radius: 14px;
          box-shadow: 0 4px 14px #dbe2ef;
        }
        a, button {
          display: inline-block;
          margin: 8px;
          padding: 12px 18px;
          color: white;
          background: #2563eb;
          border: 0;
          border-radius: 6px;
          text-decoration: none;
          cursor: pointer;
        }
        .logout { background: #dc2626; }
      </style>
    </head>
    <body>
      <div class="card">
        <h1>Student Attendance System</h1>
        <p>QR Code Attendance Monitoring and Analysis</p>
        <p>Welcome, Administrator!</p>

        <a href="/students">Manage Students</a>
        <a href="/scan">Scan QR Attendance</a>
        <a href="/report">Attendance Dashboard</a>
        <a href="/date-report">Date-wise Report</a>
        <a href="/export-attendance">Export Today's CSV</a>
        <a href="/change-password">Change Password</a>

        <form method="POST" action="/logout">
          <button class="logout" type="submit">Logout</button>
        </form>
      </div>
    </body>
    </html>
    """


# ---------------- STUDENT REGISTRATION ----------------

@app.route("/students", methods=["GET", "POST"])
@login_required
def students():
    if request.method == "POST":
        roll_no = request.form.get("roll_no", "").strip()
        name = request.form.get("name", "").strip()
        department = request.form.get("department", "").strip()

        if not roll_no or not name or not department:
            flash("Please fill all fields.", "error")
            return redirect(url_for("students"))

        try:
            with get_db() as conn:
                conn.execute("""
                    INSERT INTO students
                    (roll_no, name, department, qr_token)
                    VALUES (?, ?, ?, ?)
                """, (
                    roll_no,
                    name,
                    department,
                    secrets.token_urlsafe(32)
                ))

            flash("Student registered successfully!", "success")

        except sqlite3.IntegrityError:
            flash("This roll number already exists!", "error")

        return redirect(url_for("students"))

    with get_db() as conn:
        student_list = conn.execute("""
            SELECT id, roll_no, name, department
            FROM students
            ORDER BY id DESC
        """).fetchall()

    return render_template("students.html", students=student_list)


# ---------------- QR DOWNLOAD WITH NAME ----------------

@app.route("/qr/<int:student_id>")
@login_required
def download_qr(student_id):
    with get_db() as conn:
        student = conn.execute("""
            SELECT id, roll_no, name, qr_token
            FROM students
            WHERE id = ?
        """, (student_id,)).fetchone()

    if student is None or not student["qr_token"]:
        abort(404, description="Student QR code not found")

    qr = qrcode.QRCode(
        version=1,
        box_size=10,
        border=4
    )
    qr.add_data(student["qr_token"])
    qr.make(fit=True)

    qr_image = qr.make_image(
        fill_color="black",
        back_color="white"
    ).convert("RGB")

    # Better font when available; default font otherwise.
    try:
        font = ImageFont.truetype("arial.ttf", 22)
    except OSError:
        try:
            font = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                22
            )
        except OSError:
            font = ImageFont.load_default()

    name_text = f"Name: {student['name']}"
    roll_text = f"Roll No: {student['roll_no']}"

    # Measure text using the same font.
    measure = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    name_box = measure.textbbox((0, 0), name_text, font=font)
    roll_box = measure.textbbox((0, 0), roll_text, font=font)

    text_width = max(
        name_box[2] - name_box[0],
        roll_box[2] - roll_box[0]
    )

    padding = 24
    width = max(qr_image.width, text_width + 2 * padding)
    text_height = max(
        name_box[3] - name_box[1],
        roll_box[3] - roll_box[1]
    )
    gap = 12
    height = qr_image.height + 2 * padding + 2 * text_height + gap

    final_image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(final_image)

    qr_x = (width - qr_image.width) // 2
    final_image.paste(qr_image, (qr_x, padding))

    name_y = padding + qr_image.height + 8
    roll_y = name_y + text_height + gap

    draw.text(
        ((width - (name_box[2] - name_box[0])) // 2, name_y),
        name_text,
        fill="black",
        font=font
    )
    draw.text(
        ((width - (roll_box[2] - roll_box[0])) // 2, roll_y),
        roll_text,
        fill="black",
        font=font
    )

    output = io.BytesIO()
    final_image.save(output, format="PNG")
    output.seek(0)

    return send_file(
        output,
        mimetype="image/png",
        as_attachment=True,
        download_name=f"QR_{student['roll_no']}.png"
    )


# ---------------- QR SCANNER ----------------

@app.route("/scan")
@login_required
def scan_page():
    return render_template("scan.html")


# ---------------- MARK ATTENDANCE ----------------

@app.route("/mark-attendance", methods=["POST"])
@login_required
def mark_attendance():
    data = request.get_json(silent=True) or {}
    token = data.get("token", "")

    if not isinstance(token, str) or not token.strip():
        return jsonify({
            "success": False,
            "message": "Invalid QR code."
        }), 400

    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    current_time = now.strftime("%H:%M:%S")

    with get_db() as conn:
        student = conn.execute("""
            SELECT id, name, roll_no
            FROM students
            WHERE qr_token = ?
        """, (token.strip(),)).fetchone()

        if student is None:
            return jsonify({
                "success": False,
                "message": "QR code not recognized."
            }), 404

        try:
            conn.execute("""
                INSERT INTO attendance
                (student_id, attendance_date, attendance_time, status)
                VALUES (?, ?, ?, 'Present')
            """, (student["id"], today, current_time))

        except sqlite3.IntegrityError:
            return jsonify({
                "success": False,
                "message": (
                    f"{student['name']}'s attendance "
                    "has already been marked today."
                )
            }), 409

    return jsonify({
        "success": True,
        "message": (
            f"Attendance marked! {student['name']} "
            f"({student['roll_no']}) - {current_time}"
        )
    })


# ---------------- ATTENDANCE DASHBOARD ----------------

@app.route("/report")
@login_required
def report():
    today = datetime.now().strftime("%Y-%m-%d")

    with get_db() as conn:
        total_students = conn.execute(
            "SELECT COUNT(*) AS total FROM students"
        ).fetchone()["total"]

        total_days = conn.execute("""
            SELECT COUNT(DISTINCT attendance_date) AS total
            FROM attendance
        """).fetchone()["total"]

        present_today = conn.execute("""
            SELECT COUNT(DISTINCT student_id) AS total
            FROM attendance
            WHERE attendance_date = ? AND status = 'Present'
        """, (today,)).fetchone()["total"]

        rows = conn.execute("""
            SELECT
                s.id, s.roll_no, s.name, s.department,
                COUNT(
                    CASE WHEN a.status = 'Present' THEN 1 END
                ) AS present_days
            FROM students s
            LEFT JOIN attendance a ON s.id = a.student_id
            GROUP BY s.id, s.roll_no, s.name, s.department
            ORDER BY s.roll_no
        """).fetchall()

    records = []

    for row in rows:
        present_days = row["present_days"]
        percentage = (
            round(present_days / total_days * 100, 2)
            if total_days else 0
        )

        records.append({
            "roll_no": row["roll_no"],
            "name": row["name"],
            "department": row["department"],
            "present_days": present_days,
            "attendance_percentage": percentage
        })

    absent_today = max(total_students - present_today, 0)
    low_attendance = sum(
        1 for record in records
        if total_days > 0
        and record["attendance_percentage"] < 75
    )

    return render_template(
        "report.html",
        records=records,
        total_students=total_students,
        total_days=total_days,
        present_today=present_today,
        absent_today=absent_today,
        low_attendance=low_attendance
    )


# ---------------- DATE-WISE REPORT ----------------

@app.route("/date-report")
@login_required
def date_report():
    selected_date = request.args.get(
        "date", datetime.now().strftime("%Y-%m-%d")
    )

    try:
        selected_date = datetime.strptime(
            selected_date, "%Y-%m-%d"
        ).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        selected_date = datetime.now().strftime("%Y-%m-%d")

    with get_db() as conn:
        records = conn.execute("""
            SELECT
                s.id AS student_id,
                s.roll_no,
                s.name,
                s.department,
                a.attendance_time,
                a.status
            FROM students s
            LEFT JOIN attendance a
                ON s.id = a.student_id
                AND a.attendance_date = ?
            ORDER BY s.roll_no
        """, (selected_date,)).fetchall()

    present_count = sum(
        1 for record in records
        if record["status"] == "Present"
    )

    return render_template(
        "date_report.html",
        records=records,
        selected_date=selected_date,
        total_students=len(records),
        present_count=present_count,
        not_marked_count=len(records) - present_count
    )


# ---------------- DELETE ATTENDANCE ----------------

@app.route(
    "/delete-attendance/<int:student_id>",
    methods=["POST"]
)
@login_required
def delete_attendance(student_id):
    selected_date = request.form.get("date", "")

    try:
        selected_date = datetime.strptime(
            selected_date, "%Y-%m-%d"
        ).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        abort(400, description="Invalid attendance date")

    with get_db() as conn:
        result = conn.execute("""
            DELETE FROM attendance
            WHERE student_id = ?
              AND attendance_date = ?
        """, (student_id, selected_date))

        if result.rowcount:
            flash("Attendance deleted successfully!", "success")
        else:
            flash("Attendance record was not found.", "error")

    return redirect(
        url_for("date_report", date=selected_date)
    )


# ---------------- CSV EXPORT ----------------

@app.route("/export-attendance")
@login_required
def export_attendance():
    selected_date = request.args.get(
        "date", datetime.now().strftime("%Y-%m-%d")
    )

    try:
        selected_date = datetime.strptime(
            selected_date, "%Y-%m-%d"
        ).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        abort(400, description="Invalid date format")

    with get_db() as conn:
        rows = conn.execute("""
            SELECT
                s.roll_no,
                s.name,
                s.department,
                a.attendance_time,
                COALESCE(a.status, 'Not Marked') AS status
            FROM students s
            LEFT JOIN attendance a
                ON s.id = a.student_id
                AND a.attendance_date = ?
            ORDER BY s.roll_no
        """, (selected_date,)).fetchall()

    output = io.StringIO(newline="")
    writer = csv.writer(output)

    writer.writerow([
        "Date", "Roll Number", "Student Name",
        "Department", "Status", "Attendance Time"
    ])

    for row in rows:
        writer.writerow([
            selected_date,
            row["roll_no"],
            row["name"],
            row["department"],
            row["status"],
            row["attendance_time"] or ""
        ])

    response = make_response("\ufeff" + output.getvalue())
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = (
        f'attachment; filename="attendance_{selected_date}.csv"'
    )

    return response


# ---------------- CHANGE ADMIN PASSWORD ----------------

@app.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    error = None
    message = None

    if request.method == "POST":
        current = request.form.get("current_password", "")
        new = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")

        with get_db() as conn:
            admin = conn.execute("""
                SELECT id, password_hash
                FROM admins
                WHERE id = ?
            """, (session["admin_id"],)).fetchone()

            if not admin or not check_password_hash(
                admin["password_hash"], current
            ):
                error = "Current password is incorrect."

            elif len(new) < 8:
                error = "New password must be at least 8 characters."

            elif new != confirm:
                error = "New passwords do not match."

            else:
                conn.execute("""
                    UPDATE admins
                    SET password_hash = ?
                    WHERE id = ?
                """, (
                    generate_password_hash(new),
                    session["admin_id"]
                ))

                message = "Password changed successfully."

    return render_template(
        "change_password.html",
        error=error,
        message=message
    )


# ---------------- START APPLICATION ----------------

if __name__ == "__main__":
    create_database()
    app.run(host="0.0.0.0", port=5000, debug=True)