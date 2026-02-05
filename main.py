from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import os

app = Flask(__name__)

# 1. LINE Developersで取得したアクセストークンとシークレットを設定
# 本番運用では環境変数（os.getenv）から読み込むのが安全です
YOUR_CHANNEL_ACCESS_TOKEN = 'ここにチャネルアクセストークンを貼り付け'
YOUR_CHANNEL_SECRET = 'ここにチャネルシークレットを貼り付け'

line_bot_api = LineBotApi(YOUR_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(YOUR_CHANNEL_SECRET)

# LINE Developersの「Webhook URL」に https://〜/callback を設定します
@app.route("/callback", methods=['POST'])
def callback():
    # 署名検証（LINEからのリクエストであることを確認）
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return 'OK'

# テキストメッセージを受け取った時の挙動
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    # event.message.text がユーザーから送られた文字列
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=event.message.text) # そのまま返す
    )
if __name__ == "__main__":
    # Renderの環境変数PORTに対応させる
    import os
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)