import os
import psycopg2
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from datetime import datetime

app = Flask(__name__)

CHANNEL_ACCESS_TOKEN = os.getenv('CHANNEL_ACCESS_TOKEN')
CHANNEL_SECRET = os.getenv('CHANNEL_SECRET')
OWNER_LINE_ID = os.getenv('OWNER_LINE_ID', '').strip()

raw_db_url = os.getenv('DATABASE_URL')
DATABASE_URL = raw_db_url.replace("postgres://", "postgresql://", 1) if raw_db_url else None

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# --- データベース接続ヘルパー ---
def get_connection():
    return psycopg2.connect(DATABASE_URL)

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
    
    # --- オーナー用コマンド ---
    if user_id == OWNER_LINE_ID:
        if user_message == "次":
            call_next_user(event)
            return
        elif user_message == "状況":
            show_status(event)
            return
        elif user_message.startswith("完了"):
            # 「完了 12」のように送るとその番号を終了にする
            parts = user_message.split()
            if len(parts) > 1 and parts[1].isdigit():
                finish_reservation(event, int(parts[1]))
                return

    # --- ユーザー用コマンド ---
    if user_message == "キャンセル":
        cancel_reservation(event, user_id)
    else:
        process_reservation(event, user_id, user_message)

# --- 機能関数 ---

def call_next_user(event):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, user_id FROM reservations WHERE status = 'waiting' ORDER BY id ASC LIMIT 1")
            target = cur.fetchone()
            if target:
                res_id, target_user_id = target
                cur.execute("UPDATE reservations SET status = 'called' WHERE id = %s", (res_id,))
                conn.commit()
                line_bot_api.push_message(target_user_id, TextSendMessage(text=f"【順番が来ました】\n番号 {res_id} 番の方、会場へお越しください！"))
                reply = f"番号 {res_id} を呼び出しました。"
            else:
                reply = "待機者はいません。"
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

def finish_reservation(event, res_id):
    """オーナーが対応を完了した時に実行"""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE reservations SET status = 'done' WHERE id = %s", (res_id,))
            conn.commit()
            reply = f"番号 {res_id} の対応を完了として記録しました。"
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

def cancel_reservation(event, user_id):
    """ユーザー自身が予約を取り消す"""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE reservations SET status = 'cancelled' WHERE user_id = %s AND status IN ('waiting', 'called')", (user_id,))
            conn.commit()
            reply = "予約をキャンセルしました。またのご利用をお待ちしています。"
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

def show_status(event):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM reservations WHERE status = 'waiting'")
            w_count = cur.fetchone()[0]
            cur.execute("SELECT id FROM reservations WHERE status = 'called' ORDER BY id ASC")
            called_ids = [str(r[0]) for r in cur.fetchall()]
            reply = f"【現在状況】\n待ち人数: {w_count}人\n呼び出し中: {', '.join(called_ids) if called_ids else 'なし'}"
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

def process_reservation(event, user_id, user_message):
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
                reply = f"【受付完了】\n番号: {new_id}\n待ち: {cur.fetchone()[0]}人\nキャンセルは「キャンセル」と送ってください。"
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)