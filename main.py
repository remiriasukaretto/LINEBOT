import os
import re
import secrets
import time
from datetime import timedelta
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import psycopg2
from flask import Flask, request, abort, render_template, redirect, url_for, session, jsonify
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


def parse_bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def normalize_db_url(raw_url: str) -> str:
    url = raw_url.replace("postgres://", "postgresql://", 1)
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        raise RuntimeError("DATABASE_URL is invalid")
    # 本番ではTLS必須。ローカル開発時はlocalhostのみ緩和する。
    local_hosts = {"localhost", "127.0.0.1"}
    if parsed.hostname not in local_hosts:
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query.setdefault("sslmode", "require")
        url = urlunparse(
            (parsed.scheme, parsed.netloc, parsed.path, parsed.params, urlencode(query), parsed.fragment)
        )
    return url

# --- セキュリティ設定 ---
SECRET_KEY = os.getenv('SECRET_KEY')
if not SECRET_KEY:
    raise RuntimeError("SECRET_KEY is required")
app.secret_key = SECRET_KEY

ADMIN_PASSWORD_HASH = (os.getenv('ADMIN_PASSWORD_HASH') or "").strip()
if not ADMIN_PASSWORD_HASH:
    raise RuntimeError("ADMIN_PASSWORD_HASH is required")
if os.getenv("ADMIN_PASSWORD"):
    app.logger.warning("ADMIN_PASSWORD is deprecated and ignored. Use ADMIN_PASSWORD_HASH only.")

CHANNEL_ACCESS_TOKEN = (os.getenv('CHANNEL_ACCESS_TOKEN') or "").strip()
CHANNEL_SECRET = (os.getenv('CHANNEL_SECRET') or "").strip()
if not CHANNEL_ACCESS_TOKEN or not CHANNEL_SECRET:
    raise RuntimeError("CHANNEL_ACCESS_TOKEN and CHANNEL_SECRET are required")

raw_db_url = (os.getenv('DATABASE_URL') or "").strip()
if not raw_db_url:
    raise RuntimeError("DATABASE_URL is required")
DATABASE_URL = normalize_db_url(raw_db_url)
DB_CONNECT_TIMEOUT = int(os.getenv("DB_CONNECT_TIMEOUT", "5"))

OWNER_LINE_ID = os.getenv('OWNER_LINE_ID', '').strip()
FORCE_HTTPS = parse_bool_env("FORCE_HTTPS", True)
ALLOWED_HOSTS = {
    host.strip().lower() for host in os.getenv("ALLOWED_HOSTS", "").split(",") if host.strip()
}
SESSION_IDLE_TIMEOUT_SECONDS = int(os.getenv("SESSION_IDLE_TIMEOUT_SECONDS", "1800"))
MAX_TYPE_NAME_LENGTH = int(os.getenv("MAX_TYPE_NAME_LENGTH", "40"))
MAX_USER_MESSAGE_CHARS = int(os.getenv("MAX_USER_MESSAGE_CHARS", "100"))
TYPE_NAME_PATTERN = re.compile(
    rf"^[A-Za-z0-9ぁ-んァ-ヶー一-龠々・ 　_-]{{1,{MAX_TYPE_NAME_LENGTH}}}$"
)

WEBHOOK_RATE_LIMIT_COUNT = int(os.getenv("WEBHOOK_RATE_LIMIT_COUNT", "120"))
WEBHOOK_RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("WEBHOOK_RATE_LIMIT_WINDOW_SECONDS", "60"))
WEBHOOK_REQUESTS = {}

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=parse_bool_env("SESSION_COOKIE_SECURE", True),
    SESSION_COOKIE_NAME="__Host-session" if parse_bool_env("SESSION_COOKIE_SECURE", True) else "session",
    PERMANENT_SESSION_LIFETIME=timedelta(seconds=SESSION_IDLE_TIMEOUT_SECONDS),
)
app.jinja_env.autoescape = True

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

def get_connection():
    return psycopg2.connect(DATABASE_URL, connect_timeout=DB_CONNECT_TIMEOUT)

def verify_admin_password(candidate: str) -> bool:
    if not candidate:
        return False
    return check_password_hash(ADMIN_PASSWORD_HASH, candidate)


def is_local_host(host: str) -> bool:
    return host in {"localhost", "127.0.0.1"}


def enforce_host_allowlist():
    if not ALLOWED_HOSTS:
        return
    host = (request.host.split(":", 1)[0] if request.host else "").lower()
    if host not in ALLOWED_HOSTS:
        abort(400)


def enforce_https():
    if not FORCE_HTTPS:
        return
    host = (request.host.split(":", 1)[0] if request.host else "").lower()
    if is_local_host(host):
        return
    if request.is_secure:
        return
    forwarded_proto = (request.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
    if forwarded_proto == "https":
        return
    secure_url = request.url.replace("http://", "https://", 1)
    return redirect(secure_url, code=301)


def start_admin_session():
    now = time.time()
    session.clear()
    session["logged_in"] = True
    session["issued_at"] = now
    session["last_activity"] = now
    session["_csrf_token"] = secrets.token_urlsafe(32)
    session.permanent = True


def is_admin_authenticated(update_activity: bool = True) -> bool:
    if not session.get("logged_in"):
        return False
    last_activity = session.get("last_activity")
    if not isinstance(last_activity, (int, float)):
        session.clear()
        return False
    now = time.time()
    if now - last_activity > SESSION_IDLE_TIMEOUT_SECONDS:
        session.clear()
        return False
    if update_activity:
        session["last_activity"] = now
        session.modified = True
    return True


def normalize_type_name(value: str) -> str:
    return " ".join((value or "").split())


def validate_type_name(value: str) -> bool:
    if not value or len(value) > MAX_TYPE_NAME_LENGTH:
        return False
    return bool(TYPE_NAME_PATTERN.fullmatch(value))

def get_csrf_token() -> str:
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token

def validate_csrf():
    token = session.get("_csrf_token")
    request_token = request.form.get("_csrf_token") or request.headers.get("X-CSRF-Token")
    if not token or not request_token or not secrets.compare_digest(token, request_token):
        abort(403)


@app.before_request
def security_preflight():
    enforce_host_allowlist()
    secure_redirect = enforce_https()
    if secure_redirect:
        return secure_redirect


@app.before_request
def csrf_protect():
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        if request.path == "/callback":
            return
        validate_csrf()


@app.after_request
def apply_security_headers(response):
    csp = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' https://cdn.jsdelivr.net; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    )
    response.headers.setdefault("Content-Security-Policy", csp)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    forwarded_proto = (request.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
    if FORCE_HTTPS and (request.is_secure or forwarded_proto == "https"):
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    if request.path.startswith("/admin") or request.path.startswith("/login"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
    return response

LOGIN_ATTEMPTS = {}
LOGIN_MAX_ATTEMPTS = int(os.getenv("LOGIN_MAX_ATTEMPTS", "10"))
LOGIN_WINDOW_SECONDS = int(os.getenv("LOGIN_WINDOW_SECONDS", "300"))

def is_login_rate_limited(ip: str) -> bool:
    now = time.time()
    window_start = now - LOGIN_WINDOW_SECONDS
    attempts = [t for t in LOGIN_ATTEMPTS.get(ip, []) if t > window_start]
    LOGIN_ATTEMPTS[ip] = attempts
    return len(attempts) >= LOGIN_MAX_ATTEMPTS

def record_login_failure(ip: str):
    LOGIN_ATTEMPTS.setdefault(ip, []).append(time.time())


def is_webhook_rate_limited(ip: str) -> bool:
    now = time.time()
    window_start = now - WEBHOOK_RATE_LIMIT_WINDOW_SECONDS
    attempts = [t for t in WEBHOOK_REQUESTS.get(ip, []) if t > window_start]
    WEBHOOK_REQUESTS[ip] = attempts
    if len(attempts) >= WEBHOOK_RATE_LIMIT_COUNT:
        return True
    WEBHOOK_REQUESTS[ip].append(now)
    return False

# --- ルーティング ---

@app.route("/")
def index():
    return redirect(url_for("login"))

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    ip = request.remote_addr or "unknown"
    if request.method == "POST":
        if is_login_rate_limited(ip):
            abort(429)
        if verify_admin_password(request.form.get("password")):
            start_admin_session()
            LOGIN_ATTEMPTS.pop(ip, None)
            return redirect(url_for("admin_page"))
        else:
            record_login_failure(ip)
            error = "パスワードが正しくありません"
    return render_template("login.html", error=error, csrf_token=get_csrf_token())

def ensure_types_table():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS reservation_types (
                    id SERIAL PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    accepting BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute("""
                ALTER TABLE reservations
                ADD COLUMN IF NOT EXISTS type_id INTEGER
                REFERENCES reservation_types(id) ON DELETE SET NULL
            """)
            cur.execute("""
                ALTER TABLE reservation_types
                ADD COLUMN IF NOT EXISTS accepting BOOLEAN NOT NULL DEFAULT TRUE
            """)
            conn.commit()

def ensure_settings_table():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)
            cur.execute("""
                INSERT INTO app_settings (key, value)
                VALUES ('accepting_new', 'true')
                ON CONFLICT (key) DO NOTHING
            """)
            conn.commit()

def is_accepting_new():
    ensure_settings_table()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM app_settings WHERE key = 'accepting_new'")
            row = cur.fetchone()
            return (row and row[0] == 'true')

def set_accepting_new(flag: bool):
    ensure_settings_table()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE app_settings SET value = %s WHERE key = 'accepting_new'",
                ('true' if flag else 'false',)
            )
            conn.commit()

@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/admin")
def admin_page():
    if not is_admin_authenticated():
        return redirect(url_for("login"))

    ensure_types_table()
    type_error = request.args.get("type_error")
    type_id = request.args.get("type_id", "").strip()
    current_type_id = int(type_id) if type_id.isdigit() else None
    sort_by = request.args.get("sort_by", "id").strip()
    sort_order = request.args.get("sort_order", "asc").strip().lower()
    if sort_by not in ("id", "status", "type", "message"):
        sort_by = "id"
    if sort_order not in ("asc", "desc"):
        sort_order = "asc"
    accepting_new = is_accepting_new()

    with get_connection() as conn:
        with conn.cursor() as cur:
            params = []
            where = "WHERE r.status IN ('waiting', 'called', 'arrived')"
            if current_type_id is not None:
                where += " AND r.type_id = %s"
                params.append(current_type_id)
            order_map = {
                "id": "r.id",
                "status": "r.status",
                "type": "t.name",
                "message": "r.message"
            }
            order_by = order_map[sort_by]
            cur.execute(f"""
                SELECT r.id, r.user_id, r.message, r.status, t.name
                FROM reservations r
                LEFT JOIN reservation_types t ON r.type_id = t.id
                {where}
                ORDER BY {order_by} {sort_order.upper()}, r.id ASC
            """, params)
            rows = cur.fetchall()
            cur.execute("SELECT id, name FROM reservation_types ORDER BY id ASC")
            types = cur.fetchall()
            cur.execute("""
                SELECT COALESCE(t.name, '未設定') AS name, COUNT(*)
                FROM reservations r
                LEFT JOIN reservation_types t ON r.type_id = t.id
                WHERE r.status IN ('waiting', 'called', 'arrived')
                GROUP BY COALESCE(t.name, '未設定')
                ORDER BY COUNT(*) DESC
            """)
            type_counts = cur.fetchall()
    return render_template(
        "admin.html",
        rows=rows,
        types=types,
        type_error=type_error,
        current_type_id=current_type_id,
        type_counts=type_counts,
        sort_by=sort_by,
        sort_order=sort_order,
        accepting_new=accepting_new,
        csrf_token=get_csrf_token()
    )

@app.route("/admin/data")
def admin_data():
    if not is_admin_authenticated():
        return jsonify({"error": "unauthorized"}), 401

    with get_connection() as conn:
        with conn.cursor() as cur:
            type_id = request.args.get("type_id", "").strip()
            current_type_id = int(type_id) if type_id.isdigit() else None
            sort_by = request.args.get("sort_by", "id").strip()
            sort_order = request.args.get("sort_order", "asc").strip().lower()
            if sort_by not in ("id", "status", "type", "message"):
                sort_by = "id"
            if sort_order not in ("asc", "desc"):
                sort_order = "asc"
            params = []
            where = "WHERE r.status IN ('waiting', 'called', 'arrived')"
            if current_type_id is not None:
                where += " AND r.type_id = %s"
                params.append(current_type_id)
            order_map = {
                "id": "r.id",
                "status": "r.status",
                "type": "t.name",
                "message": "r.message"
            }
            order_by = order_map[sort_by]
            cur.execute(f"""
                SELECT r.id, r.message, r.status, t.name
                FROM reservations r
                LEFT JOIN reservation_types t ON r.type_id = t.id
                {where}
                ORDER BY {order_by} {sort_order.upper()}, r.id ASC
            """, params)
            rows = cur.fetchall()
    return jsonify({
        "rows": [
            {"id": row[0], "message": row[1], "status": row[2], "type": row[3]}
            for row in rows
        ]
    })

@app.route("/admin/type_counts")
def admin_type_counts():
    if not is_admin_authenticated():
        return jsonify({"error": "unauthorized"}), 401

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COALESCE(t.name, '未設定') AS name, COUNT(*)
                FROM reservations r
                LEFT JOIN reservation_types t ON r.type_id = t.id
                WHERE r.status IN ('waiting', 'called', 'arrived')
                GROUP BY COALESCE(t.name, '未設定')
                ORDER BY COUNT(*) DESC
            """)
            counts = cur.fetchall()
    return jsonify({
        "counts": [
            {"name": row[0], "count": row[1]}
            for row in counts
        ]
    })

@app.route("/admin/types", methods=["GET", "POST"])
def admin_types_page():
    if not is_admin_authenticated():
        return redirect(url_for("login"))

    ensure_types_table()
    type_error = request.args.get("type_error")
    type_success = request.args.get("type_success")
    if request.method == "POST":
        name = normalize_type_name(request.form.get("name"))
        if not validate_type_name(name):
            return redirect(
                url_for(
                    "admin_types_page",
                    type_error=f"種類名は1〜{MAX_TYPE_NAME_LENGTH}文字、英数字/日本語/スペース/記号(-_・)のみ使用できます。",
                )
            )
        try:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("INSERT INTO reservation_types (name) VALUES (%s)", (name,))
                    conn.commit()
            return redirect(url_for("admin_types_page", type_success="種類を追加しました。"))
        except psycopg2.IntegrityError:
            return redirect(url_for("admin_types_page", type_error="同じ名前の種類が既に存在します。"))

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, accepting FROM reservation_types ORDER BY id ASC")
            types = cur.fetchall()
    return render_template(
        "types.html",
        types=types,
        type_error=type_error,
        type_success=type_success,
        csrf_token=get_csrf_token()
    )

@app.route("/admin/types/delete/<int:type_id>", methods=["POST"])
def admin_types_delete(type_id):
    if not is_admin_authenticated():
        return redirect(url_for("login"))

    ensure_types_table()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM reservation_types WHERE id = %s", (type_id,))
            conn.commit()
    return redirect(url_for("admin_types_page"))

@app.route("/admin/types/toggle/<int:type_id>", methods=["POST"])
def admin_types_toggle(type_id):
    if not is_admin_authenticated():
        return redirect(url_for("login"))

    ensure_types_table()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE reservation_types SET accepting = NOT accepting WHERE id = %s", (type_id,))
            conn.commit()
    return redirect(url_for("admin_types_page"))

@app.route("/admin/history")
def admin_history():
    if not is_admin_authenticated():
        return redirect(url_for("login"))

    ensure_types_table()
    with get_connection() as conn:
        with conn.cursor() as cur:
            type_id = request.args.get("type_id", "").strip()
            current_type_id = int(type_id) if type_id.isdigit() else None
            sort_by = request.args.get("sort_by", "id").strip()
            sort_order = request.args.get("sort_order", "desc").strip().lower()
            if sort_by not in ("id", "status", "type", "message"):
                sort_by = "id"
            if sort_order not in ("asc", "desc"):
                sort_order = "desc"
            params = []
            where = "WHERE r.status IN ('done', 'cancelled', 'arrived')"
            if current_type_id is not None:
                where += " AND r.type_id = %s"
                params.append(current_type_id)
            order_map = {
                "id": "r.id",
                "status": "r.status",
                "type": "t.name",
                "message": "r.message"
            }
            order_by = order_map[sort_by]
            cur.execute(f"""
                SELECT r.id, r.user_id, r.message, r.status, t.name
                FROM reservations r
                LEFT JOIN reservation_types t ON r.type_id = t.id
                {where}
                ORDER BY {order_by} {sort_order.upper()}, r.id DESC LIMIT 200
            """, params)
            rows = cur.fetchall()
            cur.execute("SELECT id, name FROM reservation_types ORDER BY id ASC")
            types = cur.fetchall()
    return render_template(
        "history.html",
        rows=rows,
        types=types,
        current_type_id=current_type_id,
        sort_by=sort_by,
        sort_order=sort_order,
        csrf_token=get_csrf_token()
    )

@app.route("/admin/call/<int:res_id>", methods=["POST"])
def admin_call(res_id):
    if not is_admin_authenticated():
        return redirect(url_for("login"))

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_id FROM reservations WHERE id = %s AND status = 'waiting'",
                (res_id,),
            )
            row = cur.fetchone()
            if not row:
                abort(404)
            user_id = row[0]
            cur.execute("UPDATE reservations SET status = 'called' WHERE id = %s", (res_id,))
            conn.commit()
            try:
                line_bot_api.push_message(
                    user_id,
                    TextSendMessage(text=f"【順番が来ました】番号 {res_id} 番の方、会場へお越しください！"),
                )
            except Exception:
                app.logger.exception("Failed to send LINE push message for reservation %s", res_id)
    return redirect(url_for("admin_page"))

@app.route("/admin/finish/<int:res_id>", methods=["POST"])
def admin_finish(res_id):
    if not is_admin_authenticated():
        return redirect(url_for("login"))

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE reservations SET status = 'done' WHERE id = %s AND status = 'arrived' RETURNING id",
                (res_id,),
            )
            if not cur.fetchone():
                abort(404)
            conn.commit()
    return redirect(url_for("admin_page"))

@app.route("/admin/toggle-accepting", methods=["POST"])
def admin_toggle_accepting():
    if not is_admin_authenticated():
        return redirect(url_for("login"))
    set_accepting_new(not is_accepting_new())
    return redirect(url_for("admin_page"))

# --- LINE Webhook ---
@app.route("/callback", methods=['POST'])
def callback():
    ip = request.remote_addr or "unknown"
    if is_webhook_rate_limited(ip):
        abort(429)
    signature = request.headers.get('X-Line-Signature')
    if not signature:
        abort(400)
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_message = event.message.text.strip()
    user_id = event.source.user_id
    process_reservation(event, user_id, user_message)

def process_reservation(event, user_id, user_message):
    normalized = user_message.strip()
    if not normalized:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="メッセージを受け付けました。予約は「予約」、キャンセルは「キャンセル」、到着は「到着」と送信してください。")
        )
        return
    if len(normalized) > MAX_USER_MESSAGE_CHARS:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=f"メッセージは{MAX_USER_MESSAGE_CHARS}文字以内で送信してください。"),
        )
        return

    with get_connection() as conn:
        with conn.cursor() as cur:
            if normalized.startswith('予約'):
                if not is_accepting_new():
                    reply = "現在、新規の予約受付は停止中です。"
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
                    return
                ensure_types_table()
                requested_type_name = normalize_type_name(normalized[2:])
                type_id = None
                type_name = None
                if requested_type_name:
                    if not validate_type_name(requested_type_name):
                        reply = (
                            f"種類名は1〜{MAX_TYPE_NAME_LENGTH}文字で指定してください。"
                            "\n例: 予約 相談"
                        )
                        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
                        return
                    cur.execute("SELECT id, name, accepting FROM reservation_types WHERE name = %s", (requested_type_name,))
                    type_row = cur.fetchone()
                    if not type_row:
                        cur.execute("SELECT name FROM reservation_types WHERE accepting = TRUE ORDER BY id ASC")
                        names = [r[0] for r in cur.fetchall()]
                        if names:
                            reply = f"指定した種類「{requested_type_name}」は存在しません。\n利用可能: " + " / ".join(names)
                        else:
                            reply = "予約の種類がまだ登録されていません。管理画面で追加してください。"
                        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
                        return
                    type_id, type_name, type_accepting = type_row
                    if not type_accepting:
                        cur.execute("SELECT name FROM reservation_types WHERE accepting = TRUE ORDER BY id ASC")
                        names = [r[0] for r in cur.fetchall()]
                        if names:
                            reply = f"「{type_name}」の新規受付は停止中です。\n利用可能: " + " / ".join(names)
                        else:
                            reply = f"「{type_name}」の新規受付は停止中です。"
                        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
                        return
                else:
                    cur.execute("SELECT name FROM reservation_types WHERE accepting = TRUE ORDER BY id ASC")
                    names = [r[0] for r in cur.fetchall()]
                    if names:
                        reply = "予約の種類を指定してください。\n利用可能: " + " / ".join(names) + "\n例: 予約 相談"
                    else:
                        reply = "現在受付可能な予約の種類がありません。管理画面で受付を再開してください。"
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
                    return

                cur.execute(
                    """
                        SELECT r.id, r.status, t.name
                        FROM reservations r
                        LEFT JOIN reservation_types t ON r.type_id = t.id
                        WHERE r.user_id = %s AND r.status IN ('waiting', 'called', 'arrived')
                        ORDER BY r.id DESC LIMIT 1
                    """,
                    (user_id,)
                )
                existing = cur.fetchone()
                if existing:
                    res_id, status, existing_type_name = existing
                    if status == 'waiting':
                        if existing_type_name:
                            cur.execute("SELECT COUNT(*) FROM reservations WHERE status = 'waiting' AND id < %s AND type_id = (SELECT type_id FROM reservations WHERE id = %s)", (res_id, res_id))
                            reply = f"予約済みです。番号: {res_id} / 種類: {existing_type_name} / 待ち: {cur.fetchone()[0]}人"
                        else:
                            cur.execute("SELECT COUNT(*) FROM reservations WHERE status = 'waiting' AND id < %s", (res_id,))
                            reply = f"予約済みです。番号: {res_id} / 待ち: {cur.fetchone()[0]}人"
                    elif status == 'called':
                        if existing_type_name:
                            reply = f"【呼出中】番号: {res_id} / 種類: {existing_type_name} 会場へお越しください！"
                        else:
                            reply = f"【呼出中】番号: {res_id} 会場へお越しください！"
                    else:
                        if existing_type_name:
                            reply = f"到着受付済みです。番号: {res_id} / 種類: {existing_type_name} / スタッフが確認します。"
                        else:
                            reply = f"到着受付済みです。番号: {res_id} / スタッフが確認します。"
                else:
                    cur.execute("INSERT INTO reservations (user_id, message, type_id) VALUES (%s, %s, %s) RETURNING id", (user_id, user_message, type_id))
                    new_id = cur.fetchone()[0]
                    conn.commit()
                    if type_id:
                        cur.execute("SELECT COUNT(*) FROM reservations WHERE status = 'waiting' AND id < %s AND type_id = %s", (new_id, type_id))
                        reply = f"【受付完了】番号: {new_id} / 種類: {type_name} / 待ち: {cur.fetchone()[0]}人"
                    else:
                        cur.execute("SELECT COUNT(*) FROM reservations WHERE status = 'waiting' AND id < %s", (new_id,))
                        reply = f"【受付完了】番号: {new_id} / 待ち: {cur.fetchone()[0]}人"
            elif normalized == 'キャンセル':
                cur.execute(
                    "UPDATE reservations SET status = 'cancelled' WHERE id = (SELECT id FROM reservations WHERE user_id = %s AND status IN ('waiting', 'called') ORDER BY id DESC LIMIT 1) RETURNING id",
                    (user_id,)
                )
                cancelled = cur.fetchone()
                if cancelled:
                    reply = f"予約番号 {cancelled[0]} をキャンセルしました。"
                else:
                    reply = "キャンセル対象の予約はありません。"
            elif normalized == '到着':
                cur.execute(
                    "SELECT id, status FROM reservations WHERE user_id = %s AND status IN ('waiting', 'called') ORDER BY id DESC LIMIT 1",
                    (user_id,)
                )
                existing = cur.fetchone()
                if not existing:
                    reply = "到着の対象となる予約がありません。"
                else:
                    res_id, status = existing
                    if status == 'waiting':
                        reply = "まだ呼出されていません。呼出後に「到着」と送信してください。"
                    else:
                        cur.execute("UPDATE reservations SET status = 'arrived' WHERE id = %s", (res_id,))
                        conn.commit()
                        reply = f"到着を受け付けました。番号: {res_id} / スタッフが確認します。"
            else:
                reply = "メッセージを受け付けました。予約は「予約」、キャンセルは「キャンセル」、到着は「到着」と送信してください。"
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
