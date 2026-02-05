const { Client, middleware } = require('@line/bot-sdk');
const { Pool } = require('pg');

// 接続設定（Renderの環境変数を使用）
const pool = new Pool({ connectionString: process.env.DATABASE_URL, ssl: { rejectUnauthorized: false } });
const config = { channelAccessToken: process.env.CHANNEL_ACCESS_TOKEN, channelSecret: process.env.CHANNEL_SECRET };
const client = new Client(config);

async function handleEvent(event) {
    if (event.type !== 'message' || event.message.type !== 'text') return null;

    const userId = event.source.userId; // 課題2: WebhookからID取得 [1]
    const text = event.message.text;

    // --- 課題1 & 2: 同意プロセスとID保存 ---
    // ユーザーIDを登録し、まだなら同意を求める処理
    await pool.query(
        'INSERT INTO users (user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING',
        [userId]
    );

    const userRes = await pool.query('SELECT is_consented FROM users WHERE user_id = $1', [userId]);
    
    if (!userRes.rows.is_consented) {
        if (text === '同意する') {
            await pool.query('UPDATE users SET is_consented = TRUE WHERE user_id = $1', [userId]);
            return client.replyMessage(event.replyToken, { type: 'text', text: '同意ありがとうございます。予約を受け付けました。' });
        } else {
            // 個人情報を収集しないことを明示して同意を得る [2]
            return client.replyMessage(event.replyToken, { 
                type: 'text', 
                text: '当システムではユーザーID以外の個人情報は取得しません。利用には「同意する」と送信してください。' 
            });
        }
    }

    // --- テスト: 受信メッセージをDBに保存 ---
    await pool.query(
        'INSERT INTO message_logs (user_id, message_text, log_type) VALUES ($1, $2, $3)',
        [userId, text, 'received']
    );
}

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