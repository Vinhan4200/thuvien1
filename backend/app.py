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
DB_PATH = os.path.join(os.environ.get("DATA_DIR", BASE_DIR), "library.db")
KEY_PATH = os.path.join(os.environ.get("DATA_DIR", BASE_DIR), "secret.key")
KINDS = ["Sách", "Truyện tranh"]
LOAN_DAYS = 14

os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
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
        initial_password = os.environ.get("ADMIN_PASSWORD")
        if not initial_password or len(initial_password) < 12:
            raise RuntimeError("Set ADMIN_PASSWORD (at least 12 characters) before first launch")
        db.execute("INSERT INTO users(username, password_hash) VALUES(?, ?)",
                   ("admin", generate_password_hash(initial_password)))
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
    return getattr(g, "api_admin", False) or session.get("user") == "admin"


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
    if request.method == "POST" and not request.path.startswith("/api/"):
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



from flask import jsonify
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from flask_cors import CORS
from werkzeug.exceptions import HTTPException

allowed_origins = [x.strip().rstrip('/') for x in os.environ.get('FRONTEND_ORIGIN','http://localhost:8000').split(',') if x.strip()]
CORS(app, resources={r'/api/*': {'origins': allowed_origins}}, supports_credentials=False)
signer = URLSafeTimedSerializer(app.secret_key, salt='library-api-v1')

@app.before_request
def api_auth():
    g.api_admin = False
    if request.path.startswith('/api/'):
        token = request.headers.get('Authorization','').removeprefix('Bearer ').strip()
        if token:
            try:
                payload = signer.loads(token, max_age=8*3600)
                g.api_admin = payload.get('user') == 'admin'
            except (BadSignature, SignatureExpired):
                pass

@app.errorhandler(HTTPException)
def api_error(e):
    if request.path.startswith('/api/'):
        return jsonify(error=e.description), e.code
    return e

@app.errorhandler(Exception)
def unexpected_error(e):
    if request.path.startswith('/api/'):
        app.logger.exception('API error')
        return jsonify(error='Lỗi máy chủ'), 500
    raise e

def required():
    if not g.api_admin:
        abort(401, 'Bạn cần đăng nhập quản trị')

def data():
    return request.get_json(silent=True) or {}

def rowdict(r):
    return dict(r) if r is not None else None

def number(value, lo=0, hi=1000):
    try:
        n=int(value)
    except (TypeError,ValueError):
        abort(400,'Giá trị số không hợp lệ')
    if not lo <= n <= hi:
        abort(400, f'Giá trị phải từ {lo} đến {hi}')
    return n

@app.get('/api/health')
def health():
    return jsonify(status='ok')

@app.post('/api/login')
def api_login():
    d=data()
    user=get_db().execute('SELECT * FROM users WHERE username=?',(str(d.get('username','')),)).fetchone()
    if not user or not check_password_hash(user['password_hash'],str(d.get('password',''))):
        abort(401,'Sai tài khoản hoặc mật khẩu')
    return jsonify(token=signer.dumps({'user':user['username']}), username=user['username'])

@app.get('/api/me')
def me():
    return jsonify(admin=g.api_admin)

@app.post('/api/password')
def password():
    required(); d=data(); db=get_db()
    user=db.execute("SELECT * FROM users WHERE username='admin'").fetchone()
    if not check_password_hash(user['password_hash'],str(d.get('old',''))): abort(400,'Mật khẩu cũ không đúng')
    new=str(d.get('new',''))
    if len(new)<12 or new!=d.get('confirm'): abort(400,'Mật khẩu mới phải có ít nhất 12 ký tự và xác nhận khớp')
    db.execute('UPDATE users SET password_hash=? WHERE id=?',(generate_password_hash(new),user['id']))
    db.commit()
    # Rotate signing secret on password change to invalidate previous tokens
    newkey=secrets.token_hex(32)
    with open(KEY_PATH,'w') as f: f.write(newkey)
    app.secret_key=newkey
    global signer
    signer=URLSafeTimedSerializer(newkey,salt='library-api-v1')
    return jsonify(message='Đã đổi mật khẩu, vui lòng đăng nhập lại')

@app.get('/api/catalog')
def catalog():
    db=get_db(); d=request.args
    series=[dict(s) for s in db.execute(SERIES_SQL+' GROUP BY s.id').fetchall()]
    stats=dict(series=len(series),vols=sum(s['nvol'] for s in series),copies=sum(s['total'] for s in series),borrowed=sum(s['borrowed'] for s in series))
    q=d.get('q','').strip().casefold(); kind=d.get('kind',''); category=d.get('category',''); avail=d.get('avail','')
    categories=sorted({s['category'] for s in series if s['category']})
    filtered=[]
    for s in series:
        if q and q not in s['title'].casefold() and q not in (s['author'] or '').casefold():continue
        if kind in KINDS and s['kind']!=kind:continue
        if category and s['category']!=category:continue
        if avail=='1' and s['total']-s['borrowed']<=0:continue
        if avail=='0' and s['borrowed']<=0:continue
        filtered.append(s)
    volumes=[dict(v) for v in db.execute(VOL_SQL+' ORDER BY v.series_id,v.vol_no')]
    return jsonify(series=sorted(filtered,key=lambda s:(s['kind'],s['title'].casefold())),volumes=volumes,stats=stats,categories=categories)

@app.get('/api/loans')
def api_loans():
    status=request.args.get('status','active')
    sql='SELECT l.*,s.title,s.kind,s.single,v.vol_no FROM loans l JOIN volumes v ON v.id=l.volume_id JOIN series s ON s.id=v.series_id'
    if status=='active':sql+=' WHERE l.return_date IS NULL'
    elif status=='returned':sql+=' WHERE l.return_date IS NOT NULL'
    sql+=' ORDER BY l.return_date IS NOT NULL,l.due_date ASC,l.id DESC'
    rows=[dict(x) for x in get_db().execute(sql)]
    if not g.api_admin:
        for x in rows:
            x.pop('borrower',None);x.pop('phone',None)
    return jsonify(loans=rows)

@app.post('/api/series')
def create_series():
    required(); d=data(); db=get_db()
    title=str(d.get('title','')).strip(); kind=d.get('kind','Sách')
    if not title or kind not in KINDS:abort(400,'Tên sách hoặc loại sách không hợp lệ')
    single=bool(d.get('single')); n=1 if single else number(d.get('nvol'),1,500)
    copies=number(d.get('copies',1))
    with db:
        cur=db.execute('INSERT INTO series(title,author,kind,category,note,single) VALUES(?,?,?,?,?,?)',(title,str(d.get('author','')).strip(),kind,str(d.get('category','')).strip(),str(d.get('note','')).strip(),int(single)))
        db.executemany('INSERT INTO volumes(series_id,vol_no,copies) VALUES(?,?,?)',[(cur.lastrowid,i,copies) for i in range(1,n+1)])
    return jsonify(id=cur.lastrowid),201

@app.put('/api/series/<int:sid>')
def update_series(sid):
    required();d=data();db=get_db();title=str(d.get('title','')).strip();kind=d.get('kind')
    if not title or kind not in KINDS:abort(400,'Thông tin không hợp lệ')
    with db:
        cur=db.execute('UPDATE series SET title=?,author=?,kind=?,category=?,note=? WHERE id=?',(title,str(d.get('author','')).strip(),kind,str(d.get('category','')).strip(),str(d.get('note','')).strip(),sid))
    if not cur.rowcount:abort(404)
    return jsonify(message='Đã lưu')

@app.delete('/api/series/<int:sid>')
def delete_series(sid):
    required();db=get_db();s=db.execute(SERIES_SQL+' WHERE s.id=? GROUP BY s.id',(sid,)).fetchone()
    if not s:abort(404)
    if s['borrowed']:abort(409,'Không thể xóa khi còn sách đang mượn')
    with db:db.execute('DELETE FROM series WHERE id=?',(sid,))
    return jsonify(message='Đã xóa')

@app.put('/api/series/<int:sid>/volumes')
def save_volumes(sid):
    required();db=get_db();d=data();updates=[]
    for v in db.execute(VOL_SQL+' WHERE v.series_id=?',(sid,)):
        key=str(v['id'])
        if key in d:
            n=number(d[key]);
            if n<v['borrowed']:abort(409,f"Tập {v['vol_no']} có {v['borrowed']} quyển đang mượn")
            updates.append((n,v['id']))
    with db:db.executemany('UPDATE volumes SET copies=? WHERE id=?',updates)
    return jsonify(message='Đã lưu')

@app.post('/api/series/<int:sid>/volumes')
def add_volumes(sid):
    required();db=get_db();d=data();a=number(d.get('from'),1,100000);b=number(d.get('to',a),a,min(a+500,100000));c=number(d.get('copies',1))
    if not db.execute('SELECT 1 FROM series WHERE id=? AND single=0',(sid,)).fetchone():abort(404)
    with db:
        for n in range(a,b+1):db.execute('INSERT OR IGNORE INTO volumes(series_id,vol_no,copies) VALUES(?,?,?)',(sid,n,c))
    return jsonify(message='Đã thêm tập')

@app.delete('/api/volumes/<int:vid>')
def delete_volume(vid):
    required();db=get_db();v=db.execute(VOL_SQL+' WHERE v.id=?',(vid,)).fetchone()
    if not v:abort(404)
    if v['borrowed']:abort(409,'Tập vẫn đang được mượn')
    if db.execute('SELECT single FROM series WHERE id=?',(v['series_id'],)).fetchone()['single']:abort(409,'Không thể xóa tập duy nhất của sách lẻ')
    with db:db.execute('DELETE FROM volumes WHERE id=?',(vid,))
    return jsonify(message='Đã xóa tập')

@app.post('/api/loans')
def create_loan():
    required();db=get_db();d=data();vid=number(d.get('volume_id'),1,100000000);name=str(d.get('borrower','')).strip()
    start=d.get('borrow_date');due=d.get('due_date')
    if not name or not valid_date(start) or not valid_date(due) or due<start:abort(400,'Thông tin người mượn hoặc ngày không hợp lệ')
    # BEGIN IMMEDIATE serializes writers before checking availability.
    try:
        db.execute('BEGIN IMMEDIATE')
        v=db.execute(VOL_SQL+' WHERE v.id=?',(vid,)).fetchone()
        if not v or v['copies']<=v['borrowed']:
            db.rollback();abort(409,'Sách đã hết hoặc không tồn tại')
        db.execute('INSERT INTO loans(volume_id,borrower,phone,borrow_date,due_date) VALUES(?,?,?,?,?)',(vid,name,str(d.get('phone','')).strip(),start,due))
        db.commit()
    except Exception:
        db.rollback();raise
    return jsonify(message='Đã cho mượn'),201

@app.post('/api/loans/<int:lid>/return')
def return_loan(lid):
    required();db=get_db();d=data();day=d.get('return_date') or date.today().isoformat()
    if not valid_date(day):abort(400,'Ngày trả không hợp lệ')
    loan=db.execute('SELECT borrow_date FROM loans WHERE id=? AND return_date IS NULL',(lid,)).fetchone()
    if not loan:abort(404,'Không tìm thấy giao dịch đang mượn')
    if day<loan['borrow_date']:abort(400,'Ngày trả không thể trước ngày mượn')
    with db:db.execute('UPDATE loans SET return_date=? WHERE id=? AND return_date IS NULL',(day,lid))
    return jsonify(message='Đã trả sách')

@app.delete('/api/loans/<int:lid>')
def delete_loan(lid):
    required();db=get_db()
    with db:cur=db.execute('DELETE FROM loans WHERE id=?',(lid,))
    if not cur.rowcount:abort(404)
    return jsonify(message='Đã xóa giao dịch')

init_db()
if __name__=='__main__':
    app.run(host='127.0.0.1',port=5000,debug=False)
