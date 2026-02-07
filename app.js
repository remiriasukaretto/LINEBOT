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
        const isConsented = userRes.rows[0]?.is_consented ?? false;

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

        let replyText = null;

        if (text === '予約') {
            const existingRes = await pool.query(
                "SELECT id, status FROM reservations WHERE user_id = $1 AND status IN ('waiting', 'called') ORDER BY id DESC LIMIT 1",
                [userId]
            );
            if (existingRes.rows.length > 0) {
                const { id, status } = existingRes.rows[0];
                if (status === 'waiting') {
                    const waitCountRes = await pool.query(
                        "SELECT COUNT(*) FROM reservations WHERE status = 'waiting' AND id < $1",
                        [id]
                    );
                    replyText = `予約済みです。番号: ${id} / 待ち: ${waitCountRes.rows[0].count}人`;
                } else {
                    replyText = `【呼出中】番号: ${id} 会場へお越しください！`;
                }
            } else {
                const insertRes = await pool.query(
                    'INSERT INTO reservations (user_id, message) VALUES ($1, $2) RETURNING id',
                    [userId, text]
                );
                const newId = insertRes.rows[0].id;
                const waitCountRes = await pool.query(
                    "SELECT COUNT(*) FROM reservations WHERE status = 'waiting' AND id < $1",
                    [newId]
                );
                replyText = `【受付完了】番号: ${newId} / 待ち: ${waitCountRes.rows[0].count}人`;
            }
        } else if (text === 'キャンセル') {
            const cancelRes = await pool.query(
                "UPDATE reservations SET status = 'cancelled' WHERE user_id = $1 AND status IN ('waiting', 'called') RETURNING id",
                [userId]
            );
            if (cancelRes.rows.length === 0) {
                replyText = 'キャンセル対象の予約はありません。';
            } else {
                replyText = `予約番号 ${cancelRes.rows[0].id} をキャンセルしました。`;
            }
        } else {
            replyText = 'メッセージを受け付けました。予約は「予約」、キャンセルは「キャンセル」と送信してください。';
        }

        // 同意済みの場合、メッセージを保存
        await pool.query('INSERT INTO message_logs (user_id, message_text) VALUES ($1, $2)', [userId, text]);
        console.log(`保存成功: ${text} (from: ${userId})`);

        if (replyText) {
            return client.replyMessage(event.replyToken, {
                type: 'text',
                text: replyText
            });
        }

    } catch (err) {
        console.error('エラー発生:', err);
        // 障害発生時は手動運用に切り替える旨を考慮 [2]
    }
}

app.listen(process.env.PORT || 3000);
