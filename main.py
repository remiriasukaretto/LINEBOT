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

# DATABASE_URLの取得と修正
# Renderは 'postgres://' を返すが、ライブラリによっては 'postgresql://' が必要なため置換する
raw_db_url = os.getenv('DATABASE_URL')
DATABASE_URL = raw_db_url.replace("postgres://", "postgresql://") if raw_db_url else None

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# --- データベース初期化関数 ---
def init_db():
    if not DATABASE_URL:
        print("【警告】DATABASE_URL環境変数が設定されていません！DB機能は無効です。")
        return

    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id SERIAL PRIMARY KEY,
                user_id VARCHAR(50) NOT NULL,
                message TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.commit()
        cur.close()
        conn.close()
        print("Database initialized successfully.")
    except Exception as e:
        print(f"【初期化エラー】DB接続に失敗しました: {e}")

# アプリ起動時にDB初期化
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
    
    # --- データベースへの保存処理 ---
    save_status = "保存失敗" # デフォルト
    
    if DATABASE_URL:
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
            print(f"Message saved: {user_message} from {user_id}")
            save_status = "保存完了"
        except Exception as e:
            print(f"【保存エラー】: {e}")
            save_status = "DBエラー"
    else:
        print("DATABASE_URLが設定されていないため保存できませんでした。")
        save_status = "設定未完了"

    # --- ユーザーへの返信 ---
    # デバッグ用にステータスを返信に含めています。本番では消してください。
    reply_text = f"受け付けました（{save_status}）: {user_message}"

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply_text)
    )

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)