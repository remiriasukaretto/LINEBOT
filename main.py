from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage,
    StickerMessage, StickerSendMessage
)
import os

app = Flask(__name__)

# --- 環境変数の読み込み ---
# RenderのEnvironment Variablesで設定した名前に合わせています
CHANNEL_ACCESS_TOKEN = os.getenv('CHANNEL_ACCESS_TOKEN')
CHANNEL_SECRET = os.getenv('CHANNEL_SECRET')

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# LINEからのWebhookを受け取る窓口
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return 'OK'

# テキストメッセージが届いた時の処理 (オウム返し)
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    # もし中身が空なら何もしない（エラー防止）
    if not event.message.text:
        return

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=event.message.text)
    )

# スタンプメッセージが届いた時の処理 (お辞儀ムーンを返す)
@handler.add(MessageEvent, message=StickerMessage)
def handle_sticker(event):
    line_bot_api.reply_message(
        event.reply_token,
        StickerSendMessage(
            package_id='446',
            sticker_id='1988'
        )
    )

if __name__ == "__main__":
    # Renderのポート番号設定に対応
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)