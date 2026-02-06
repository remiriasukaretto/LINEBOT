import os
import psycopg2
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from datetime import datetime

app = Flask(__name__)

# --- 環境変数の読み込み ---
CHANNEL_ACCESS_TOKEN = os.getenv('CHANNEL_ACCESS_TOKEN')
CHANNEL_SECRET = os.getenv('CHANNEL_SECRET')

# .strip() を追加して、前後に入ってしまった不要なスペースを除去します
OWNER_LINE_ID = os.getenv('OWNER_LINE_ID', '').strip()

# DB URLの整形
raw_db_url = os.getenv('DATABASE_URL')
if raw_db_url and raw_db_url.startswith("postgres://"):
    DATABASE_URL = raw_db_url.replace("postgres://", "postgresql://", 1)
else:
    DATABASE_URL = raw_db_url

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

def init_db():
    if not DATABASE_URL: return
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS reservations (
                id SERIAL PRIMARY KEY,
                user_id VARCHAR(100) NOT NULL,
                message TEXT,
                status VARCHAR(20) DEFAULT 'waiting',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Init DB Error: {e}")

init_db()

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
    user_message = event.message.text.strip() # 前後の空白を消す
    user_id = event.source.user_id
    
    # 【デバッグ用ログ】RenderのLogsタブで確認してください
    print(f"DEBUG: 受信したID: {user_id}")
    print(f"DEBUG: 設定されたOWNER_ID: {OWNER_LINE_ID}")
    print(f"DEBUG: 一致するか: {user_id == OWNER_LINE_ID}")

    if not DATABASE_URL:
        return

    # --- オーナー用コマンドの処理 ---
    # もしIDが一致していればこちらに入るはず
    if user_id == OWNER_LINE_ID:
        if user_message == "次":
            call_next_user(event)
            return
        elif user_message == "状況":
            show_status(event)
            return
        elif user_message == "確認":
            # 自分がオーナーとして認識されているか確認する専用コマンド
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="あなたはオーナーとして正しく認識されています。"))
            return

    # --- 一般ユーザー用：予約処理 ---
    process_reservation(event, user_id, user_message)

def call_next_user(event):
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("SELECT id, user_id FROM reservations WHERE status = 'waiting' ORDER BY id ASC LIMIT 1")
        target = cur.fetchone()

        if target:
            res_id, target_user_id = target
            cur.execute("UPDATE reservations SET status = 'called' WHERE id = %s", (res_id,))
            conn.commit()
            try:
                line_bot_api.push_message(target_user_id, TextSendMessage(text=f"【順番が来ました！】\n受付番号 {res_id} 番の方、催事場へお越しください！"))
                reply_text = f"番号 {res_id} 番の方を呼び出しました。"
            except:
                reply_text = f"番号 {res_id} の呼び出しに失敗（ブロック等）"
        else:
            reply_text = "待機者はいません。"
        cur.close()
        conn.close()
    except Exception as e:
        reply_text = f"エラー: {e}"
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))

def show_status(event):
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM reservations WHERE status = 'waiting'")
        wait_count = cur.fetchone()[0]
        cur.close()
        conn.close()
        reply_text = f"【現在の状況】\n待機人数: {wait_count}人"
    except:
        reply_text = "取得失敗"
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))

def process_reservation(event, user_id, user_message):
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("SELECT id, status FROM reservations WHERE user_id = %s AND status IN ('waiting', 'called') ORDER BY created_at DESC LIMIT 1", (user_id,))
        existing = cur.fetchone()
        if existing:
            res_id, status = existing
            if status == 'waiting':
                cur.execute("SELECT COUNT(*) FROM reservations WHERE status = 'waiting' AND id < %s", (res_id,))
                wait_count = cur.fetchone()[0]
                reply_text = f"【予約済み】番号:{res_id} / 前に{wait_count}人"
            else:
                reply_text = f"【呼び出し中】番号:{res_id} 会場へお越しください！"
        else:
            cur.execute("INSERT INTO reservations (user_id, message, status) VALUES (%s, %s, 'waiting') RETURNING id", (user_id, user_message))
            new_id = cur.fetchone()[0]
            conn.commit()
            cur.execute("SELECT COUNT(*) FROM reservations WHERE status = 'waiting' AND id < %s", (new_id,))
            wait_count = cur.fetchone()[0]
            reply_text = f"【受付完了】番号: {new_id} / 待ち: {wait_count}人"
        cur.close()
        conn.close()
    except:
        reply_text = "予約エラー"
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)