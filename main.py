import os
import psycopg2
from flask import Flask, request, abort, render_template_string, redirect, url_for, session
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from datetime import datetime

app = Flask(__name__)

# --- セキュリティ設定 ---
# セッションの暗号化キー（Renderの環境変数にランダムな文字列を設定することを推奨）
app.secret_key = os.getenv('SECRET_KEY', 'ukind-secret-2024')
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'ukind2024')

CHANNEL_ACCESS_TOKEN = os.getenv('CHANNEL_ACCESS_TOKEN')
CHANNEL_SECRET = os.getenv('CHANNEL_SECRET')
OWNER_LINE_ID = os.getenv('OWNER_LINE_ID', '').strip()

raw_db_url = os.getenv('DATABASE_URL')
DATABASE_URL = raw_db_url.replace("postgres://", "postgresql://", 1) if raw_db_url else None

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

def get_connection():
    return psycopg2.connect(DATABASE_URL)

# --- HTMLテンプレート（ログイン画面） ---
LOGIN_HTML = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ログイン - UKind</title>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css">
</head>
<body class="bg-light">
    <div class="container mt-5" style="max-width: 400px;">
        <div class="card shadow">
            <div class="card-body">
                <h3 class="text-center mb-4">UKind 管理者ログイン</h3>
                {% if error %}
                <div class="alert alert-danger">{{ error }}</div>
                {% endif %}
                <form method="POST">
                    <div class="mb-3">
                        <label class="form-label">パスワード</label>
                        <input type="password" name="password" class="form-control" required>
                    </div>
                    <button type="submit" class="btn btn-primary w-100">ログイン</button>
                </form>
            </div>
        </div>
    </div>
</body>
</html>
"""

# --- HTMLテンプレート（管理画面本体） ---
ADMIN_HTML = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>UKind 管理画面</title>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css">
</head>
<body class="bg-light">
    <nav class="navbar navbar-dark bg-dark mb-4">
        <div class="container">
            <span class="navbar-brand">UKind 管理パネル</span>
            <a href="/logout" class="btn btn-outline-light btn-sm">ログアウト</a>
        </div>
    </nav>
    <div class="container">
        <div class="card shadow-sm">
            <div class="card-body p-0">
                <table class="table table-striped mb-0">
                    <thead class="table-dark">
                        <tr>
                            <th>番号</th>
                            <th>メッセージ</th>
                            <th>状態</th>
                            <th>操作</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for row in rows %}
                        <tr>
                            <td>{{ row[0] }}</td>
                            <td>{{ row[2] or '-' }}</td>
                            <td>
                                {% if row[3] == 'waiting' %}
                                <span class="badge bg-warning text-dark">待機中</span>
                                {% elif row[3] == 'called' %}
                                <span class="badge bg-info">呼出中</span>
                                {% else %}
                                <span class="badge bg-success">到着済み</span>
                                {% endif %}
                            </td>
                            <td>
                                {% if row[3] == 'waiting' %}
                                <a href="/admin/call/{{ row[0] }}" class="btn btn-sm btn-success">呼出</a>
                                {% elif row[3] == 'called' %}
                                <span class="text-muted small">到着待ち</span>
                                {% else %}
                                <a href="/admin/finish/{{ row[0] }}" class="btn btn-sm btn-primary">確認完了</a>
                                {% endif %}
                            </td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>
        </div>
        <div class="text-center mt-4">
            <a href="/admin" class="btn btn-secondary">リストを更新</a>
        </div>
    </div>
</body>
</html>
"""

# --- ルーティング ---

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        if request.form.get("password") == ADMIN_PASSWORD:
            session["logged_in"] = True
            return redirect(url_for("admin_page"))
        else:
            error = "パスワードが正しくありません"
    return render_template_string(LOGIN_HTML, error=error)

@app.route("/logout")
def logout():
    session.pop("logged_in", None)
    return redirect(url_for("login"))

@app.route("/admin")
def admin_page():
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, user_id, message, status FROM reservations WHERE status IN ('waiting', 'called', 'arrived') ORDER BY id ASC")
            rows = cur.fetchall()
    return render_template_string(ADMIN_HTML, rows=rows)

@app.route("/admin/call/<int:res_id>")
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

@app.route("/admin/finish/<int:res_id>")
def admin_finish(res_id):
    if not session.get("logged_in"): return redirect(url_for("login"))

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE reservations SET status = 'done' WHERE id = %s", (res_id,))
            conn.commit()
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
            if normalized == '予約':
                cur.execute(
                    "SELECT id, status FROM reservations WHERE user_id = %s AND status IN ('waiting', 'called', 'arrived') ORDER BY id DESC LIMIT 1",
                    (user_id,)
                )
                existing = cur.fetchone()
                if existing:
                    res_id, status = existing
                    if status == 'waiting':
                        cur.execute("SELECT COUNT(*) FROM reservations WHERE status = 'waiting' AND id < %s", (res_id,))
                        reply = f"予約済みです。番号: {res_id} / 待ち: {cur.fetchone()[0]}人"
                    elif status == 'called':
                        reply = f"【呼出中】番号: {res_id} 会場へお越しください！"
                    else:
                        reply = f"到着受付済みです。番号: {res_id} / スタッフが確認します。"
                else:
                    cur.execute("INSERT INTO reservations (user_id, message) VALUES (%s, %s) RETURNING id", (user_id, user_message))
                    new_id = cur.fetchone()[0]
                    conn.commit()
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
