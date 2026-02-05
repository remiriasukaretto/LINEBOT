const express = require('express');
const { Client } = require('@line/bot-sdk');
const { Pool } = require('pg');

const app = express();
const pool = new Pool({ connectionString: process.env.DATABASE_URL, ssl: { rejectUnauthorized: false } });
const client = new Client({
    channelAccessToken: process.env.CHANNEL_ACCESS_TOKEN,
    channelSecret: process.env.CHANNEL_SECRET
});

// オーナーがユーザーを呼び出すAPI
app.post('/api/call', express.json(), async (req, res) => {
    const { userId } = req.body;
    const msg = "順番が来ました！催事場へお越しください。決済後に調理を開始します。"; // 作り置きはしない運用 [1]

    try {
        // 1. LINE送信
        await client.pushMessage(userId, { type: 'text', text: msg });
        // 2. DB保存（送信時刻を記録） [1]
        await pool.query('INSERT INTO message_logs (user_id, message_text) VALUES ($1, $2)', [userId, msg]);
        res.json({ success: true });
    } catch (err) {
        res.status(500).send("障害時は手動運用に切り替えてください"); // リカバリ規定 [2]
    }
});

// [2] 呼び出しから15分以上経過したユーザーを自動除外する処理（定期実行を想定）
async function autoRemove() {
    await pool.query("DELETE FROM message_logs WHERE sent_at < NOW() - INTERVAL '15 minutes'");
}

app.listen(process.env.PORT || 3000);