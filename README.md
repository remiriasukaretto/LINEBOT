# LINEBOT 
　
## セットアップ
1. `.env.example` の値をデプロイ先の環境変数に設定してください。
2. Werkzeug の `generate_password_hash` で `ADMIN_PASSWORD_HASH` を生成してください。
3. 監査専用の管理者を分けたい場合は `AUDIT_ADMIN_PASSWORD_HASH` も設定してください。
   全アカウントの予約一括削除を使う場合だけ、`ENABLE_GLOBAL_RESERVATION_DELETE=true` を設定して再起動してください。
4. `gunicorn main:app` でアプリを起動します（`Procfile` 参照）。

## Render へのデプロイ
1. [Render.com](https://render.com) で新しい Web Service を作成します。
2. Render のダッシュボードで以下の環境変数を設定します。
   - `ALLOWED_HOSTS`: Render のアプリドメイン（例: `myapp.onrender.com`）。複数ドメインはカンマまたは空白区切りで指定できます。
   - その他の必須変数: `SECRET_KEY`, `ADMIN_PASSWORD_HASH`, `AUDIT_ADMIN_PASSWORD_HASH`, `CHANNEL_ACCESS_TOKEN`, `CHANNEL_SECRET`, `DATABASE_URL`, `OWNER_LINE_ID`
   - 任意の `REDIS_URL` を設定すると、Webhookのレート制限をPostgreSQLではなくRedisで処理します。未設定時は従来のPostgreSQL方式です。
   - `WEBHOOK_ASYNC_WORKERS` でWebhookのバックグラウンド処理ワーカー数を調整できます（デフォルト4）。
   - Webhookの受信確認は `metric=webhook_received`、処理時間は `metric=webhook_request`（受付・署名検証）と `metric=webhook_background`（予約処理）としてログ出力されます。
3. デプロイします。Render は `Procfile` を自動検出して Gunicorn で起動します。
   - 本番では `ALLOWED_HOSTS` の設定が必須です。未設定だとアプリは起動に失敗します。

## バッチ呼び出しキュー
1. アプリ環境変数に `BATCH_CALL_RUNNER_TOKEN` を設定します。
2. GitHub リポジトリの secrets に以下を追加します。
   - `BATCH_CALL_RUNNER_TOKEN`: アプリ環境変数と同じ値
   - `CALL_QUEUE_TASK_URL`: `https://your-app.example.com/tasks/process-call-queue`
3. ワークフロー `.github/workflows/process-call-queue.yml` は 1 分ごとに実行され、GitHub Actions から手動実行も可能です。
4. 毎日 0:00 JST に待機中・呼出中の予約は自動でキャンセルされます。この深夜キャンセルではユーザー通知は送りません。

## Zabbix での死活監視
- Liveness（プロセス生存確認）: `GET /health` または `GET /healthz`
   - 正常時: `200` / `{"status":"ok","version":"v..."}`
- Readiness（DB疎通込み）: `GET /readyz`
- 負荷テスト用DB操作: `POST /loadtest/db`（`LOAD_TEST_MODE=true` と `LOAD_TEST_TOKEN` の設定が必要）
   - ヘッダー: `X-Loadtest-Token: <LOAD_TEST_TOKEN>`
   - 正常時: `200` / `{"status":"ok","version":"v..."}`
   - トークン不一致時: `403`
   - DB異常時: `503` / `{"status":"error","version":"v..."}`

### k6で実予約処理を負荷試験する

実DBへ予約を作成する100 RPS・10分のシナリオを `loadtests/reservation-callback-100rps.js` に用意しています。試験用環境では、以下を設定してください。

- `LOAD_TEST_MODE=true`（LINEへの返信・Push送信をスキップ）
- `WEBHOOK_RATE_LIMIT_COUNT` を10000以上に設定（100 RPSを1分間受けるため。境界値による429を避ける）
- `RESERVATION_TYPE` と同名の受付中・管理者割り当て済み予約種別
- 実行元から到達可能な `BASE_URL`

```bash
k6 run \
   -e BASE_URL=https://test.example.com \
   -e CHANNEL_SECRET="$CHANNEL_SECRET" \
   -e RESERVATION_TYPE=相談 \
   -e RUN_ID="$(date +%Y%m%d%H%M%S)" \
   loadtests/reservation-callback-100rps.js
```

このシナリオはLINE署名を生成し、異なるユーザーIDで `予約 相談` を送信します。`/callback` は内部エラーでもLINE再送防止のため200を返すため、k6のHTTP成功率だけではDB登録成功率を判定できません。試験後に、送信リクエスト数（100 RPS × 600秒 = 60000）と予約登録数をDBで照合してください。

```sql
SELECT COUNT(*) AS loadtest_reservations
FROM reservations
WHERE user_id LIKE 'Uloadtest<実行時のRUN_ID>%'
   AND created_at >= CURRENT_TIMESTAMP - INTERVAL '15 minutes';
```

試験後に作成データを削除する場合は、対象時刻とユーザーIDの条件を確認してから実行してください。

運用の目安:
1. まず `healthz` を 1 分間隔で監視し、Webプロセス停止を検知する。
2. 追加で `readyz` を監視し、DB障害や接続不可を検知する。
3. トリガー条件は「HTTPステータスが 200 以外」またはレスポンスJSONの `status` 不一致で設定する。

## データベースの定期バックアップ

GitHub Actions の `.github/workflows/database-backup.yml` が、毎週日曜 03:00 JST にフルバックアップ、月曜から土曜の 03:30 JST に差分バックアップを実行します。バックアップは 90 日間 Actions artifact に保持されます。

初回利用前に、リポジトリ Secret `BACKUP_DATABASE_URL`（バックアップ対象 DB の接続 URL）を登録してください。`BACKUP_DATABASE_PASSWORD` を別途設定する場合は、URL にパスワードを含めず、ユーザー名を URL に指定してください。

差分バックアップはデータセクションのみです。復元時は同じ期間のフルバックアップを先に `pg_restore` し、その後に差分バックアップを適用してください。Actions の `workflow_dispatch` からフル／差分を手動実行できます。

## セキュリティ
- セキュリティ強化の概要と運用チェックリスト: `SECURITY_HARDENING.md`
