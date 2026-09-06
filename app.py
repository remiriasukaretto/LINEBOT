"""Flask アプリケーションファクトリ。

Compress/ProxyFix/セッション設定、静的アセットのminify、既に切り出し済みの
モジュール（main_routes / admin_routes / backup_routes / line_routes）の
ルート登録、context_processorの登録を担当する。

login・/tasks/process-call-queueルートや before_request/after_request/
teardown_appcontext フックは main.py 側に残す。これらのフック関数は
main.py 内の他のモジュールレベル関数（ensure_database_schema等）を
バレ名で参照しており、テストの monkeypatch も app_module（main.py）を
対象にしているため、循環importを避けつつ既存の互換性を保てるよう、
あえてapp.py側には移していない。main.py は `from app import create_app`
して `app = create_app()` を呼び、その上に自身のルート・フックを追加する。
"""
import logging
import re
import sys
from datetime import timedelta
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

from flask import Flask  # type: ignore
from flask_compress import Compress  # type: ignore
from linebot.v3.webhooks import MessageEvent, TextMessageContent  # type: ignore
from werkzeug.middleware.proxy_fix import ProxyFix  # type: ignore

from config import parse_bool_env, SECRET_KEY, ADMIN_PASSWORD_DEPRECATED_SET, SESSION_IDLE_TIMEOUT_SECONDS, APP_VERSION, APP_RELEASED_AT
from auth import AppSessionInterface
from formatting import format_duration_from_seconds
from services.queue_service import format_call_origin

from blueprints.main_routes import reservation_type_image, favicon, index
from blueprints.admin_routes import (
    logout,
    admin_login_logs_page,
    admin_login_logs_data,
    admin_accounts_create,
    admin_accounts_update_login_id,
    admin_accounts_toggle_active,
    admin_accounts_delete,
    admin_page,
    admin_data,
    admin_type_counts,
    admin_types_page,
    admin_types_update_image,
    admin_types_delete,
    admin_types_toggle,
    admin_types_update_flavor,
    admin_types_update_name,
    admin_types_update_price,
    admin_history,
    admin_history_export,
    admin_call,
    admin_finish,
    admin_cancel,
    admin_toggle_accepting,
    admin_auto_call_count,
    admin_management_no,
)
from blueprints.backup_routes import (
    admin_backup_page,
    admin_backup_export,
    admin_backup_export_account,
    admin_backup_import,
    admin_backup_import_account,
    admin_delete_all_reservations,
)
from blueprints.line_routes import handler, callback, handle_message


def minify_css_files():
    """Minify CSS files on application startup."""
    css_dir = Path(__file__).parent / "static" / "css"
    if not css_dir.exists():
        return
    for css_file in css_dir.glob("*.css"):
        if css_file.name.endswith(".min.css"):
            continue
        minified_file = css_file.with_name(css_file.stem + ".min.css")
        try:
            content = css_file.read_text(encoding="utf-8")
            # Remove comments
            content = re.sub(r"/\*.*?\*/", "", content, flags=re.DOTALL)
            # Remove spaces around delimiters
            content = re.sub(r"\s*([\{\};:,])\s*", r"\1", content)
            # Remove multiple spaces/newlines
            content = re.sub(r"\s+", " ", content)
            # Remove last semicolon in block
            content = re.sub(r";\}", "}", content)
            minified_file.write_text(content.strip(), encoding="utf-8")
        except Exception as e:
            print(f"Failed to minify {css_file.name}: {e}")


def minify_js_files():
    """Minify JS files on application startup."""
    js_dir = Path(__file__).parent / "static" / "js"
    if not js_dir.exists():
        return
    for js_file in js_dir.glob("*.js"):
        if js_file.name.endswith(".min.js"):
            continue
        minified_file = js_file.with_name(js_file.stem + ".min.js")
        try:
            content = js_file.read_text(encoding="utf-8")
            # Remove multi-line comments
            content = re.sub(r"/\*.*?\*/", "", content, flags=re.DOTALL)

            # Remove single-line comments safely
            lines = []
            for line in content.splitlines():
                stripped = line.strip()
                if stripped.startswith("//"):
                    continue
                match = re.search(r'(?<![:"\'\`])//.*$', line)
                if match:
                    line = line[:match.start()]
                if line.strip():
                    lines.append(line.strip())

            minified = "\n".join(lines)
            minified = re.sub(r"\s*([\{\}\(\);,])\s*", r"\1", minified)
            minified_file.write_text(minified.strip(), encoding="utf-8")
        except Exception as e:
            print(f"Failed to minify {js_file.name}: {e}")


_MAIN_ROUTES = [
    ("/reservation-type-images/<int:type_id>", "reservation_type_image", reservation_type_image, None),
    ("/favicon.ico", "favicon", favicon, None),
    ("/", "index", index, None),
]

_ADMIN_ROUTES = [
    ("/logout", "logout", logout, ["POST"]),
    ("/admin/login-logs", "admin_login_logs_page", admin_login_logs_page, None),
    ("/admin/login-logs/data", "admin_login_logs_data", admin_login_logs_data, None),
    ("/admin/admin-accounts", "admin_accounts_create", admin_accounts_create, ["POST"]),
    ("/admin/admin-accounts/<int:account_id>/login-id", "admin_accounts_update_login_id", admin_accounts_update_login_id, ["POST"]),
    ("/admin/admin-accounts/<int:account_id>/active", "admin_accounts_toggle_active", admin_accounts_toggle_active, ["POST"]),
    ("/admin/admin-accounts/<int:account_id>/delete", "admin_accounts_delete", admin_accounts_delete, ["POST"]),
    ("/admin", "admin_page", admin_page, None),
    ("/admin/data", "admin_data", admin_data, None),
    ("/admin/type_counts", "admin_type_counts", admin_type_counts, None),
    ("/admin/types", "admin_types_page", admin_types_page, ["GET", "POST"]),
    ("/admin/types/<int:type_id>/image", "admin_types_update_image", admin_types_update_image, ["POST"]),
    ("/admin/types/delete/<int:type_id>", "admin_types_delete", admin_types_delete, ["POST"]),
    ("/admin/types/toggle/<int:type_id>", "admin_types_toggle", admin_types_toggle, ["POST"]),
    ("/admin/types/<int:type_id>/flavor", "admin_types_update_flavor", admin_types_update_flavor, ["POST"]),
    ("/admin/types/<int:type_id>/name", "admin_types_update_name", admin_types_update_name, ["POST"]),
    ("/admin/types/<int:type_id>/price", "admin_types_update_price", admin_types_update_price, ["POST"]),
    ("/admin/history", "admin_history", admin_history, None),
    ("/admin/history/export.csv", "admin_history_export", admin_history_export, None),
    ("/admin/call/<int:res_id>", "admin_call", admin_call, ["POST"]),
    ("/admin/finish/<int:res_id>", "admin_finish", admin_finish, ["POST"]),
    ("/admin/cancel/<int:res_id>", "admin_cancel", admin_cancel, ["POST"]),
    ("/admin/toggle-accepting", "admin_toggle_accepting", admin_toggle_accepting, ["POST"]),
    ("/admin/auto-call-count", "admin_auto_call_count", admin_auto_call_count, ["POST"]),
    ("/admin/management-no", "admin_management_no", admin_management_no, ["POST"]),
]

_BACKUP_ROUTES = [
    ("/admin/backup", "admin_backup_page", admin_backup_page, None),
    ("/admin/backup/export", "admin_backup_export", admin_backup_export, None),
    ("/admin/backup/export/<int:account_id>", "admin_backup_export_account", admin_backup_export_account, None),
    ("/admin/backup/import", "admin_backup_import", admin_backup_import, ["POST"]),
    ("/admin/backup/import/<int:account_id>", "admin_backup_import_account", admin_backup_import_account, ["POST"]),
    ("/admin/backup/delete-all-reservations", "admin_delete_all_reservations", admin_delete_all_reservations, ["POST"]),
]

_LINE_ROUTES = [
    ("/callback", "callback", callback, ["POST"]),
]


def create_app():
    app = Flask(__name__)
    Compress(app)
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 1800
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    minify_css_files()
    minify_js_files()

    app.secret_key = SECRET_KEY
    if ADMIN_PASSWORD_DEPRECATED_SET:
        app.logger.warning(
            "ADMIN_PASSWORD is deprecated and ignored. Use ADMIN_PASSWORD_HASH only."
        )

    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=parse_bool_env("SESSION_COOKIE_SECURE", True),
        SESSION_COOKIE_NAME=(
            "__Host-session" if parse_bool_env("SESSION_COOKIE_SECURE", True) else "session"
        ),
        PERMANENT_SESSION_LIFETIME=timedelta(seconds=SESSION_IDLE_TIMEOUT_SECONDS),
    )
    app.jinja_env.autoescape = True
    app.session_interface = AppSessionInterface()

    @app.context_processor
    def inject_template_globals():
        return {
            "app_version": APP_VERSION,
            "app_released_at": APP_RELEASED_AT,
            "format_duration": format_duration_from_seconds,
            "format_call_origin": format_call_origin,
        }

    for path, endpoint, view_func, methods in (
        _MAIN_ROUTES + _ADMIN_ROUTES + _BACKUP_ROUTES + _LINE_ROUTES
    ):
        if methods:
            app.add_url_rule(path, endpoint=endpoint, view_func=view_func, methods=methods)
        else:
            app.add_url_rule(path, endpoint=endpoint, view_func=view_func)

    handler.add(MessageEvent, message=TextMessageContent)(handle_message)

    return app
