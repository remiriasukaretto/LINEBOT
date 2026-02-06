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

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        if request.form.get("password") == ADMIN_PASSWORD:
            session["logged_in"] = True
            return redirect(url_for("admin_page"))
        error = "パスワードが違います"
    return render_template("login.html", error=error)

@app.route("/logout")
def logout():
    session.pop("logged_in", None)
    return redirect(url_for("login"))

@app.route("/admin")
def admin_page():
    if not session.get("logged_in"):
        return redirect(url_for("login"))
    
    # URLパラメータから更新秒数を取得（デフォルト5秒）
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
            # 完了時にユーザーに通知を送る処理を追加
            cur.execute("SELECT user_id FROM reservations WHERE id = %s", (res_id,))
            user_id = cur.fetchone()[0]
            cur.execute("UPDATE reservations SET status = 'done' WHERE id = %s", (res_id,))
            conn.commit()
            # 「確認できた（受付完了）」という旨を送信
            line_bot_api.push_message(user_id, TextSendMessage(text=f"ご来場ありがとうございました。番号 {res_id} 番の受付を完了しました。"))
    return redirect(url_for("admin_page", interval=interval))

# (以下 LINE Webhook/process_reservation 等は以前と同じ)