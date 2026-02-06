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

# Renderは 'postgres://' を返すが、SQLAlchemyやpsycopg2でエラーが出る場合があるため置換
raw_db_url = os.getenv('DATABASE_URL')
if raw_db_url and raw_db_url.startswith("postgres://"):
    DATABASE_URL = raw_db_url.replace("postgres://", "postgresql://", 1)
else:
    DATABASE_URL = raw_db_url

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

def init_db():
    """起動時にテーブルを作成する"""
    if not DATABASE_URL:
        return "DATABASE_URLが設定されていません"
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id SERIAL PRIMARY KEY,
                user_id VARCHAR(100) NOT NULL,
                message TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.commit()
        cur.close()
        conn.close()
        return "DB初期化成功"
    except Exception as e:
        return f"DB初期化エラー: {str(e)}"

# 起動時に一度実行
print(init_db())

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
    
    status_msg = ""
    
    # --- データベース保存の試行 ---
    if not DATABASE_URL:
        status_msg = "【設定ミス】DATABASE_URLが環境変数にありません"
    else:
        try:
            conn = psycopg2.connect(DATABASE_URL)
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO messages (user_id, message, created_at) VALUES (%s, %s, %s)",
                (user_id, user_message, datetime.now())
            )
            conn.commit()
            cur.close()
            conn.close()
            status_msg = "【成功】DBに保存しました"
        except Exception as e:
            status_msg = f"【保存失敗】エラー内容: {str(e)}"

    # LINEへの返信（ここで原因を教えてくれます）
    reply_text = f"{status_msg}\n送信内容: {user_message}"

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply_text)
    )

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)