const { Client, middleware } = require('@line/bot-sdk');
const { Pool } = require('pg');

// 接続設定（Renderの環境変数を使用）
const pool = new Pool({ connectionString: process.env.DATABASE_URL, ssl: { rejectUnauthorized: false } });
const config = { channelAccessToken: process.env.CHANNEL_ACCESS_TOKEN, channelSecret: process.env.CHANNEL_SECRET };
const client = new Client(config);


// --- 課題4: オーナーによる呼び出しAPI ---
// 決済・調理前に「呼び出し」だけを行う [1]
async function callUser(userId) {
    const msg = "順番が来ました！受付で決済完了後に調理を開始します。"; // ソースの運用方針 [1]
    await client.pushMessage(userId, { type: 'text', text: msg });
    await pool.query(
        'INSERT INTO message_logs (user_id, message_text, log_type) VALUES ($1, $2, $3)',
        [userId, msg, 'called']
    );
}
async function handleEvent(event) {
    if (event.type !== 'message' || event.message.type !== 'text') return null;

    const userId = event.source.userId; // ユーザーIDを取得 [2]
    const text = event.message.text;

    try {
        // 解決策：ON CONFLICT (user_id) DO NOTHING を使い、重複エラーを防ぐ [2]
        await pool.query(
            'INSERT INTO users (user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING',
            [userId]
        );

        // 同意済みかチェック [1]
        const user = await pool.query('SELECT is_consented FROM users WHERE user_id = $1', [userId]);
        
        if (!user.rows.is_consented) {
            if (text === '同意する') {
                await pool.query('UPDATE users SET is_consented = TRUE WHERE user_id = $1', [userId]);
                return client.replyMessage(event.replyToken, { type: 'text', text: '同意を確認しました。予約可能です。' });
            }
            // 同意を求めるメッセージ（個人情報を取らないことを明示） [1]
            return client.replyMessage(event.replyToken, { type: 'text', text: '利用には「同意する」と送信してください。ID以外の個人情報は取得しません。' });
        }

        // 同意済みならメッセージをログに保存
        await pool.query('INSERT INTO message_logs (user_id, message_text) VALUES ($1, $2)', [userId, text]);
        console.log("保存成功");

    } catch (err) {
        console.error("保存失敗:", err);
    }
}