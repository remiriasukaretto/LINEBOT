import os
import psycopg2
from flask import Flask, request, abort, render_template_string, redirect, url_for, session, jsonify
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from datetime import datetime

app = Flask(__name__)

# --- セキュリティ設定 ---
# セッションの暗号化キー（Renderの環境変数にランダムな文字列を設定することを推奨）
app.secret_key = os.getenv('SECRET_KEY', 'ukind-secret-2024')
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'ukind2024')

CHANNEL_ACCESS_TOKEN = os.getenv('CHANNEL_ACCESS_TOKEN')
CHANNEL_SECRET = os.getenv('CHANNEL_SECRET')
OWNER_LINE_ID = os.getenv('OWNER_LINE_ID', '').strip()

raw_db_url = os.getenv('DATABASE_URL')
DATABASE_URL = raw_db_url.replace("postgres://", "postgresql://", 1) if raw_db_url else None

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

def get_connection():
    return psycopg2.connect(DATABASE_URL)

# --- HTMLテンプレート（ログイン画面） ---
LOGIN_HTML = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ログイン - UKind</title>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css">
</head>
<body class="bg-light">
    <div class="container mt-5" style="max-width: 400px;">
        <div class="card shadow">
            <div class="card-body">
                <h3 class="text-center mb-4">UKind 管理者ログイン</h3>
                {% if error %}
                <div class="alert alert-danger">{{ error }}</div>
                {% endif %}
                <form method="POST">
                    <div class="mb-3">
                        <label class="form-label">パスワード</label>
                        <input type="password" name="password" class="form-control" required>
                    </div>
                    <button type="submit" class="btn btn-primary w-100">ログイン</button>
                </form>
            </div>
        </div>
    </div>
</body>
</html>
"""

# --- HTMLテンプレート（管理画面本体） ---
ADMIN_HTML = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>UKind 管理画面</title>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css">
</head>
<body class="bg-light">
    <nav class="navbar navbar-dark bg-dark mb-4">
        <div class="container">
            <span class="navbar-brand">UKind 管理パネル</span>
            <div class="d-flex gap-2">
                <a href="/admin/types" class="btn btn-outline-light btn-sm">種類管理</a>
                <a href="/admin/history" class="btn btn-outline-light btn-sm">過去ログ</a>
                <a href="/logout" class="btn btn-outline-light btn-sm">ログアウト</a>
            </div>
        </div>
    </nav>
    <div class="container">
        <div class="card shadow-sm mb-4">
            <div class="card-body">
                <div class="row g-2 align-items-center">
                    <div class="col-sm-6">
                        <label class="form-label">種類で絞り込み</label>
                        <select id="type-filter" class="form-select">
                            <option value="">すべて</option>
                            {% for t in types %}
                            <option value="{{ t[0] }}" {% if current_type_id == t[0] %}selected{% endif %}>{{ t[1] }}</option>
                            {% endfor %}
                        </select>
                    </div>
                    <div class="col-sm-6">
                        <label class="form-label">待機数（種類別）</label>
                        <div class="d-flex flex-wrap gap-2">
                            {% for c in type_counts %}
                            <span class="badge bg-secondary">{{ c[0] }}: {{ c[1] }}</span>
                            {% endfor %}
                        </div>
                    </div>
                </div>
                <div class="row g-2 align-items-center mt-2">
                    <div class="col-sm-6">
                        <label class="form-label">並べ替え</label>
                        <select id="sort-by" class="form-select">
                            <option value="id" {% if sort_by == 'id' %}selected{% endif %}>番号</option>
                            <option value="status" {% if sort_by == 'status' %}selected{% endif %}>状態</option>
                            <option value="type" {% if sort_by == 'type' %}selected{% endif %}>種類</option>
                            <option value="message" {% if sort_by == 'message' %}selected{% endif %}>メッセージ</option>
                        </select>
                    </div>
                    <div class="col-sm-6">
                        <label class="form-label">順序</label>
                        <select id="sort-order" class="form-select">
                            <option value="asc" {% if sort_order == 'asc' %}selected{% endif %}>昇順</option>
                            <option value="desc" {% if sort_order == 'desc' %}selected{% endif %}>降順</option>
                        </select>
                    </div>
                </div>
            </div>
        </div>
        <div class="card shadow-sm">
            <div class="card-body p-0">
                <table class="table table-striped mb-0">
                    <thead class="table-dark">
                        <tr>
                            <th>番号</th>
                            <th>種類</th>
                            <th>メッセージ</th>
                            <th>状態</th>
                            <th>操作</th>
                        </tr>
                    </thead>
                    <tbody id="active-rows">
                        {% for row in rows %}
                        <tr>
                            <td>{{ row[0] }}</td>
                            <td>{{ row[4] or '-' }}</td>
                            <td>{{ row[2] or '-' }}</td>
                            <td>
                                {% if row[3] == 'waiting' %}
                                <span class="badge bg-warning text-dark">待機中</span>
                                {% elif row[3] == 'called' %}
                                <span class="badge bg-info">呼出中</span>
                                {% else %}
                                <span class="badge bg-success">到着済み</span>
                                {% endif %}
                            </td>
                            <td>
                                {% if row[3] == 'waiting' %}
                                <div class="d-flex gap-1">
                                    <a href="/admin/call/{{ row[0] }}" class="btn btn-sm btn-success">呼出</a>
                                    <a href="/admin/cancel/{{ row[0] }}" class="btn btn-sm btn-outline-danger">中止</a>
                                </div>
                                {% elif row[3] == 'called' %}
                                <div class="d-flex gap-1">
                                    <span class="text-muted small">到着待ち</span>
                                    <a href="/admin/cancel/{{ row[0] }}" class="btn btn-sm btn-outline-danger">中止</a>
                                </div>
                                {% else %}
                                <div class="d-flex gap-1">
                                    <a href="/admin/finish/{{ row[0] }}" class="btn btn-sm btn-primary">確認完了</a>
                                    <a href="/admin/cancel/{{ row[0] }}" class="btn btn-sm btn-outline-danger">中止</a>
                                </div>
                                {% endif %}
                            </td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>
        </div>
        <div class="text-center mt-4">
            <a href="/admin" class="btn btn-secondary">リストを更新</a>
        </div>
    </div>
    <script>
        function getQueryParams() {
            const params = new URLSearchParams();
            const select = document.getElementById('type-filter');
            if (select && select.value) params.set('type_id', select.value);
            const sortBy = document.getElementById('sort-by');
            if (sortBy && sortBy.value) params.set('sort_by', sortBy.value);
            const sortOrder = document.getElementById('sort-order');
            if (sortOrder && sortOrder.value) params.set('sort_order', sortOrder.value);
            const q = params.toString();
            return q ? `?${q}` : '';
        }

        async function refreshActiveRows() {
            try {
                const res = await fetch('/admin/data' + getQueryParams(), { cache: 'no-store' });
                if (!res.ok) return;
                const data = await res.json();
                const tbody = document.getElementById('active-rows');
                if (!tbody) return;
                tbody.innerHTML = data.rows.map(row => {
                    const id = row.id;
                    const message = row.message || '-';
                    const typeName = row.type || '-';
                    let statusBadge = '';
                    let action = '';
                    if (row.status === 'waiting') {
                        statusBadge = '<span class="badge bg-warning text-dark">待機中</span>';
                        action = `<div class="d-flex gap-1"><a href="/admin/call/${id}" class="btn btn-sm btn-success">呼出</a><a href="/admin/cancel/${id}" class="btn btn-sm btn-outline-danger">中止</a></div>`;
                    } else if (row.status === 'called') {
                        statusBadge = '<span class="badge bg-info">呼出中</span>';
                        action = `<div class="d-flex gap-1"><span class="text-muted small">到着待ち</span><a href="/admin/cancel/${id}" class="btn btn-sm btn-outline-danger">中止</a></div>`;
                    } else {
                        statusBadge = '<span class="badge bg-success">到着済み</span>';
                        action = `<div class="d-flex gap-1"><a href="/admin/finish/${id}" class="btn btn-sm btn-primary">確認完了</a><a href="/admin/cancel/${id}" class="btn btn-sm btn-outline-danger">中止</a></div>`;
                    }
                    return `<tr>
                        <td>${id}</td>
                        <td>${typeName}</td>
                        <td>${message}</td>
                        <td>${statusBadge}</td>
                        <td>${action}</td>
                    </tr>`;
                }).join('');
            } catch (e) {
                // no-op
            }
        }
        setInterval(refreshActiveRows, 5000);
        function applyAdminFilters() {
            window.location.href = '/admin' + getQueryParams();
        }
        document.getElementById('type-filter')?.addEventListener('change', applyAdminFilters);
        document.getElementById('sort-by')?.addEventListener('change', applyAdminFilters);
        document.getElementById('sort-order')?.addEventListener('change', applyAdminFilters);
    </script>
</body>
</html>
"""

TYPES_HTML = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>UKind 予約種類管理</title>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css">
</head>
<body class="bg-light">
    <nav class="navbar navbar-dark bg-dark mb-4">
        <div class="container">
            <span class="navbar-brand">UKind 予約種類管理</span>
            <div class="d-flex gap-2">
                <a href="/admin" class="btn btn-outline-light btn-sm">管理画面</a>
                <a href="/admin/history" class="btn btn-outline-light btn-sm">過去ログ</a>
                <a href="/logout" class="btn btn-outline-light btn-sm">ログアウト</a>
            </div>
        </div>
    </nav>
    <div class="container">
        <div class="card shadow-sm mb-4">
            <div class="card-body">
                <h5 class="card-title">種類を追加</h5>
                {% if type_error %}
                <div class="alert alert-danger">{{ type_error }}</div>
                {% endif %}
                {% if type_success %}
                <div class="alert alert-success">{{ type_success }}</div>
                {% endif %}
                <form method="POST" action="/admin/types" class="row g-2 align-items-center">
                    <div class="col-sm-8">
                        <input type="text" name="name" class="form-control" placeholder="種類名（例：相談 / 受付 / 案内）" required>
                    </div>
                    <div class="col-sm-4 d-grid">
                        <button type="submit" class="btn btn-primary">追加</button>
                    </div>
                </form>
            </div>
        </div>
        <div class="card shadow-sm">
            <div class="card-body">
                <h5 class="card-title">登録済みの種類</h5>
                <ul class="list-group">
                    {% for t in types %}
                    <li class="list-group-item d-flex justify-content-between align-items-center">
                        <span>{{ t[1] }}</span>
                        <a href="/admin/types/delete/{{ t[0] }}" class="btn btn-sm btn-outline-danger">削除</a>
                    </li>
                    {% else %}
                    <li class="list-group-item text-muted">登録されている種類はありません。</li>
                    {% endfor %}
                </ul>
            </div>
        </div>
    </div>
</body>
</html>
"""

# --- HTMLテンプレート（過去ログ） ---
HISTORY_HTML = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>UKind 過去ログ</title>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css">
</head>
<body class="bg-light">
    <nav class="navbar navbar-dark bg-dark mb-4">
        <div class="container">
            <span class="navbar-brand">UKind 過去ログ</span>
            <div class="d-flex gap-2">
                <a href="/admin" class="btn btn-outline-light btn-sm">管理画面</a>
                <a href="/logout" class="btn btn-outline-light btn-sm">ログアウト</a>
            </div>
        </div>
    </nav>
    <div class="container">
        <div class="card shadow-sm mb-4">
            <div class="card-body">
                <div class="row g-2 align-items-center">
                    <div class="col-sm-6">
                        <label class="form-label">種類で絞り込み</label>
                        <select id="history-type-filter" class="form-select">
                            <option value="">すべて</option>
                            {% for t in types %}
                            <option value="{{ t[0] }}" {% if current_type_id == t[0] %}selected{% endif %}>{{ t[1] }}</option>
                            {% endfor %}
                        </select>
                    </div>
                    <div class="col-sm-6">
                        <label class="form-label">並べ替え</label>
                        <div class="d-flex gap-2">
                            <select id="history-sort-by" class="form-select">
                                <option value="id" {% if sort_by == 'id' %}selected{% endif %}>番号</option>
                                <option value="status" {% if sort_by == 'status' %}selected{% endif %}>状態</option>
                                <option value="type" {% if sort_by == 'type' %}selected{% endif %}>種類</option>
                                <option value="message" {% if sort_by == 'message' %}selected{% endif %}>メッセージ</option>
                            </select>
                            <select id="history-sort-order" class="form-select">
                                <option value="asc" {% if sort_order == 'asc' %}selected{% endif %}>昇順</option>
                                <option value="desc" {% if sort_order == 'desc' %}selected{% endif %}>降順</option>
                            </select>
                        </div>
                    </div>
                </div>
            </div>
        </div>
        <div class="card shadow-sm">
            <div class="card-body p-0">
                <table class="table table-striped mb-0">
                    <thead class="table-dark">
                        <tr>
                            <th>番号</th>
                            <th>種類</th>
                            <th>メッセージ</th>
                            <th>状態</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for row in rows %}
                        <tr>
                            <td>{{ row[0] }}</td>
                            <td>{{ row[4] or '-' }}</td>
                            <td>{{ row[2] or '-' }}</td>
                            <td>
                                {% if row[3] == 'done' %}
                                <span class="badge bg-primary">確認完了</span>
                                {% elif row[3] == 'cancelled' %}
                                <span class="badge bg-secondary">キャンセル</span>
                                {% else %}
                                <span class="badge bg-success">到着済み</span>
                                {% endif %}
                            </td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>
        </div>
        <div class="text-center mt-4">
            <a href="/admin/history" class="btn btn-secondary">リストを更新</a>
        </div>
    </div>
    <script>
        function getHistoryQueryParams() {
            const params = new URLSearchParams();
            const select = document.getElementById('history-type-filter');
            if (select && select.value) params.set('type_id', select.value);
            const sortBy = document.getElementById('history-sort-by');
            if (sortBy && sortBy.value) params.set('sort_by', sortBy.value);
            const sortOrder = document.getElementById('history-sort-order');
            if (sortOrder && sortOrder.value) params.set('sort_order', sortOrder.value);
            const q = params.toString();
            return q ? `?${q}` : '';
        }
        function applyHistoryFilters() {
            window.location.href = '/admin/history' + getHistoryQueryParams();
        }
        document.getElementById('history-type-filter')?.addEventListener('change', applyHistoryFilters);
        document.getElementById('history-sort-by')?.addEventListener('change', applyHistoryFilters);
        document.getElementById('history-sort-order')?.addEventListener('change', applyHistoryFilters);
    </script>
</body>
</html>
"""

# --- ルーティング ---

@app.route("/")
def index():
    return redirect(url_for("login"))

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        if request.form.get("password") == ADMIN_PASSWORD:
            session["logged_in"] = True
            return redirect(url_for("admin_page"))
        else:
            error = "パスワードが正しくありません"
    return render_template_string(LOGIN_HTML, error=error)

def ensure_types_table():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS reservation_types (
                    id SERIAL PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute("""
                ALTER TABLE reservations
                ADD COLUMN IF NOT EXISTS type_id INTEGER
                REFERENCES reservation_types(id) ON DELETE SET NULL
            """)
            conn.commit()

@app.route("/logout")
def logout():
    session.pop("logged_in", None)
    return redirect(url_for("login"))

@app.route("/admin")
def admin_page():
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    ensure_types_table()
    type_error = request.args.get("type_error")
    type_id = request.args.get("type_id", "").strip()
    current_type_id = int(type_id) if type_id.isdigit() else None
    sort_by = request.args.get("sort_by", "id").strip()
    sort_order = request.args.get("sort_order", "asc").strip().lower()
    if sort_by not in ("id", "status", "type", "message"):
        sort_by = "id"
    if sort_order not in ("asc", "desc"):
        sort_order = "asc"

    with get_connection() as conn:
        with conn.cursor() as cur:
            params = []
            where = "WHERE r.status IN ('waiting', 'called', 'arrived')"
            if current_type_id is not None:
                where += " AND r.type_id = %s"
                params.append(current_type_id)
            order_map = {
                "id": "r.id",
                "status": "r.status",
                "type": "t.name",
                "message": "r.message"
            }
            order_by = order_map[sort_by]
            cur.execute(f"""
                SELECT r.id, r.user_id, r.message, r.status, t.name
                FROM reservations r
                LEFT JOIN reservation_types t ON r.type_id = t.id
                {where}
                ORDER BY {order_by} {sort_order.upper()}, r.id ASC
            """, params)
            rows = cur.fetchall()
            cur.execute("SELECT id, name FROM reservation_types ORDER BY id ASC")
            types = cur.fetchall()
            cur.execute("""
                SELECT COALESCE(t.name, '未設定') AS name, COUNT(*)
                FROM reservations r
                LEFT JOIN reservation_types t ON r.type_id = t.id
                WHERE r.status IN ('waiting', 'called', 'arrived')
                GROUP BY COALESCE(t.name, '未設定')
                ORDER BY COUNT(*) DESC
            """)
            type_counts = cur.fetchall()
    return render_template_string(
        ADMIN_HTML,
        rows=rows,
        types=types,
        type_error=type_error,
        current_type_id=current_type_id,
        type_counts=type_counts,
        sort_by=sort_by,
        sort_order=sort_order
    )

@app.route("/admin/data")
def admin_data():
    if not session.get("logged_in"):
        return jsonify({"error": "unauthorized"}), 401

    with get_connection() as conn:
        with conn.cursor() as cur:
            type_id = request.args.get("type_id", "").strip()
            current_type_id = int(type_id) if type_id.isdigit() else None
            sort_by = request.args.get("sort_by", "id").strip()
            sort_order = request.args.get("sort_order", "asc").strip().lower()
            if sort_by not in ("id", "status", "type", "message"):
                sort_by = "id"
            if sort_order not in ("asc", "desc"):
                sort_order = "asc"
            params = []
            where = "WHERE r.status IN ('waiting', 'called', 'arrived')"
            if current_type_id is not None:
                where += " AND r.type_id = %s"
                params.append(current_type_id)
            order_map = {
                "id": "r.id",
                "status": "r.status",
                "type": "t.name",
                "message": "r.message"
            }
            order_by = order_map[sort_by]
            cur.execute(f"""
                SELECT r.id, r.message, r.status, t.name
                FROM reservations r
                LEFT JOIN reservation_types t ON r.type_id = t.id
                {where}
                ORDER BY {order_by} {sort_order.upper()}, r.id ASC
            """, params)
            rows = cur.fetchall()
    return jsonify({
        "rows": [
            {"id": row[0], "message": row[1], "status": row[2], "type": row[3]}
            for row in rows
        ]
    })

@app.route("/admin/types", methods=["GET", "POST"])
def admin_types_page():
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    ensure_types_table()
    type_error = request.args.get("type_error")
    type_success = request.args.get("type_success")
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        if not name:
            return redirect(url_for("admin_types_page", type_error="種類名を入力してください。"))
        try:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("INSERT INTO reservation_types (name) VALUES (%s)", (name,))
                    conn.commit()
            return redirect(url_for("admin_types_page", type_success="種類を追加しました。"))
        except psycopg2.IntegrityError:
            return redirect(url_for("admin_types_page", type_error="同じ名前の種類が既に存在します。"))

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name FROM reservation_types ORDER BY id ASC")
            types = cur.fetchall()
    return render_template_string(TYPES_HTML, types=types, type_error=type_error, type_success=type_success)

@app.route("/admin/types/delete/<int:type_id>")
def admin_types_delete(type_id):
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    ensure_types_table()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM reservation_types WHERE id = %s", (type_id,))
            conn.commit()
    return redirect(url_for("admin_types_page"))

@app.route("/admin/history")
def admin_history():
    if not session.get("logged_in"):
        return redirect(url_for("login"))

    ensure_types_table()
    with get_connection() as conn:
        with conn.cursor() as cur:
            type_id = request.args.get("type_id", "").strip()
            current_type_id = int(type_id) if type_id.isdigit() else None
            sort_by = request.args.get("sort_by", "id").strip()
            sort_order = request.args.get("sort_order", "desc").strip().lower()
            if sort_by not in ("id", "status", "type", "message"):
                sort_by = "id"
            if sort_order not in ("asc", "desc"):
                sort_order = "desc"
            params = []
            where = "WHERE r.status IN ('done', 'cancelled', 'arrived')"
            if current_type_id is not None:
                where += " AND r.type_id = %s"
                params.append(current_type_id)
            order_map = {
                "id": "r.id",
                "status": "r.status",
                "type": "t.name",
                "message": "r.message"
            }
            order_by = order_map[sort_by]
            cur.execute(f"""
                SELECT r.id, r.user_id, r.message, r.status, t.name
                FROM reservations r
                LEFT JOIN reservation_types t ON r.type_id = t.id
                {where}
                ORDER BY {order_by} {sort_order.upper()}, r.id DESC LIMIT 200
            """, params)
            rows = cur.fetchall()
            cur.execute("SELECT id, name FROM reservation_types ORDER BY id ASC")
            types = cur.fetchall()
    return render_template_string(
        HISTORY_HTML,
        rows=rows,
        types=types,
        current_type_id=current_type_id,
        sort_by=sort_by,
        sort_order=sort_order
    )

@app.route("/admin/call/<int:res_id>")
def admin_call(res_id):
    if not session.get("logged_in"): return redirect(url_for("login"))

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT user_id FROM reservations WHERE id = %s", (res_id,))
            user_id = cur.fetchone()[0]
            cur.execute("UPDATE reservations SET status = 'called' WHERE id = %s", (res_id,))
            conn.commit()
            line_bot_api.push_message(user_id, TextSendMessage(text=f"【順番が来ました】番号 {res_id} 番の方、会場へお越しください！"))
    return redirect(url_for("admin_page"))

@app.route("/admin/finish/<int:res_id>")
def admin_finish(res_id):
    if not session.get("logged_in"): return redirect(url_for("login"))

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE reservations SET status = 'done' WHERE id = %s", (res_id,))
            conn.commit()
    return redirect(url_for("admin_page"))

@app.route("/admin/cancel/<int:res_id>")
def admin_cancel(res_id):
    if not session.get("logged_in"): return redirect(url_for("login"))

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE reservations SET status = 'cancelled' WHERE id = %s", (res_id,))
            conn.commit()
    return redirect(url_for("admin_page"))

# --- LINE Webhook ---
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
    user_message = event.message.text.strip()
    user_id = event.source.user_id
    process_reservation(event, user_id, user_message)

def process_reservation(event, user_id, user_message):
    normalized = user_message.strip()
    if not normalized:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="メッセージを受け付けました。予約は「予約」、キャンセルは「キャンセル」、到着は「到着」と送信してください。")
        )
        return

    with get_connection() as conn:
        with conn.cursor() as cur:
            if normalized.startswith('予約'):
                ensure_types_table()
                requested_type_name = normalized[2:].strip()
                type_id = None
                type_name = None
                if requested_type_name:
                    cur.execute("SELECT id, name FROM reservation_types WHERE name = %s", (requested_type_name,))
                    type_row = cur.fetchone()
                    if not type_row:
                        cur.execute("SELECT name FROM reservation_types ORDER BY id ASC")
                        names = [r[0] for r in cur.fetchall()]
                        if names:
                            reply = f"指定した種類「{requested_type_name}」は存在しません。\n利用可能: " + " / ".join(names)
                        else:
                            reply = "予約の種類がまだ登録されていません。管理画面で追加してください。"
                        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
                        return
                    type_id, type_name = type_row
                else:
                    cur.execute("SELECT name FROM reservation_types ORDER BY id ASC")
                    names = [r[0] for r in cur.fetchall()]
                    if names:
                        reply = "予約の種類を指定してください。\n利用可能: " + " / ".join(names) + "\n例: 予約 相談"
                    else:
                        reply = "予約の種類がまだ登録されていません。管理画面で追加してください。"
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
                    return

                cur.execute(
                    """
                        SELECT r.id, r.status, t.name
                        FROM reservations r
                        LEFT JOIN reservation_types t ON r.type_id = t.id
                        WHERE r.user_id = %s AND r.status IN ('waiting', 'called', 'arrived')
                        ORDER BY r.id DESC LIMIT 1
                    """,
                    (user_id,)
                )
                existing = cur.fetchone()
                if existing:
                    res_id, status, existing_type_name = existing
                    if status == 'waiting':
                        if existing_type_name:
                            cur.execute("SELECT COUNT(*) FROM reservations WHERE status = 'waiting' AND id < %s AND type_id = (SELECT type_id FROM reservations WHERE id = %s)", (res_id, res_id))
                            reply = f"予約済みです。番号: {res_id} / 種類: {existing_type_name} / 待ち: {cur.fetchone()[0]}人"
                        else:
                            cur.execute("SELECT COUNT(*) FROM reservations WHERE status = 'waiting' AND id < %s", (res_id,))
                            reply = f"予約済みです。番号: {res_id} / 待ち: {cur.fetchone()[0]}人"
                    elif status == 'called':
                        if existing_type_name:
                            reply = f"【呼出中】番号: {res_id} / 種類: {existing_type_name} 会場へお越しください！"
                        else:
                            reply = f"【呼出中】番号: {res_id} 会場へお越しください！"
                    else:
                        if existing_type_name:
                            reply = f"到着受付済みです。番号: {res_id} / 種類: {existing_type_name} / スタッフが確認します。"
                        else:
                            reply = f"到着受付済みです。番号: {res_id} / スタッフが確認します。"
                else:
                    cur.execute("INSERT INTO reservations (user_id, message, type_id) VALUES (%s, %s, %s) RETURNING id", (user_id, user_message, type_id))
                    new_id = cur.fetchone()[0]
                    conn.commit()
                    if type_id:
                        cur.execute("SELECT COUNT(*) FROM reservations WHERE status = 'waiting' AND id < %s AND type_id = %s", (new_id, type_id))
                        reply = f"【受付完了】番号: {new_id} / 種類: {type_name} / 待ち: {cur.fetchone()[0]}人"
                    else:
                        cur.execute("SELECT COUNT(*) FROM reservations WHERE status = 'waiting' AND id < %s", (new_id,))
                        reply = f"【受付完了】番号: {new_id} / 待ち: {cur.fetchone()[0]}人"
            elif normalized == 'キャンセル':
                cur.execute(
                    "UPDATE reservations SET status = 'cancelled' WHERE id = (SELECT id FROM reservations WHERE user_id = %s AND status IN ('waiting', 'called') ORDER BY id DESC LIMIT 1) RETURNING id",
                    (user_id,)
                )
                cancelled = cur.fetchone()
                if cancelled:
                    reply = f"予約番号 {cancelled[0]} をキャンセルしました。"
                else:
                    reply = "キャンセル対象の予約はありません。"
            elif normalized == '到着':
                cur.execute(
                    "SELECT id, status FROM reservations WHERE user_id = %s AND status IN ('waiting', 'called') ORDER BY id DESC LIMIT 1",
                    (user_id,)
                )
                existing = cur.fetchone()
                if not existing:
                    reply = "到着の対象となる予約がありません。"
                else:
                    res_id, status = existing
                    if status == 'waiting':
                        reply = "まだ呼出されていません。呼出後に「到着」と送信してください。"
                    else:
                        cur.execute("UPDATE reservations SET status = 'arrived' WHERE id = %s", (res_id,))
                        conn.commit()
                        reply = f"到着を受け付けました。番号: {res_id} / スタッフが確認します。"
            else:
                reply = "メッセージを受け付けました。予約は「予約」、キャンセルは「キャンセル」、到着は「到着」と送信してください。"
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
