import os
import psycopg2
from flask import Flask, request, abort, render_template, redirect, url_for, session
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from datetime import datetime

app = Flask(__name__)
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

# --- 管理画面系ルート ---
@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        if request.form.get("password") == ADMIN_PASSWORD:
            session["logged_in"] = True
            return redirect(url_for("admin_page"))
        error = "パスワードが違います"
    return render_template("login.html", error=error)

@app.route("/admin")
def admin_page():
    if not session.get("logged_in"):
        return redirect(url_for("login"))
    interval = request.args.get("interval", 5, type=int)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, user_id, message, status FROM reservations WHERE status IN ('waiting', 'called') ORDER BY id ASC")
            rows = cur.fetchall()
    return render_template("admin.html", rows=rows, interval=interval)

@app.route("/admin/call/<int:res_id>")
def admin_call(res_id):
    if not session.get("logged_in"): return redirect(url_for("login"))
    interval = request.args.get("interval", 5)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT user_id FROM reservations WHERE id = %s", (res_id,))
            user_id = cur.fetchone()[0]
            cur.execute("UPDATE reservations SET status = 'called' WHERE id = %s", (res_id,))
            conn.commit()
            line_bot_api.push_message(user_id, TextSendMessage(text=f"【順番が来ました】番号 {res_id} 番の方、会場へお越しください！"))
    return redirect(url_for("admin_page", interval=interval))

@app.route("/admin/finish/<int:res_id>")
def admin_finish(res_id):
    if not session.get("logged_in"): return redirect(url_for("login"))
    interval = request.args.get("interval", 5)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT user_id FROM reservations WHERE id = %s", (res_id,))
            user_id = cur.fetchone()[0]
            cur.execute("UPDATE reservations SET status = 'done' WHERE id = %s", (res_id,))
            conn.commit()
            line_bot_api.push_message(user_id, TextSendMessage(text=f"ご来場ありがとうございました。番号 {res_id} 番の受付を完了しました。"))
    return redirect(url_for("admin_page", interval=interval))

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
    
    # 予約処理を呼び出す
    process_reservation(event, user_id, user_message)

def process_reservation(event, user_id, user_message):
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                # 重複確認
                cur.execute("SELECT id, status FROM reservations WHERE user_id = %s AND status IN ('waiting', 'called') ORDER BY id DESC LIMIT 1", (user_id,))
                existing = cur.fetchone()
                
                if existing:
                    res_id, status = existing
                    if status == 'waiting':
                        cur.execute("SELECT COUNT(*) FROM reservations WHERE status = 'waiting' AND id < %s", (res_id,))
                        wait_count = cur.fetchone()[0]
                        reply_text = f"【予約済み】\n番号: {res_id}\nあなたの前に {wait_count} 人待機中です。"
                    else:
                        reply_text = f"【呼出中】\n番号: {res_id}\n会場へお越しください！"
                else:
                    # 新規予約
                    cur.execute("INSERT INTO reservations (user_id, message, status, created_at) VALUES (%s, %s, 'waiting', %s) RETURNING id", 
                                (user_id, user_message, 'waiting', datetime.now()))
                    new_id = cur.fetchone()[0]
                    conn.commit()
                    cur.execute("SELECT COUNT(*) FROM reservations WHERE status = 'waiting' AND id < %s", (new_id,))
                    wait_count = cur.fetchone()[0]
                    reply_text = f"【受付完了】\n番号: {new_id}\n現在 {wait_count} 人待ちです。"
                
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
    except Exception as e:
        print(f"Error in process_reservation: {e}")

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)