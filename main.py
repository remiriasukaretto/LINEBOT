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
DATABASE_URL = os.getenv('DATABASE_URL')  # RenderのInternal Database URL

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# --- データベース初期化関数 ---
def init_db():
    """テーブルが存在しない場合に作成する関数"""
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        # messagesテーブルを作成（id, user_id, message, created_at）
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
        print("Database initialized.")
    except Exception as e:
        print(f"Error initializing database: {e}")

# アプリ起動時にDB初期化を実行
if DATABASE_URL:
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
    # メッセージ内容とユーザーIDを取得
    user_message = event.message.text
    user_id = event.source.user_id
    
    # --- データベースへの保存処理 ---
    try:
        if DATABASE_URL:
            # DB接続
            conn = psycopg2.connect(DATABASE_URL)
            cur = conn.cursor()
            
            # データの挿入 (SQLインジェクション対策のためプレースホルダ %s を使用)
            cur.execute(
                "INSERT INTO messages (user_id, message, created_at) VALUES (%s, %s, %s)",
                (user_id, user_message, datetime.now())
            )
            
            conn.commit() # 保存を確定
            cur.close()
            conn.close()
            print(f"Message saved: {user_message} from {user_id}")
    except Exception as e:
        print(f"Database error: {e}")
        # エラーが起きてもユーザーには返信できるように処理を続ける（必要に応じてエラーメッセージを変える）

    # --- ユーザーへの返信 ---
    # ここでは「保存しました」＋オウム返し に変更していますが、
    # 必要に応じて元のオウム返しのみに戻してください。
    reply_text = f"受け付けました: {user_message}"

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply_text)
    )

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)