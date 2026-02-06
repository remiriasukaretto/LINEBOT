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

# URLの整形（postgres:// を postgresql:// に変換）
raw_db_url = os.getenv('DATABASE_URL')
if raw_db_url and raw_db_url.startswith("postgres://"):
    DATABASE_URL = raw_db_url.replace("postgres://", "postgresql://", 1)
else:
    DATABASE_URL = raw_db_url

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# --- データベース初期化 ---
def init_db():
    if not DATABASE_URL:
        return
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        # reservationsテーブルを作成
        # status: 'waiting' (待機中), 'called' (呼び出し中), 'done' (完了)
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
        print("Database initialized.")
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
    user_message = event.message.text
    user_id = event.source.user_id
    
    if not DATABASE_URL:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="システムエラー：DB設定が見つかりません"))
        return

    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()

        # 1. すでに並んでいるか確認（ステータスが waiting または called のもの）
        cur.execute(
            "SELECT id, status FROM reservations WHERE user_id = %s AND status IN ('waiting', 'called') ORDER BY created_at DESC LIMIT 1",
            (user_id,)
        )
        existing_reservation = cur.fetchone()

        if existing_reservation:
            # すでに並んでいる場合
            res_id, status = existing_reservation
            if status == 'waiting':
                # 自分の前に何人待っているか計算
                cur.execute("SELECT COUNT(*) FROM reservations WHERE status = 'waiting' AND id < %s", (res_id,))
                wait_count = cur.fetchone()[0]
                reply_text = f"【予約済み】\n受付番号: {res_id}\nあなたの前に {wait_count} 人待っています。順番が来たらお知らせしますので、そのままお待ちください。"
            else:
                reply_text = f"【呼び出し中】\n受付番号: {res_id}\nまもなく順番です！催事場へお越しください。"
        
        else:
            # 新しく予約する場合
            cur.execute(
                "INSERT INTO reservations (user_id, message, status, created_at) VALUES (%s, %s, 'waiting', %s) RETURNING id",
                (user_id, user_message, datetime.now())
            )
            new_id = cur.fetchone()[0]
            conn.commit()

            # 待ち人数の計算
            cur.execute("SELECT COUNT(*) FROM reservations WHERE status = 'waiting' AND id < %s", (new_id,))
            wait_count = cur.fetchone()[0]

            reply_text = f"【予約完了】\n受付番号: {new_id}\n予約を承りました。\n\n現在の待ち人数: {wait_count}人\n順番が来たらこのLINEでお知らせします。"

        cur.close()
        conn.close()

    except Exception as e:
        print(f"Error: {e}")
        reply_text = "申し訳ありません、予約処理中にエラーが発生しました。時間を置いて再度お試しください。"

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply_text)
    )

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)