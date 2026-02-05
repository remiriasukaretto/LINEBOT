from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import os

app = Flask(__name__)

# --- 環境変数の読み込み ---
# RenderのEnvironment Variablesで設定した名前（Key）と一致させる
CHANNEL_ACCESS_TOKEN = os.getenv('CHANNEL_ACCESS_TOKEN')
CHANNEL_SECRET = os.getenv('CHANNEL_SECRET')

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return 'OK'

# 【既存】テキストメッセージへの反応
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=event.message.text)
    )

# 【追加】スタンプメッセージへの反応
@handler.add(MessageEvent, message=StickerMessage)
def handle_sticker(event):
    # スタンプが送られてきたら、特定のスタンプで返す
    # package_id と sticker_id を変えることで好きなスタンプを送れます
    line_bot_api.reply_message(
        event.reply_token,
        StickerSendMessage(
            package_id='446',
            sticker_id='1988'
        )
    )

if __name__ == "__main__":
    # RenderはPORT環境変数を指定してくるのでそれを使う
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

    #seikou!!!!!