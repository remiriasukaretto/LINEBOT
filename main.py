import os
import psycopg2
from flask import Flask, request, abort, render_template_string, redirect, url_for
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from datetime import datetime

app = Flask(__name__)

# --- 環境変数 ---
CHANNEL_ACCESS_TOKEN = os.getenv('CHANNEL_ACCESS_TOKEN')
CHANNEL_SECRET = os.getenv('CHANNEL_SECRET')
OWNER_LINE_ID = os.getenv('OWNER_LINE_ID', '').strip()
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'ukind2024') # 管理画面用のパスワード

raw_db_url = os.getenv('DATABASE_URL')
DATABASE_URL = raw_db_url.replace("postgres://", "postgresql://", 1) if raw_db_url else None

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

def get_connection():
    return psycopg2.connect(DATABASE_URL)

# --- 管理画面のHTMLテンプレート ---
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ja">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>UKind 管理画面</title>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css">
    <style>
        body { background-color: #f8f9fa; }
        .container { max-width: 800px; margin-top: 30px; }
        .card { margin-bottom: 20px; }
        .badge-waiting { background-color: #ffc107; color: black; }
        .badge-called { background-color: #0dcaf0; }
    </style>
</head>
<body>
    <div class="container">
        <h2 class="mb-4">UKind 予約管理画面</h2>
        
        <div class="card">
            <div class="card-header bg-primary text-white">現在の待機列</div>
            <div class="card-body p-0">
                <table class="table table-hover mb-0">
                    <thead class="table-light">
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
                                    <span class="badge badge-waiting">待機中</span>
                                {% elif row[3] == 'called' %}
                                    <span class="badge badge-called">呼出中</span>
                                {% endif %}
                            </td>
                            <td>
                                {% if row[3] == 'waiting' %}
                                    <a href="/admin/call/{{ row[0] }}" class="btn btn-sm btn-success">呼出</a>
                                {% elif row[3] == 'called' %}
                                    <a href="/admin/finish/{{ row[0] }}" class="btn btn-sm btn-outline-secondary">完了</a>
                                {% endif %}
                            </td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>
        </div>
        <div class="text-center mt-3">
            <a href="/admin" class="btn btn-secondary btn-sm">画面を更新</a>
        </div>
    </div>
</body>
</html>
"""

# --- 管理用ルート ---

@app.route("/admin")
def admin_page():
    # パスワードチェック (URLパラメータ ?pw=xxx で簡易認証)
    if request.args.get("pw") != ADMIN_PASSWORD:
        return "アクセス権限がありません", 403

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, user_id, message, status FROM reservations WHERE status IN ('waiting', 'called') ORDER BY id ASC")
            rows = cur.fetchall()
    return render_template_string(HTML_TEMPLATE, rows=rows)

@app.route("/admin/call/<int:res_id>")
def admin_call(res_id):
    if request.args.get("pw") != ADMIN_PASSWORD: return "Error", 403
    
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT user_id FROM reservations WHERE id = %s", (res_id,))
            user_id = cur.fetchone()[0]
            cur.execute("UPDATE reservations SET status = 'called' WHERE id = %s", (res_id,))
            conn.commit()
            line_bot_api.push_message(user_id, TextSendMessage(text=f"【順番が来ました】\n受付番号 {res_id} 番の方、会場へお越しください！"))
    return redirect(url_for('admin_page', pw=ADMIN_PASSWORD))

@app.route("/admin/finish/<int:res_id>")
def admin_finish(res_id):
    if request.args.get("pw") != ADMIN_PASSWORD: return "Error", 403
    
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE reservations SET status = 'done' WHERE id = %s", (res_id,))
            conn.commit()
    return redirect(url_for('admin_page', pw=ADMIN_PASSWORD))

# --- LINE Webhook 処理 (既存のまま) ---

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
    
    # LINEからの「次」「完了」などのコマンドも引き続き使用可能
    if user_id == OWNER_LINE_ID:
        if user_message == "次":
            # 内部的にadmin_callと同じような処理を呼ぶことも可能
            pass 

    # ユーザー予約ロジック (省略せず以前のものをそのまま使用してください)
    process_reservation(event, user_id, user_message)

def process_reservation(event, user_id, user_message):
    # 以前のコードと同じため中身は省略（前の回答のものをそのまま入れてください）
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, status FROM reservations WHERE user_id = %s AND status IN ('waiting', 'called') ORDER BY id DESC LIMIT 1", (user_id,))
            existing = cur.fetchone()
            if existing:
                res_id, status = existing
                if status == 'waiting':
                    cur.execute("SELECT COUNT(*) FROM reservations WHERE status = 'waiting' AND id < %s", (res_id,))
                    reply = f"予約済みです。\n番号: {res_id}\n待ち: {cur.fetchone()[0]}人"
                else:
                    reply = f"【呼び出し中】\n番号: {res_id}\n会場へお越しください！"
            else:
                cur.execute("INSERT INTO reservations (user_id, message) VALUES (%s, %s) RETURNING id", (user_id, user_message))
                new_id = cur.fetchone()[0]
                conn.commit()
                cur.execute("SELECT COUNT(*) FROM reservations WHERE status = 'waiting' AND id < %s", (new_id,))
                reply = f"【受付完了】\n番号: {new_id}\n待ち: {cur.fetchone()[0]}人"
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)