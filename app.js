const express = require('express');
const { Client, middleware } = require('@line/bot-sdk');
const { Pool } = require('pg');

const app = express();
const pool = new Pool({ connectionString: process.env.DATABASE_URL, ssl: { rejectUnauthorized: false } });
const config = {
    channelAccessToken: process.env.CHANNEL_ACCESS_TOKEN,
    channelSecret: process.env.CHANNEL_SECRET
};
const client = new Client(config);

app.post('/webhook', middleware(config), (req, res) => {
    Promise.all(req.body.events.map(handleEvent)).then(() => res.json({}));
});

async function handleEvent(event) {
    if (event.type !== 'message' || event.message.type !== 'text') return null;

    const userId = event.source.userId; // ユーザーIDのみで識別 [1]
    const text = event.message.text;

    try {
        // ユーザーをDBに登録（未登録の場合のみ）
        await pool.query('INSERT INTO users (user_id) VALUES ($1) ON CONFLICT DO NOTHING', [userId]);

        // 現在の同意状態を確認
        const userRes = await pool.query('SELECT is_consented FROM users WHERE user_id = $1', [userId]);
        const isConsented = userRes.rows.is_consented;

        if (!isConsented) {
            // 「同意します」と返信が来た場合のみ同意とみなす
            if (text === '同意します') {
                await pool.query('UPDATE users SET is_consented = TRUE WHERE user_id = $1', [userId]);
                return client.replyMessage(event.replyToken, {
                    type: 'text',
                    text: '同意ありがとうございます。予約システムが利用可能になりました。'
                });
            } else {
                // 同意を促すメッセージ。個人情報を取らないことを明示 [2]
                return client.replyMessage(event.replyToken, {
                    type: 'text',
                    text: '【UKind】当システムではユーザーID以外の個人情報は収集しません。利用に同意いただける場合は「同意します」と返信してください。'
                });
            }
        }

        // 同意済みの場合、メッセージを保存
        await pool.query('INSERT INTO message_logs (user_id, message_text) VALUES ($1, $2)', [userId, text]);
        console.log(`保存成功: ${text} (from: ${userId})`);

    } catch (err) {
        console.error('エラー発生:', err);
        // 障害発生時は手動運用に切り替える旨を考慮 [2]
    }
}

app.listen(process.env.PORT || 3000);