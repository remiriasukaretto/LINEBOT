import os
import secrets
import time
import psycopg2
from flask import Flask, request, abort, render_template, redirect, url_for, session, jsonify
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from datetime import datetime
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)

# --- セキュリティ設定 ---
SECRET_KEY = os.getenv('SECRET_KEY')
if not SECRET_KEY:
    raise RuntimeError("SECRET_KEY is required")
app.secret_key = SECRET_KEY

ADMIN_PASSWORD_HASH = os.getenv('ADMIN_PASSWORD_HASH')
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD')
if not ADMIN_PASSWORD_HASH and ADMIN_PASSWORD:
    ADMIN_PASSWORD_HASH = generate_password_hash(ADMIN_PASSWORD)
if not ADMIN_PASSWORD_HASH:
    raise RuntimeError("ADMIN_PASSWORD_HASH or ADMIN_PASSWORD is required")

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("SESSION_COOKIE_SECURE", "true").lower() == "true",
)
app.jinja_env.autoescape = True

CHANNEL_ACCESS_TOKEN = os.getenv('CHANNEL_ACCESS_TOKEN')
CHANNEL_SECRET = os.getenv('CHANNEL_SECRET')
OWNER_LINE_ID = os.getenv('OWNER_LINE_ID', '').strip()

raw_db_url = os.getenv('DATABASE_URL')
DATABASE_URL = raw_db_url.replace("postgres://", "postgresql://", 1) if raw_db_url else None

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

def get_connection():
    return psycopg2.connect(DATABASE_URL)

def verify_admin_password(candidate: str) -> bool:
    if not candidate:
        return False
    return check_password_hash(ADMIN_PASSWORD_HASH, candidate)

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
def csrf_protect():
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        if request.path == "/callback":
            return
        validate_csrf()

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
            session["logged_in"] = True
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
    session.pop("logged_in", None)
    return redirect(url_for("login"))

@app.route("/admin")
def admin_page():
    if not session.get("logged_in"):
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
    if not session.get("logged_in"):
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
    if not session.get("logged_in"):
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
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    ensure_types_table()
    type_error = request.args.get("type_error")
    type_success = request.args.get("type_success")
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        if not name:
            return redirect(url_for("admin_types_page", type_error="種類名を入力してください。"))
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
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    ensure_types_table()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM reservation_types WHERE id = %s", (type_id,))
            conn.commit()
    return redirect(url_for("admin_types_page"))

@app.route("/admin/types/toggle/<int:type_id>", methods=["POST"])
def admin_types_toggle(type_id):
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    ensure_types_table()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE reservation_types SET accepting = NOT accepting WHERE id = %s", (type_id,))
            conn.commit()
    return redirect(url_for("admin_types_page"))

@app.route("/admin/history")
def admin_history():
    if not session.get("logged_in"):
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
    if not session.get("logged_in"): return redirect(url_for("login"))

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT user_id FROM reservations WHERE id = %s", (res_id,))
            user_id = cur.fetchone()[0]
            cur.execute("UPDATE reservations SET status = 'called' WHERE id = %s", (res_id,))
            conn.commit()
            line_bot_api.push_message(user_id, TextSendMessage(text=f"【順番が来ました】番号 {res_id} 番の方、会場へお越しください！"))
    return redirect(url_for("admin_page"))

@app.route("/admin/finish/<int:res_id>", methods=["POST"])
def admin_finish(res_id):
    if not session.get("logged_in"): return redirect(url_for("login"))

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE reservations SET status = 'done' WHERE id = %s", (res_id,))
            conn.commit()
    return redirect(url_for("admin_page"))

@app.route("/admin/toggle-accepting", methods=["POST"])
def admin_toggle_accepting():
    if not session.get("logged_in"):
        return redirect(url_for("login"))
    set_accepting_new(not is_accepting_new())
    return redirect(url_for("admin_page"))

# --- LINE Webhook ---
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
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

    with get_connection() as conn:
        with conn.cursor() as cur:
            if normalized.startswith('予約'):
                if not is_accepting_new():
                    reply = "現在、新規の予約受付は停止中です。"
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
                    return
                ensure_types_table()
                requested_type_name = normalized[2:].strip()
                type_id = None
                type_name = None
                if requested_type_name:
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
