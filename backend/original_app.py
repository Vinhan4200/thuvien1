"""
Quản lý thư viện - Flask + SQLite (1 file)

Cấu trúc:  Bộ / đầu sách  ->  Tập  ->  Quyển (bản sao)
  Ví dụ: "Doraemon" (bộ truyện tranh) có 10 tập; tập 1 có 2 quyển, tập 2 có 1 quyển...
  Sách lẻ (không chia tập) là bộ chỉ có 1 tập, tick "Sách lẻ" khi thêm.

Chạy:
    pip install flask
    python app.py
Mở trình duyệt: http://127.0.0.1:5000
Tài khoản quản trị mặc định: admin / admin  (nên đổi mật khẩu trong menu "Đổi mật khẩu")
"""
import os
import secrets
import sqlite3
from datetime import date, datetime, timedelta
from functools import wraps

from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   session, url_for)
from jinja2 import DictLoader
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "library.db")
KEY_PATH = os.path.join(BASE_DIR, "secret.key")
KINDS = ["Sách", "Truyện tranh"]
LOAN_DAYS = 14

app = Flask(__name__)


def _load_secret():
    if os.path.exists(KEY_PATH):
        with open(KEY_PATH) as f:
            return f.read().strip()
    key = secrets.token_hex(32)
    with open(KEY_PATH, "w") as f:
        f.write(key)
    return key


app.secret_key = _load_secret()


# ----------------------------------------------------------------- Database
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS series(           -- bộ / đầu sách
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    author TEXT DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'Sách',       -- Sách / Truyện tranh
    category TEXT DEFAULT '',                -- thể loại
    note TEXT DEFAULT '',
    single INTEGER NOT NULL DEFAULT 0        -- 1 = sách lẻ, không chia tập
);
CREATE TABLE IF NOT EXISTS volumes(          -- tập
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    series_id INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    vol_no INTEGER NOT NULL DEFAULT 1,
    copies INTEGER NOT NULL DEFAULT 1,       -- số quyển của tập này
    UNIQUE(series_id, vol_no)
);
CREATE TABLE IF NOT EXISTS loans(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    volume_id INTEGER NOT NULL REFERENCES volumes(id) ON DELETE CASCADE,
    borrower TEXT NOT NULL,
    phone TEXT DEFAULT '',
    borrow_date TEXT NOT NULL,
    due_date TEXT NOT NULL,
    return_date TEXT
);
"""


def migrate_old(db):
    """Chuyển dữ liệu từ bản cũ (bảng books: mỗi dòng 1 sách/tập) sang cấu trúc mới."""
    series_ids, vol_ids, book_to_vol = {}, {}, {}
    for b in db.execute("SELECT * FROM books ORDER BY id").fetchall():
        key = (b["title"], b["author"] or "", b["kind"], b["category"] or "")
        if key not in series_ids:
            cur = db.execute(
                "INSERT INTO series(title,author,kind,category,note,single) VALUES(?,?,?,?,?,1)",
                (*key, b["note"] or ""))
            series_ids[key] = cur.lastrowid
        sid = series_ids[key]
        vol = (b["volume"] or "").strip()
        if vol:
            db.execute("UPDATE series SET single=0 WHERE id=?", (sid,))
        no = int(vol) if vol.isdigit() else None
        if no is None:  # không phải số -> lấy số tiếp theo còn trống
            used = {r[0] for r in db.execute("SELECT vol_no FROM volumes WHERE series_id=?", (sid,))}
            no = 1
            while no in used:
                no += 1
        if (sid, no) in vol_ids:
            db.execute("UPDATE volumes SET copies = copies + ? WHERE id=?",
                       (b["total"], vol_ids[(sid, no)]))
        else:
            cur = db.execute("INSERT INTO volumes(series_id,vol_no,copies) VALUES(?,?,?)",
                             (sid, no, b["total"]))
            vol_ids[(sid, no)] = cur.lastrowid
        book_to_vol[b["id"]] = vol_ids[(sid, no)]
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='loans_old'").fetchone():
        for l in db.execute("SELECT * FROM loans_old").fetchall():
            if l["book_id"] in book_to_vol:
                db.execute("INSERT INTO loans(volume_id,borrower,phone,borrow_date,due_date,return_date) "
                           "VALUES(?,?,?,?,?,?)",
                           (book_to_vol[l["book_id"]], l["borrower"], l["phone"],
                            l["borrow_date"], l["due_date"], l["return_date"]))
    db.execute("ALTER TABLE books RENAME TO books_old")


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    need_migrate = "books" in tables and "series" not in tables
    if need_migrate and "loans" in tables:
        db.execute("ALTER TABLE loans RENAME TO loans_old")
    db.executescript(SCHEMA)
    if need_migrate:
        migrate_old(db)
    if not db.execute("SELECT 1 FROM users WHERE username='admin'").fetchone():
        db.execute("INSERT INTO users(username, password_hash) VALUES(?, ?)",
                   ("admin", generate_password_hash("admin")))
    db.commit()
    db.close()


# Tập kèm số quyển đang mượn
VOL_SQL = ("SELECT v.*, (SELECT COUNT(*) FROM loans l "
           "WHERE l.volume_id = v.id AND l.return_date IS NULL) AS borrowed FROM volumes v")
# Bộ kèm tổng hợp: số tập, tổng quyển, đang mượn
SERIES_SQL = ("SELECT s.*, COUNT(vv.id) AS nvol, COALESCE(SUM(vv.copies),0) AS total, "
              "COALESCE(SUM(vv.borrowed),0) AS borrowed FROM series s "
              "LEFT JOIN (" + VOL_SQL + ") vv ON vv.series_id = s.id")


# ------------------------------------------------------------------ Helpers
def is_admin():
    return session.get("user") == "admin"


def admin_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not is_admin():
            flash("Vui lòng đăng nhập quản trị.", "err")
            return redirect(url_for("login", next=request.path))
        return fn(*a, **kw)
    return wrapper


@app.before_request
def csrf_protect():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    if request.method == "POST":
        if request.form.get("csrf") != session.get("csrf"):
            abort(400, "CSRF token không hợp lệ")


@app.context_processor
def inject():
    return {"is_admin": is_admin(), "csrf": session.get("csrf", ""),
            "kinds": KINDS, "today": date.today().isoformat()}


@app.template_filter("dmy")
def dmy(value):
    if not value:
        return ""
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError:
        return value


def valid_date(s):
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except (ValueError, TypeError):
        return False


def to_int(s, default=None):
    try:
        return int(str(s).strip())
    except (ValueError, TypeError):
        return default


# ------------------------------------------------------------------- Public
@app.route("/")
def index():
    db = get_db()
    q = request.args.get("q", "").strip().lower()
    kind = request.args.get("kind", "")
    category = request.args.get("category", "")
    avail = request.args.get("avail", "")

    all_series = db.execute(SERIES_SQL + " GROUP BY s.id").fetchall()
    stats = dict(series=len(all_series),
                 vols=sum(s["nvol"] for s in all_series),
                 copies=sum(s["total"] for s in all_series),
                 borrowed=sum(s["borrowed"] for s in all_series))
    categories = sorted({s["category"] for s in all_series if s["category"]})

    series = []
    for s in all_series:
        if q and q not in s["title"].lower() and q not in (s["author"] or "").lower():
            continue
        if kind in KINDS and s["kind"] != kind:
            continue
        if category and s["category"] != category:
            continue
        if avail == "1" and s["total"] - s["borrowed"] <= 0:
            continue
        if avail == "0" and s["borrowed"] <= 0:
            continue
        series.append(s)
    series.sort(key=lambda s: (s["kind"], s["title"].lower()))

    vols = {}
    for v in db.execute(VOL_SQL + " ORDER BY v.series_id, v.vol_no").fetchall():
        vols.setdefault(v["series_id"], []).append(v)
    return render_template("index.html", series=series, vols=vols, stats=stats,
                           categories=categories, q=request.args.get("q", ""),
                           kind=kind, category=category, avail=avail)


@app.route("/loans")
def loans():
    db = get_db()
    status = request.args.get("status", "active")
    sql = ("SELECT l.*, s.title, s.kind, s.single, v.vol_no FROM loans l "
           "JOIN volumes v ON v.id = l.volume_id JOIN series s ON s.id = v.series_id")
    if status == "active":
        sql += " WHERE l.return_date IS NULL"
    elif status == "returned":
        sql += " WHERE l.return_date IS NOT NULL"
    sql += " ORDER BY l.return_date IS NOT NULL, l.due_date ASC, l.id DESC"
    return render_template("loans.html", loans=db.execute(sql).fetchall(), status=status)


# --------------------------------------------------------------------- Auth
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = request.form.get("username", "").strip()
        p = request.form.get("password", "")
        row = get_db().execute("SELECT * FROM users WHERE username=?", (u,)).fetchone()
        if row and check_password_hash(row["password_hash"], p):
            session.clear()
            session["user"] = row["username"]
            session["csrf"] = secrets.token_hex(16)
            flash("Đăng nhập thành công.", "ok")
            nxt = request.args.get("next", "")
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("index"))
        flash("Sai tên đăng nhập hoặc mật khẩu.", "err")
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("Đã đăng xuất.", "ok")
    return redirect(url_for("index"))


@app.route("/admin/password", methods=["GET", "POST"])
@admin_required
def change_password():
    if request.method == "POST":
        db = get_db()
        row = db.execute("SELECT * FROM users WHERE username='admin'").fetchone()
        old, new, new2 = (request.form.get(k, "") for k in ("old", "new", "new2"))
        if not check_password_hash(row["password_hash"], old):
            flash("Mật khẩu cũ không đúng.", "err")
        elif len(new) < 4 or new != new2:
            flash("Mật khẩu mới phải từ 4 ký tự và nhập lại phải khớp.", "err")
        else:
            db.execute("UPDATE users SET password_hash=? WHERE id=?",
                       (generate_password_hash(new), row["id"]))
            db.commit()
            flash("Đã đổi mật khẩu.", "ok")
            return redirect(url_for("index"))
    return render_template("password.html")


# ------------------------------------------------------- Admin: bộ / đầu sách
def read_series_form():
    f = request.form
    data = dict(title=f.get("title", "").strip(), author=f.get("author", "").strip(),
                kind=f.get("kind", "Sách"), category=f.get("category", "").strip(),
                note=f.get("note", "").strip())
    errors = []
    if not data["title"]:
        errors.append("Tên không được để trống.")
    if data["kind"] not in KINDS:
        errors.append("Loại không hợp lệ.")
    return data, errors


@app.route("/admin/series/new", methods=["GET", "POST"])
@admin_required
def series_new():
    s = dict(title="", author="", kind="Truyện tranh", category="", note="", single=0)
    form = dict(nvol=10, copies=1)
    if request.method == "POST":
        s, errors = read_series_form()
        s["single"] = 1 if request.form.get("single") == "1" else 0
        nvol = 1 if s["single"] else to_int(request.form.get("nvol"), -1)
        copies = to_int(request.form.get("copies"), -1)
        form = dict(nvol=request.form.get("nvol", 1), copies=request.form.get("copies", 1))
        if not 1 <= nvol <= 500:
            errors.append("Số tập phải từ 1 đến 500.")
        if not 0 <= copies <= 1000:
            errors.append("Số quyển mỗi tập phải từ 0 đến 1000.")
        if errors:
            for e in errors:
                flash(e, "err")
        else:
            db = get_db()
            cur = db.execute("INSERT INTO series(title,author,kind,category,note,single) "
                             "VALUES(:title,:author,:kind,:category,:note,:single)", s)
            db.executemany("INSERT INTO volumes(series_id,vol_no,copies) VALUES(?,?,?)",
                           [(cur.lastrowid, n, copies) for n in range(1, nvol + 1)])
            db.commit()
            flash("Đã thêm. Bạn có thể chỉnh số quyển cho từng tập ở trang này.", "ok")
            return redirect(url_for("series_edit", series_id=cur.lastrowid))
    return render_template("series_form.html", s=s, form=form, editing=False, vols=[])


@app.route("/admin/series/<int:series_id>/edit", methods=["GET", "POST"])
@admin_required
def series_edit(series_id):
    db = get_db()
    row = db.execute(SERIES_SQL + " WHERE s.id=? GROUP BY s.id", (series_id,)).fetchone()
    if not row:
        abort(404)
    s = dict(row)
    if request.method == "POST":
        data, errors = read_series_form()
        if errors:
            for e in errors:
                flash(e, "err")
            s.update(data)
        else:
            data["id"] = series_id
            db.execute("UPDATE series SET title=:title, author=:author, kind=:kind, "
                       "category=:category, note=:note WHERE id=:id", data)
            db.commit()
            flash("Đã cập nhật thông tin.", "ok")
            return redirect(url_for("series_edit", series_id=series_id))
    vols = db.execute(VOL_SQL + " WHERE v.series_id=? ORDER BY v.vol_no", (series_id,)).fetchall()
    return render_template("series_form.html", s=s, form={}, editing=True, vols=vols)


@app.route("/admin/series/<int:series_id>/delete", methods=["POST"])
@admin_required
def series_delete(series_id):
    db = get_db()
    row = db.execute(SERIES_SQL + " WHERE s.id=? GROUP BY s.id", (series_id,)).fetchone()
    if not row:
        abort(404)
    if row["borrowed"] > 0:
        flash("Không thể xóa: còn quyển đang được mượn.", "err")
        return redirect(url_for("series_edit", series_id=series_id))
    db.execute("DELETE FROM series WHERE id=?", (series_id,))  # tập + lịch sử mượn xóa theo
    db.commit()
    flash("Đã xóa.", "ok")
    return redirect(url_for("index"))


# ------------------------------------------------------------- Admin: các tập
@app.route("/admin/series/<int:series_id>/volumes/save", methods=["POST"])
@admin_required
def volumes_save(series_id):
    db = get_db()
    vols = db.execute(VOL_SQL + " WHERE v.series_id=?", (series_id,)).fetchall()
    updates, errors = [], []
    for v in vols:
        c = to_int(request.form.get(f"copies_{v['id']}"), -1)
        if c < 0 or c > 1000:
            errors.append(f"Tập {v['vol_no']}: số quyển không hợp lệ.")
        elif c < v["borrowed"]:
            errors.append(f"Tập {v['vol_no']}: không thể ít hơn số đang mượn ({v['borrowed']}).")
        else:
            updates.append((c, v["id"]))
    if errors:
        for e in errors:
            flash(e, "err")
    else:
        db.executemany("UPDATE volumes SET copies=? WHERE id=?", updates)
        db.commit()
        flash("Đã lưu số quyển các tập.", "ok")
    return redirect(url_for("series_edit", series_id=series_id))


@app.route("/admin/series/<int:series_id>/volumes/add", methods=["POST"])
@admin_required
def volumes_add(series_id):
    db = get_db()
    if not db.execute("SELECT 1 FROM series WHERE id=? AND single=0", (series_id,)).fetchone():
        abort(404)
    a = to_int(request.form.get("from"), 0)
    b = to_int(request.form.get("to"), 0) or a
    c = to_int(request.form.get("copies"), -1)
    if not (1 <= a <= b <= a + 500) or not 0 <= c <= 1000:
        flash("Số tập / số quyển không hợp lệ.", "err")
        return redirect(url_for("series_edit", series_id=series_id))
    exist = {r[0] for r in db.execute("SELECT vol_no FROM volumes WHERE series_id=?", (series_id,))}
    new = [n for n in range(a, b + 1) if n not in exist]
    db.executemany("INSERT INTO volumes(series_id,vol_no,copies) VALUES(?,?,?)",
                   [(series_id, n, c) for n in new])
    db.commit()
    skipped = (b - a + 1) - len(new)
    flash(f"Đã thêm {len(new)} tập" + (f" (bỏ qua {skipped} tập đã có)." if skipped else "."), "ok")
    return redirect(url_for("series_edit", series_id=series_id))


@app.route("/admin/volumes/<int:vol_id>/delete", methods=["POST"])
@admin_required
def volume_delete(vol_id):
    db = get_db()
    v = db.execute(VOL_SQL + " WHERE v.id=?", (vol_id,)).fetchone()
    if not v:
        abort(404)
    if v["borrowed"] > 0:
        flash("Không thể xóa tập: còn quyển đang được mượn.", "err")
    else:
        db.execute("DELETE FROM volumes WHERE id=?", (vol_id,))
        db.commit()
        flash(f"Đã xóa tập {v['vol_no']}.", "ok")
    return redirect(url_for("series_edit", series_id=v["series_id"]))


# -------------------------------------------------------------- Admin: loans
@app.route("/admin/loans/new", methods=["GET", "POST"])
@admin_required
def loan_new():
    db = get_db()
    choices = db.execute(
        "SELECT * FROM (SELECT vv.*, s.title, s.kind, s.single FROM (" + VOL_SQL + ") vv "
        "JOIN series s ON s.id = vv.series_id) WHERE copies - borrowed > 0 "
        "ORDER BY title COLLATE NOCASE, vol_no").fetchall()
    form = dict(volume_id=request.args.get("volume_id", ""), borrower="", phone="",
                borrow_date=date.today().isoformat(),
                due_date=(date.today() + timedelta(days=LOAN_DAYS)).isoformat())
    if request.method == "POST":
        form = {k: request.form.get(k, "").strip() for k in form}
        errors = []
        vol = None
        if form["volume_id"].isdigit():
            vol = db.execute(VOL_SQL + " WHERE v.id=?", (int(form["volume_id"]),)).fetchone()
        if not vol:
            errors.append("Vui lòng chọn sách / tập.")
        elif vol["copies"] - vol["borrowed"] <= 0:
            errors.append("Tập này đã hết quyển.")
        if not form["borrower"]:
            errors.append("Vui lòng nhập tên người mượn.")
        if not (valid_date(form["borrow_date"]) and valid_date(form["due_date"])):
            errors.append("Ngày không hợp lệ.")
        elif form["due_date"] < form["borrow_date"]:
            errors.append("Hạn trả không được trước ngày mượn.")
        if errors:
            for e in errors:
                flash(e, "err")
        else:
            db.execute("INSERT INTO loans(volume_id,borrower,phone,borrow_date,due_date) "
                       "VALUES(?,?,?,?,?)",
                       (vol["id"], form["borrower"], form["phone"],
                        form["borrow_date"], form["due_date"]))
            db.commit()
            flash("Đã ghi nhận cho mượn.", "ok")
            return redirect(url_for("loans"))
    return render_template("loan_form.html", choices=choices, form=form)


@app.route("/admin/loans/<int:loan_id>/return", methods=["POST"])
@admin_required
def loan_return(loan_id):
    db = get_db()
    d = request.form.get("return_date", "")
    if not valid_date(d):
        d = date.today().isoformat()
    db.execute("UPDATE loans SET return_date=? WHERE id=? AND return_date IS NULL", (d, loan_id))
    db.commit()
    flash("Đã ghi nhận trả sách.", "ok")
    return redirect(request.referrer or url_for("loans"))


@app.route("/admin/loans/<int:loan_id>/delete", methods=["POST"])
@admin_required
def loan_delete(loan_id):
    db = get_db()
    db.execute("DELETE FROM loans WHERE id=?", (loan_id,))
    db.commit()
    flash("Đã xóa bản ghi mượn.", "ok")
    return redirect(request.referrer or url_for("loans"))


# ---------------------------------------------------------------- Templates
BASE = """<!doctype html>
<html lang="vi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{% block title %}Thư viện{% endblock %}</title>
<style>
:root{--bg:#f5f6f8;--card:#fff;--ink:#1f2933;--mut:#6b7785;--line:#e2e6ea;--pri:#2b6cb0;--ok:#2f855a;--warn:#c05621;--err:#c53030}
@media(prefers-color-scheme:dark){:root{--bg:#14171a;--card:#1d2125;--ink:#e6e9ec;--mut:#98a2ad;--line:#2d343b;--pri:#63a4ee;--ok:#5cc08a;--warn:#f0a15e;--err:#f27c7c}}
*{box-sizing:border-box}body{margin:0;font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;background:var(--bg);color:var(--ink)}
header{background:var(--card);border-bottom:1px solid var(--line)}
nav{max-width:1100px;margin:auto;padding:12px 16px;display:flex;gap:6px;flex-wrap:wrap;align-items:center}
nav .brand{font-weight:700;margin-right:12px}nav a,nav button.link{color:var(--ink);text-decoration:none;padding:6px 10px;border-radius:6px;background:none;border:0;font:inherit;cursor:pointer}
nav a:hover,nav button.link:hover{background:var(--bg)}nav .sp{flex:1}
main{max-width:1100px;margin:20px auto;padding:0 16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin-bottom:12px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-bottom:16px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 16px}
.stat b{display:block;font-size:24px}.stat span{color:var(--mut);font-size:13px}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:middle}
th{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--mut)}
.scroll{overflow-x:auto}
input,select,textarea{font:inherit;padding:7px 9px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--ink);max-width:100%}
label{display:block;font-size:13px;color:var(--mut);margin:10px 0 3px}
.btn{display:inline-block;font:inherit;padding:7px 14px;border-radius:6px;border:1px solid var(--pri);background:var(--pri);color:#fff;text-decoration:none;cursor:pointer}
.btn.sec{background:none;color:var(--pri)}.btn.dng{background:none;border-color:var(--err);color:var(--err)}.btn.sm{padding:3px 9px;font-size:13px}
.filters{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.tag{display:inline-block;padding:1px 8px;border-radius:99px;font-size:12px;border:1px solid var(--line);color:var(--mut)}
.ok{color:var(--ok)}.warn{color:var(--warn)}.err{color:var(--err)}.mut{color:var(--mut)}
.flash{padding:10px 14px;border-radius:8px;margin-bottom:12px;border:1px solid var(--line);background:var(--card)}
.flash.ok{border-color:var(--ok)}.flash.err{border-color:var(--err)}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:0 16px}@media(max-width:600px){.grid2{grid-template-columns:1fr}}
form.inline{display:inline}
details.card>summary{cursor:pointer;list-style:none;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
details.card>summary::-webkit-details-marker{display:none}
details.card>summary::before{content:"▸";color:var(--mut)}details[open].card>summary::before{content:"▾"}
.head{display:flex;gap:10px;align-items:center;flex-wrap:wrap;flex:1}.head .nums{margin-left:auto;color:var(--mut);font-size:13px}
.vols{margin-top:10px}
</style></head><body>
<header><nav>
 <span class="brand">📚 Thư viện</span>
 <a href="{{ url_for('index') }}">Danh sách sách</a>
 <a href="{{ url_for('loans') }}">Mượn / trả</a>
 {% if is_admin %}
  <a href="{{ url_for('series_new') }}">+ Thêm bộ / sách</a>
  <a href="{{ url_for('loan_new') }}">+ Cho mượn</a>
 {% endif %}
 <span class="sp"></span>
 {% if is_admin %}
  <a href="{{ url_for('change_password') }}">Đổi mật khẩu</a>
  <form class="inline" method="post" action="{{ url_for('logout') }}">
   <input type="hidden" name="csrf" value="{{ csrf }}"><button class="link">Đăng xuất (admin)</button></form>
 {% else %}<a href="{{ url_for('login') }}">Đăng nhập quản trị</a>{% endif %}
</nav></header>
<main>
{% with msgs = get_flashed_messages(with_categories=true) %}{% for c,m in msgs %}<div class="flash {{ c }}">{{ m }}</div>{% endfor %}{% endwith %}
{% block body %}{% endblock %}
</main></body></html>"""

INDEX = """{% extends 'base.html' %}
{% macro head(s) %}
 <span class="head">
  <b>{{ s.title }}</b> <span class="tag">{{ s.kind }}</span>
  {% if s.category %}<span class="tag">{{ s.category }}</span>{% endif %}
  {% if s.author %}<span class="mut">{{ s.author }}</span>{% endif %}
  <span class="nums">
   {% if not s.single %}{{ s.nvol }} tập · {% endif %}{{ s.total }} quyển · mượn {{ s.borrowed }} ·
   còn <b class="{{ 'ok' if s.total - s.borrowed > 0 else 'err' }}">{{ s.total - s.borrowed }}</b>
  </span>
 </span>
{% endmacro %}
{% block body %}
<div class="stats">
 <div class="stat"><b>{{ stats.series }}</b><span>Bộ / đầu sách</span></div>
 <div class="stat"><b>{{ stats.vols }}</b><span>Tổng số tập</span></div>
 <div class="stat"><b>{{ stats.copies }}</b><span>Tổng số quyển</span></div>
 <div class="stat"><b>{{ stats.borrowed }}</b><span>Đang cho mượn</span></div>
 <div class="stat"><b>{{ stats.copies - stats.borrowed }}</b><span>Hiện có sẵn</span></div>
</div>
<div class="card">
<form class="filters" method="get">
 <input name="q" value="{{ q }}" placeholder="Tìm tên hoặc tác giả...">
 <select name="kind"><option value="">Tất cả loại</option>
  {% for k in kinds %}<option {{ 'selected' if k==kind }}>{{ k }}</option>{% endfor %}</select>
 <select name="category"><option value="">Mọi thể loại</option>
  {% for c in categories %}<option {{ 'selected' if c==category }}>{{ c }}</option>{% endfor %}</select>
 <select name="avail">
  <option value="">Tất cả tình trạng</option>
  <option value="1" {{ 'selected' if avail=='1' }}>Còn sẵn</option>
  <option value="0" {{ 'selected' if avail=='0' }}>Có quyển đang được mượn</option></select>
 <button class="btn">Lọc</button><a class="btn sec" href="{{ url_for('index') }}">Xóa lọc</a>
 <span style="flex:1"></span>
 <button type="button" class="btn sec" onclick="document.querySelectorAll('details.card').forEach(d=>d.open=true)">Mở tất cả</button>
 <button type="button" class="btn sec" onclick="document.querySelectorAll('details.card').forEach(d=>d.open=false)">Thu gọn</button>
</form></div>

{% for s in series %}
{% if s.single %}
 <div class="card filters">{{ head(s) }}
  {% if is_admin %}
   {% if s.total - s.borrowed > 0 %}<a class="btn sm" href="{{ url_for('loan_new', volume_id=vols[s.id][0].id) }}">Cho mượn</a>{% endif %}
   <a class="btn sm sec" href="{{ url_for('series_edit', series_id=s.id) }}">Sửa</a>
  {% endif %}
 </div>
{% else %}
 <details class="card" {{ 'open' if series|length == 1 }}>
  <summary>{{ head(s) }}
   {% if is_admin %}<a class="btn sm sec" href="{{ url_for('series_edit', series_id=s.id) }}">Sửa bộ / tập</a>{% endif %}</summary>
  <div class="vols scroll"><table>
   <tr><th>Tập</th><th>Số quyển</th><th>Đang mượn</th><th>Còn</th>{% if is_admin %}<th></th>{% endif %}</tr>
   {% for v in vols.get(s.id, []) %}
   <tr><td><b>Tập {{ v.vol_no }}</b></td><td>{{ v.copies }}</td><td>{{ v.borrowed }}</td>
    <td><b class="{{ 'ok' if v.copies - v.borrowed > 0 else 'err' }}">{{ v.copies - v.borrowed }}</b></td>
    {% if is_admin %}<td>{% if v.copies - v.borrowed > 0 %}<a class="btn sm" href="{{ url_for('loan_new', volume_id=v.id) }}">Cho mượn</a>{% endif %}</td>{% endif %}</tr>
   {% else %}<tr><td colspan="5" class="mut">Chưa có tập nào.</td></tr>{% endfor %}
  </table></div>
 </details>
{% endif %}
{% else %}<div class="card mut">Không có kết quả.</div>{% endfor %}
{% endblock %}"""

LOANS = """{% extends 'base.html' %}{% block body %}
<div class="card filters">
 <a class="btn {{ '' if status=='active' else 'sec' }}" href="?status=active">Đang mượn</a>
 <a class="btn {{ '' if status=='returned' else 'sec' }}" href="?status=returned">Đã trả</a>
 <a class="btn {{ '' if status=='all' else 'sec' }}" href="?status=all">Tất cả</a>
 {% if not is_admin %}<span class="mut">Thông tin người mượn chỉ hiển thị cho quản trị viên.</span>{% endif %}
</div>
<div class="card scroll"><table>
<tr><th>Sách / truyện</th><th>Tập</th>{% if is_admin %}<th>Người mượn</th><th>SĐT</th>{% endif %}
<th>Ngày mượn</th><th>Hạn trả</th><th>Ngày trả</th><th>Tình trạng</th>{% if is_admin %}<th></th>{% endif %}</tr>
{% for l in loans %}
{% set overdue = (not l.return_date) and l.due_date < today %}
<tr>
 <td><b>{{ l.title }}</b> <span class="tag">{{ l.kind }}</span></td><td>{{ '' if l.single else l.vol_no }}</td>
 {% if is_admin %}<td>{{ l.borrower }}</td><td>{{ l.phone }}</td>{% endif %}
 <td>{{ l.borrow_date|dmy }}</td><td>{{ l.due_date|dmy }}</td><td>{{ l.return_date|dmy }}</td>
 <td>{% if l.return_date %}<span class="ok">Đã trả</span>{% elif overdue %}<span class="err">Quá hạn</span>{% else %}<span class="warn">Đang mượn</span>{% endif %}</td>
 {% if is_admin %}<td style="white-space:nowrap">
  {% if not l.return_date %}<form class="inline" method="post" action="{{ url_for('loan_return', loan_id=l.id) }}">
   <input type="hidden" name="csrf" value="{{ csrf }}"><input type="hidden" name="return_date" value="{{ today }}"><button class="btn sm">Trả sách</button></form>{% endif %}
  <form class="inline" method="post" action="{{ url_for('loan_delete', loan_id=l.id) }}" onsubmit="return confirm('Xóa bản ghi này?')">
   <input type="hidden" name="csrf" value="{{ csrf }}"><button class="btn sm dng">Xóa</button></form></td>{% endif %}
</tr>
{% else %}<tr><td colspan="9" class="mut">Chưa có dữ liệu.</td></tr>{% endfor %}
</table></div>{% endblock %}"""

SERIES_FORM = """{% extends 'base.html' %}{% block body %}
<div class="card"><h2 style="margin-top:0">{{ 'Sửa thông tin' if editing else 'Thêm bộ / đầu sách' }}</h2>
<form method="post"><input type="hidden" name="csrf" value="{{ csrf }}">
<div class="grid2">
 <div><label>Tên bộ truyện / sách *</label><input name="title" value="{{ s.title }}" required style="width:100%"></div>
 <div><label>Tác giả</label><input name="author" value="{{ s.author }}" style="width:100%"></div>
 <div><label>Loại</label><select name="kind" style="width:100%">{% for k in kinds %}<option {{ 'selected' if k==s.kind }}>{{ k }}</option>{% endfor %}</select></div>
 <div><label>Thể loại (văn học, trinh thám, thiếu nhi...)</label><input name="category" value="{{ s.category }}" style="width:100%"></div>
</div>
{% if not editing %}
 <label style="display:flex;gap:8px;align-items:center;color:var(--ink)">
  <input type="checkbox" name="single" value="1" id="single" {{ 'checked' if s.single }}> Sách lẻ (không chia tập)</label>
 <div class="grid2" id="multi">
  <div><label>Số tập của bộ</label><input type="number" min="1" max="500" name="nvol" value="{{ form.nvol }}" style="width:100%"></div>
  <div id="cp"><label>Số quyển mỗi tập (có thể chỉnh riêng từng tập sau)</label><input type="number" min="0" name="copies" value="{{ form.copies }}" style="width:100%"></div>
 </div>
 <script>
  const sg=document.getElementById('single'),mu=document.querySelector('[name=nvol]').parentElement;
  const sync=()=>{mu.style.display=sg.checked?'none':''};sg.onchange=sync;sync();
 </script>
{% endif %}
<label>Ghi chú</label><textarea name="note" rows="2" style="width:100%">{{ s.note }}</textarea>
<p><button class="btn">{{ 'Lưu thông tin' if editing else 'Tạo' }}</button> <a class="btn sec" href="{{ url_for('index') }}">Quay lại</a></p>
</form></div>

{% if editing %}
<div class="card"><h3 style="margin-top:0">{{ 'Số quyển' if s.single else 'Các tập và số quyển' }}</h3>
<form method="post" action="{{ url_for('volumes_save', series_id=s.id) }}"><input type="hidden" name="csrf" value="{{ csrf }}">
<div class="scroll"><table>
 <tr><th>{{ '' if s.single else 'Tập' }}</th><th>Số quyển</th><th>Đang mượn</th>{% if not s.single %}<th></th>{% endif %}</tr>
 {% for v in vols %}
 <tr><td>{{ '' if s.single else 'Tập ' ~ v.vol_no }}</td>
  <td><input type="number" min="{{ v.borrowed }}" max="1000" name="copies_{{ v.id }}" value="{{ v.copies }}" style="width:90px"></td>
  <td>{{ v.borrowed }}</td>
  {% if not s.single %}<td><button class="btn sm dng" formaction="{{ url_for('volume_delete', vol_id=v.id) }}" onclick="return confirm('Xóa tập {{ v.vol_no }} (kèm lịch sử mượn)?')">Xóa tập</button></td>{% endif %}
 </tr>
 {% else %}<tr><td colspan="4" class="mut">Chưa có tập nào.</td></tr>{% endfor %}
</table></div>
<p><button class="btn">Lưu số quyển</button></p>
</form></div>

{% if not s.single %}
<div class="card"><h3 style="margin-top:0">Thêm tập</h3>
<form class="filters" method="post" action="{{ url_for('volumes_add', series_id=s.id) }}"><input type="hidden" name="csrf" value="{{ csrf }}">
 Từ tập <input type="number" name="from" min="1" required style="width:80px">
 đến tập <input type="number" name="to" min="1" style="width:80px" placeholder="(1 tập)">
 mỗi tập <input type="number" name="copies" min="0" value="1" style="width:80px"> quyển
 <button class="btn">Thêm</button>
</form></div>
{% endif %}

<div class="card"><form method="post" action="{{ url_for('series_delete', series_id=s.id) }}" onsubmit="return confirm('Xóa toàn bộ mục này, kèm các tập và lịch sử mượn?')">
 <input type="hidden" name="csrf" value="{{ csrf }}"><button class="btn dng">Xóa {{ 'sách' if s.single else 'cả bộ' }} này</button>
 <span class="mut"> (không xóa được khi còn quyển đang mượn)</span></form></div>
{% endif %}
{% endblock %}"""

LOAN_FORM = """{% extends 'base.html' %}{% block body %}
<div class="card"><h2 style="margin-top:0">Cho mượn sách</h2>
<form method="post"><input type="hidden" name="csrf" value="{{ csrf }}">
<label>Sách / tập (chỉ hiện các tập còn quyển)</label>
<select name="volume_id" required style="width:100%"><option value="">-- chọn --</option>
{% for c in choices %}<option value="{{ c.id }}" {{ 'selected' if form.volume_id|string==c.id|string }}>
 {{ c.title }}{% if not c.single %} - Tập {{ c.vol_no }}{% endif %} [{{ c.kind }}] (còn {{ c.copies-c.borrowed }} quyển)</option>{% endfor %}</select>
<div class="grid2">
 <div><label>Người mượn *</label><input name="borrower" value="{{ form.borrower }}" required style="width:100%"></div>
 <div><label>Số điện thoại</label><input name="phone" value="{{ form.phone }}" style="width:100%"></div>
 <div><label>Ngày mượn</label><input type="date" name="borrow_date" value="{{ form.borrow_date }}" required style="width:100%"></div>
 <div><label>Hạn trả</label><input type="date" name="due_date" value="{{ form.due_date }}" required style="width:100%"></div>
</div>
<p><button class="btn">Ghi nhận</button> <a class="btn sec" href="{{ url_for('loans') }}">Hủy</a></p>
</form></div>{% endblock %}"""

LOGIN = """{% extends 'base.html' %}{% block body %}
<div class="card" style="max-width:360px;margin:40px auto"><h2 style="margin-top:0">Đăng nhập quản trị</h2>
<form method="post"><input type="hidden" name="csrf" value="{{ csrf }}">
<label>Tên đăng nhập</label><input name="username" autofocus required style="width:100%">
<label>Mật khẩu</label><input type="password" name="password" required style="width:100%">
<p><button class="btn">Đăng nhập</button></p></form></div>{% endblock %}"""

PASSWORD = """{% extends 'base.html' %}{% block body %}
<div class="card" style="max-width:360px;margin:40px auto"><h2 style="margin-top:0">Đổi mật khẩu</h2>
<form method="post"><input type="hidden" name="csrf" value="{{ csrf }}">
<label>Mật khẩu cũ</label><input type="password" name="old" required style="width:100%">
<label>Mật khẩu mới</label><input type="password" name="new" required style="width:100%">
<label>Nhập lại mật khẩu mới</label><input type="password" name="new2" required style="width:100%">
<p><button class="btn">Lưu</button></p></form></div>{% endblock %}"""

app.jinja_loader = DictLoader({
    "base.html": BASE, "index.html": INDEX, "loans.html": LOANS,
    "series_form.html": SERIES_FORM, "loan_form.html": LOAN_FORM,
    "login.html": LOGIN, "password.html": PASSWORD,
})

init_db()

if __name__ == "__main__":
    # Đổi host thành "0.0.0.0" nếu muốn máy khác trong mạng LAN truy cập
    app.run(host="127.0.0.1", port=5000, debug=False)
