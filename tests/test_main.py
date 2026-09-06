import csv
import threading
from io import BytesIO
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from werkzeug.exceptions import BadRequest, Forbidden
from linebot.v3.messaging.models.flex_image import FlexImage


def flex_message_text(message):
    assert message["type"] == "flex"
    header = message["contents"].get("header", {}).get("contents", [])
    body = message["contents"]["body"]["contents"]
    title = header[0]["text"] if header else ""
    body_text = "\n".join(item["text"] for item in body if item.get("type") == "text")
    return f"{title}\n{body_text}".strip()


def test_parse_bool_env_true_false_default(app_module, monkeypatch):
    monkeypatch.setenv("TEST_BOOL", "true")
    assert app_module.parse_bool_env("TEST_BOOL", False) is True

    monkeypatch.setenv("TEST_BOOL", "off")
    assert app_module.parse_bool_env("TEST_BOOL", True) is False

    monkeypatch.delenv("TEST_BOOL", raising=False)
    assert app_module.parse_bool_env("TEST_BOOL", True) is True


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on", " On "])
def test_parse_bool_env_truthy_variants(app_module, monkeypatch, raw):
    monkeypatch.setenv("TEST_BOOL", raw)
    assert app_module.parse_bool_env("TEST_BOOL", False) is True


@pytest.mark.parametrize("raw", ["0", "false", "no", "off", "", " random "])
def test_parse_bool_env_falsy_variants(app_module, monkeypatch, raw):
    monkeypatch.setenv("TEST_BOOL", raw)
    assert app_module.parse_bool_env("TEST_BOOL", True) is False


def test_normalize_db_url_remote_adds_sslmode(app_module):
    normalized = app_module.normalize_db_url("postgres://u:p@example.com:5432/db")
    assert normalized.startswith("postgresql://")
    assert "sslmode=require" in normalized


def test_normalize_db_url_localhost_keeps_no_sslmode(app_module):
    normalized = app_module.normalize_db_url("postgresql://u:p@localhost:5432/db")
    assert "sslmode=require" not in normalized


def test_normalize_db_url_invalid_raises(app_module):
    with pytest.raises(RuntimeError):
        app_module.normalize_db_url("not-a-url")


def test_parse_allowed_hosts_supports_multiple_separators(app_module):
    parsed = app_module.parse_allowed_hosts("example.com, api.example.com  admin.example.com")
    assert parsed == {"example.com", "api.example.com", "admin.example.com"}


def test_css_minification_creates_minified_files(app_module):
    from pathlib import Path
    css_dir = Path(app_module.__file__).parent / "static" / "css"
    app_module.minify_css_files()
    
    # Check that each CSS file has a corresponding min.css
    for css_file in css_dir.glob("*.css"):
        if css_file.name.endswith(".min.css"):
            continue
        minified_file = css_file.with_name(css_file.stem + ".min.css")
        assert minified_file.exists()
        original_content = css_file.read_text(encoding="utf-8")
        minified_content = minified_file.read_text(encoding="utf-8")
        assert len(minified_content) <= len(original_content)
        assert "/*" not in minified_content


def test_js_minification_creates_minified_files(app_module):
    from pathlib import Path
    js_dir = Path(app_module.__file__).parent / "static" / "js"
    app_module.minify_js_files()
    
    # Check that each JS file has a corresponding min.js
    for js_file in js_dir.glob("*.js"):
        if js_file.name.endswith(".min.js"):
            continue
        minified_file = js_file.with_name(js_file.stem + ".min.js")
        assert minified_file.exists()
        original_content = js_file.read_text(encoding="utf-8")
        minified_content = minified_file.read_text(encoding="utf-8")
        assert len(minified_content) <= len(original_content)
        assert "/*" not in minified_content


def test_text_compression_gzip(client):
    response = client.get("/login", headers=[("Accept-Encoding", "gzip")])
    assert response.status_code == 200
    assert "gzip" in response.headers.get("Content-Encoding", "").lower()


def test_healthz_endpoint_returns_ok(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "ok"
    assert payload["version"]


def test_health_endpoint_alias_returns_ok(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.get_json()["status"] == "ok"


def test_readyz_returns_200_when_db_is_available(client, app_module, monkeypatch):
    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, _query, _params=None):
            return None

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

    monkeypatch.setattr(app_module, "get_connection", lambda: FakeConnection())
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.get_json()["status"] == "ready"


def test_readyz_returns_503_when_db_is_unavailable(client, app_module, monkeypatch):
    def fail_connection():
        raise RuntimeError("db down")

    monkeypatch.setattr(app_module, "get_connection", fail_connection)
    response = client.get("/readyz")
    assert response.status_code == 503
    assert response.get_json()["status"] == "unready"


def test_loadtest_db_returns_404_when_disabled(client, app_module, monkeypatch):
    monkeypatch.setattr(app_module, "LOAD_TEST_MODE", False)
    response = client.post("/loadtest/db")
    assert response.status_code == 404


def test_loadtest_db_rejects_invalid_token_without_database_access(
    client, app_module, monkeypatch
):
    monkeypatch.setattr(app_module, "LOAD_TEST_MODE", True)
    monkeypatch.setattr(app_module, "LOAD_TEST_TOKEN", "expected-token")
    database_accessed = False

    def fail_connection():
        nonlocal database_accessed
        database_accessed = True
        raise AssertionError("database must not be accessed")

    monkeypatch.setattr(app_module, "get_connection", fail_connection)
    response = client.post(
        "/loadtest/db", headers={"X-Loadtest-Token": "wrong-token"}
    )

    assert response.status_code == 403
    assert database_accessed is False


def test_loadtest_db_uses_database_and_cleans_up(client, app_module, monkeypatch):
    monkeypatch.setattr(app_module, "LOAD_TEST_MODE", True)
    monkeypatch.setattr(app_module, "LOAD_TEST_TOKEN", "expected-token")
    executed = []

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            executed.append((query, params))

        def fetchone(self):
            return (42,)

    class FakeConnection:
        closed = False

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

        def commit(self):
            executed.append(("COMMIT", None))

    monkeypatch.setattr(app_module, "get_connection", lambda: FakeConnection())
    response = client.post(
        "/loadtest/db", headers={"X-Loadtest-Token": "expected-token"}
    )

    assert response.status_code == 200
    assert response.get_json()["status"] == "ok"
    assert executed[0][0].lstrip().startswith("INSERT INTO reservations")
    assert "SELECT id FROM reservations" in executed[1][0]
    assert executed[2][0].startswith("DELETE FROM reservations")
    assert executed[3][0] == "COMMIT"


def test_loadtest_db_returns_503_when_database_fails(client, app_module, monkeypatch):
    monkeypatch.setattr(app_module, "LOAD_TEST_MODE", True)
    monkeypatch.setattr(app_module, "LOAD_TEST_TOKEN", "expected-token")

    def fail_connection():
        raise RuntimeError("db down")

    monkeypatch.setattr(app_module, "get_connection", fail_connection)
    response = client.post(
        "/loadtest/db", headers={"X-Loadtest-Token": "expected-token"}
    )
    assert response.status_code == 503
    assert response.get_json()["status"] == "error"


def test_initialize_database_once_skips_health_routes(app_module, monkeypatch):
    called = {"count": 0}

    def fake_ensure_schema():
        called["count"] += 1

    monkeypatch.setattr(app_module, "ensure_database_schema", fake_ensure_schema)

    with app_module.app.test_request_context("/health"):
        app_module.initialize_database_once()
    with app_module.app.test_request_context("/healthz"):
        app_module.initialize_database_once()
    with app_module.app.test_request_context("/readyz"):
        app_module.initialize_database_once()
    with app_module.app.test_request_context("/admin"):
        app_module.initialize_database_once()

    assert called["count"] == 1


def test_build_type_image_url_prefers_public_base_url(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "PUBLIC_BASE_URL", "https://example.com")
    assert app_module.build_type_image_url(3, 2) == "https://example.com/reservation-type-images/3?v=2"


def test_build_type_image_url_forces_https_from_request(app_module, monkeypatch):
    monkeypatch.setattr(app_module.line_service, "PUBLIC_BASE_URL", "")
    with app_module.app.test_request_context("/", base_url="http://api.example.com"):
        assert app_module.build_type_image_url(3, 2) == "https://api.example.com/reservation-type-images/3?v=2"


def test_build_flex_component_handles_image(app_module):
    image = app_module.build_flex_component(
        {
            "type": "image",
            "url": "https://example.com/type.png",
            "size": "full",
            "aspectRatio": "16:9",
            "aspectMode": "cover",
        }
    )
    assert isinstance(image, FlexImage)
    assert image.url == "https://example.com/type.png"


def test_sanitize_flex_message_removes_invalid_hero_urls(app_module):
    message = {
        "type": "flex",
        "altText": "予約の種類一覧",
        "contents": {
            "type": "carousel",
            "contents": [
                {
                    "type": "bubble",
                    "hero": {"type": "image", "url": "https://example.com/ok.png"},
                },
                {
                    "type": "bubble",
                    "hero": {"type": "image", "url": "/static/bad.png"},
                },
            ],
        },
    }

    sanitized = app_module.sanitize_flex_message(message)
    bubbles = sanitized["contents"]["contents"]
    assert bubbles[0]["hero"]["url"] == "https://example.com/ok.png"
    assert "hero" not in bubbles[1]


def test_enforce_host_allowlist_accepts_multiple_hosts(app_module, monkeypatch):
    monkeypatch.setattr(
        app_module,
        "ALLOWED_HOSTS",
        {"example.com", "api.example.com"},
    )
    with app_module.app.test_request_context("/", base_url="https://api.example.com"):
        app_module.enforce_host_allowlist()


def test_enforce_host_allowlist_rejects_unknown_host(app_module, monkeypatch):
    monkeypatch.setattr(
        app_module.auth,
        "ALLOWED_HOSTS",
        {"example.com", "api.example.com"},
    )
    with app_module.app.test_request_context("/", base_url="https://evil.example.net"):
        with pytest.raises(BadRequest):
            app_module.enforce_host_allowlist()


def test_ensure_reservations_table_adds_type_id_column(app_module, monkeypatch):
    queries = []

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            queries.append((query, params))

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

        def commit(self):
            return None

    monkeypatch.setattr(app_module, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.admin_routes, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.database, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.queue_service, "get_connection", lambda: FakeConnection())

    app_module.ensure_reservations_table()

    normalized_queries = [" ".join(query.split()) for query, _ in queries]
    assert any(
        "ALTER TABLE reservations ADD COLUMN IF NOT EXISTS type_id INTEGER" in query
        for query in normalized_queries
    )
    assert any(
        "ALTER TABLE reservations ALTER COLUMN user_id SET NOT NULL" in query
        for query in normalized_queries
    )
    assert any(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_reservations_user_active ON reservations (user_id) WHERE status IN ('waiting', 'called')"
        in query
        for query in normalized_queries
    )


def test_process_reservation_persists_user_id_on_new_booking(app_module, monkeypatch):
    queries = []

    class FakeCursor:
        def __init__(self):
            self._last = None

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            queries.append((query, params))
            if "FROM reservation_types" in query and "WHERE name = %s" in query:
                self._last = (1, "相談", True, 7, "説明", "")
            elif "WHERE r.user_id = %s AND r.status IN" in query:
                self._last = None
            elif "next_reservation_no" in query and "FROM admin_accounts" in query:
                self._last = (1,)
            elif "FROM admin_accounts" in query and "login_id" in query:
                self._last = None
            elif "UPDATE admin_accounts" in query and "next_reservation_no" in query:
                self._last = None
            elif "SELECT reservation_no FROM reservations" in query:
                self._last_all = []
            elif "INSERT INTO reservations" in query:
                self._last = (10,)
            elif "SELECT 1 FROM reservations" in query:
                self._last = None
            elif "FROM app_settings" in query:
                self._last = None
            elif (
                "JOIN reservation_types t ON r.type_id = t.id" in query
                and "r.id < %s" in query
            ):
                self._last = (2,)
            else:
                raise AssertionError(f"Unexpected query: {query}")

        def fetchone(self):
            return self._last

        def fetchall(self):
            return getattr(self, "_last_all", [])

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

        def commit(self):
            return None

    monkeypatch.setattr(app_module, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.line_routes, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.admin_routes, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.database, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.queue_service, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module, "is_accepting_new", lambda *args, **kwargs: True)
    monkeypatch.setattr(app_module.line_routes, "is_accepting_new", lambda *args, **kwargs: True)
    monkeypatch.setattr(app_module.admin_routes, "is_accepting_new", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        app_module,
        "refresh_wait_time_estimate",
        lambda now=None, owner_admin_id=None: {
            "message": "現在の目安待ち時間: 6分",
            "estimated_seconds": 360,
        },
    )
    monkeypatch.setattr(
        app_module.line_routes,
        "refresh_wait_time_estimate",
        lambda now=None, owner_admin_id=None: {
            "message": "現在の目安待ち時間: 6分",
            "estimated_seconds": 360,
        },
    )

    sent_texts = []
    monkeypatch.setattr(
        app_module,
        "send_flex_notice",
        lambda *args, **kwargs: sent_texts.append(
            args[2] if len(args) > 2 else kwargs.get("body")
        ),
    )
    monkeypatch.setattr(
        app_module.line_routes,
        "send_flex_notice",
        lambda *args, **kwargs: sent_texts.append(
            args[2] if len(args) > 2 else kwargs.get("body")
        ),
    )

    event = SimpleNamespace(reply_token="reply-token")
    app_module.process_reservation(event, "U-123", "予約 相談")

    insert_queries = [
        params
        for query, params in queries
        if "INSERT INTO reservations" in query
    ]
    assert len(insert_queries) == 1
    inserted = insert_queries[0]
    # (user_id, message, type_id, owner_admin_id, reservation_no) の 5 要素
    assert inserted[0] == "U-123"
    assert inserted[1] == ""
    assert inserted[2] == 1
    assert inserted[3] == 7  # type_owner_admin_id
    # reservation_no は XXXYZA 形式（001000〜999999）
    assert isinstance(inserted[4], int)
    assert 1000 <= inserted[4] <= 999999
    assert sent_texts


def test_allocate_admin_reservation_no_generates_xxxyza_format(app_module, monkeypatch):
    class FakeCursor:
        def __init__(self):
            self.seq = 1

        def execute(self, query, params=None):
            if "SELECT next_reservation_no" in query:
                pass
            elif "SELECT 1 FROM reservations" in query:
                pass
            elif "UPDATE admin_accounts" in query:
                pass

        def fetchone(self):
            if self.seq == 1:
                self.seq += 1
                return (1,)
            return None

    monkeypatch.setattr(app_module.queue_service, "get_management_no", lambda owner_admin_id=None: 0)
    cur = FakeCursor()
    res = app_module.allocate_admin_reservation_no(cur, owner_admin_id=1)
    # XXX=001, Y=?, Z=0, A=0 -> res >= 1000 and res <= 1990
    assert 1000 <= res <= 1990
    assert app_module.fmt_no(res).endswith("00")  # Z=0, A=0


def test_fmt_no_formats_as_six_digits(app_module):
    assert app_module.fmt_no(19) == "000019"
    assert app_module.fmt_no(123456) == "123456"
    assert app_module.fmt_no("42") == "000042"
    assert app_module.fmt_no(None) == "None"


def test_handle_message_ignores_specific_url(app_module, monkeypatch):
    called = []

    monkeypatch.setattr(
        app_module,
        "process_reservation",
        lambda *args, **kwargs: called.append((args, kwargs)),
    )
    monkeypatch.setattr(
        app_module.line_routes,
        "process_reservation",
        lambda *args, **kwargs: called.append((args, kwargs)),
    )

    event = SimpleNamespace(
        message=SimpleNamespace(text="https://ukweb.ikura.workers.dev/"),
        source=SimpleNamespace(user_id="U-ignore"),
        reply_token="reply-token",
    )

    app_module.handle_message(event)

    assert called == []


def test_handle_message_ignores_usage_message(app_module, monkeypatch):
    called = []

    monkeypatch.setattr(
        app_module,
        "process_reservation",
        lambda *args, **kwargs: called.append((args, kwargs)),
    )
    monkeypatch.setattr(
        app_module.line_routes,
        "process_reservation",
        lambda *args, **kwargs: called.append((args, kwargs)),
    )

    event = SimpleNamespace(
        message=SimpleNamespace(text="使い方"),
        source=SimpleNamespace(user_id="U-ignore"),
        reply_token="reply-token",
    )

    app_module.handle_message(event)

    assert called == []


def test_admin_call_push_failure_returns_to_admin_page(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "is_admin_authenticated", lambda: True)
    monkeypatch.setattr(app_module.admin_routes, "is_admin_authenticated", lambda: True)
    monkeypatch.setattr(app_module, "get_current_admin_account_id", lambda: 1)
    monkeypatch.setattr(app_module.admin_routes, "get_current_admin_account_id", lambda: 1)
    monkeypatch.setattr(
        app_module, "send_push_message", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("push fail"))
    )

    class FakeCursor:
        rowcount = 1

        def __init__(self):
            self.calls = []
            self._last = None

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            self.calls.append((" ".join(query.split()), params))
            if "RETURNING user_id, COALESCE(reservation_no, r.id)" in query:
                self._last = ("U-123", 28)
            elif "SELECT status, COALESCE(reservation_no, id)" in query:
                self._last = ("waiting", 28)
            elif "UPDATE reservations SET status = %s, called_at = NULL, call_origin = NULL" in query:
                self.rowcount = 1

        def fetchone(self):
            return self._last

    class FakeConnection:
        def __init__(self):
            self.cursor_obj = FakeCursor()
            self.committed = False

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return self.cursor_obj

        def commit(self):
            self.committed = True

    monkeypatch.setattr(app_module, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.admin_routes, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.database, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.queue_service, "get_connection", lambda: FakeConnection())

    with app_module.app.test_request_context("/admin/call/28", method="POST"):
        response = app_module.admin_call(28)

    assert response.status_code == 302
    assert "call_error=" in response.headers["Location"]


def test_ensure_types_table_adds_type_foreign_key(app_module, monkeypatch):
    queries = []

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            queries.append((query, params))

        def fetchone(self):
            return None

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

        def commit(self):
            return None

    monkeypatch.setattr(app_module, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.admin_routes, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.database, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.queue_service, "get_connection", lambda: FakeConnection())

    app_module.ensure_types_table()

    normalized_queries = [" ".join(query.split()) for query, _ in queries]
    assert any(
        "ADD CONSTRAINT fk_reservations_type_id" in query
        for query in normalized_queries
    )
    assert any(
        "ALTER TABLE reservation_types ADD COLUMN IF NOT EXISTS image_data BYTEA"
        in query
        for query in normalized_queries
    )
    assert any(
        "ALTER TABLE reservation_types ADD COLUMN IF NOT EXISTS image_mime_type TEXT NOT NULL DEFAULT ''"
        in query
        for query in normalized_queries
    )
    assert any(
        "ALTER TABLE reservation_types ADD COLUMN IF NOT EXISTS image_filename TEXT NOT NULL DEFAULT ''"
        in query
        for query in normalized_queries
    )
    assert any("ON DELETE RESTRICT" in query for query in normalized_queries)


def test_format_duration_from_seconds(app_module):
    assert app_module.format_duration_from_seconds(None) == ""
    assert app_module.format_duration_from_seconds(5) == "5秒"
    assert app_module.format_duration_from_seconds(61) == "1分1秒"
    assert app_module.format_duration_from_seconds(3661) == "1時間1分"


def test_format_duration_from_seconds_negative(app_module):
    assert app_module.format_duration_from_seconds(-3) == "0秒"


def test_format_dt_with_naive_datetime(app_module):
    value = datetime(2026, 4, 16, 0, 0, 0)
    assert app_module.format_dt(value) == "04-16 09:00"


def test_format_dt_with_aware_datetime(app_module):
    utc = datetime(2026, 4, 16, 0, 0, 0, tzinfo=ZoneInfo("UTC"))
    assert app_module.format_dt(utc) == "04-16 09:00"


def test_should_run_call_batch(app_module):
    assert app_module.should_run_call_batch(SimpleNamespace(tm_min=10)) is True
    assert app_module.should_run_call_batch(SimpleNamespace(tm_min=11)) is False


def test_send_push_message_uses_retry_key(app_module, monkeypatch):
    captured = []

    class DummyApiClient:
        def __init__(self, _config):
            return None

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class DummyMessagingApi:
        def __init__(self, _api_client):
            return None

        def push_message(self, request_payload, x_line_retry_key=None):
            captured.append(
                (request_payload.to, request_payload.messages[0].text, x_line_retry_key)
            )
            return None

    monkeypatch.setattr(app_module.line_service, "ApiClient", DummyApiClient)
    monkeypatch.setattr(app_module.line_service, "MessagingApi", DummyMessagingApi)
    app_module.send_push_message("U1", "hello", retry_key="retry-key-1")
    assert captured == [("U1", "hello", "retry-key-1")]


def test_send_push_message_retries_with_same_key(app_module, monkeypatch):
    attempt_keys = []

    class RetryableError(Exception):
        status = 500

    class DummyApiClient:
        def __init__(self, _config):
            return None

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class DummyMessagingApi:
        calls = 0

        def __init__(self, _api_client):
            return None

        def push_message(self, _request_payload, x_line_retry_key=None):
            DummyMessagingApi.calls += 1
            attempt_keys.append(x_line_retry_key)
            if DummyMessagingApi.calls == 1:
                raise RetryableError("temporary failure")
            return None

    monkeypatch.setattr(app_module.line_service, "ApiClient", DummyApiClient)
    monkeypatch.setattr(app_module.line_service, "MessagingApi", DummyMessagingApi)
    monkeypatch.setattr(app_module, "LINE_PUSH_MAX_RETRIES", 2)
    monkeypatch.setattr(app_module.time, "sleep", lambda _secs: None)
    app_module.send_push_message("U2", "hello", retry_key="retry-fixed")
    assert attempt_keys == ["retry-fixed", "retry-fixed"]


def test_send_push_message_treats_409_as_success(app_module, monkeypatch):
    class ConflictError(Exception):
        status = 409

    class DummyApiClient:
        def __init__(self, _config):
            return None

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class DummyMessagingApi:
        def __init__(self, _api_client):
            return None

        def push_message(self, _request_payload, x_line_retry_key=None):
            raise ConflictError("already accepted")

    monkeypatch.setattr(app_module.line_service, "ApiClient", DummyApiClient)
    monkeypatch.setattr(app_module.line_service, "MessagingApi", DummyMessagingApi)
    # 409は受理済み扱いで例外を送出しない
    app_module.send_push_message("U3", "hello", retry_key="retry-409")


def test_get_messaging_api_reuses_single_client(app_module, monkeypatch):
    created = []

    class DummyApiClient:
        def __init__(self, _config):
            created.append("client")

    class DummyMessagingApi:
        def __init__(self, api_client):
            self.api_client = api_client

    monkeypatch.setattr(app_module.line_service, "_MESSAGING_API", None)
    monkeypatch.setattr(app_module.line_service, "ApiClient", DummyApiClient)
    monkeypatch.setattr(app_module.line_service, "MessagingApi", DummyMessagingApi)

    first = app_module.line_service.get_messaging_api()
    second = app_module.line_service.get_messaging_api()

    assert first is second
    assert created == ["client"]


def test_normalize_and_validate_type_name(app_module):
    assert app_module.normalize_type_name("  A   B  ") == "A B"
    assert app_module.validate_type_name("相談") is True
    assert app_module.validate_type_name("") is False


def test_validate_type_name_length_boundary(app_module):
    max_len_name = "A" * app_module.MAX_TYPE_NAME_LENGTH
    over_name = "A" * (app_module.MAX_TYPE_NAME_LENGTH + 1)
    assert app_module.validate_type_name(max_len_name) is True
    assert app_module.validate_type_name(over_name) is False


def test_validate_type_name_invalid_chars(app_module):
    assert app_module.validate_type_name("<script>") is False


def test_build_auto_call_summary_empty(app_module):
    summary = app_module.build_auto_call_summary({}, "last")
    assert summary["run_at"] == ""
    assert "まだ自動呼出は実行されていません" in summary["message"]


def test_build_auto_call_summary_with_values(app_module):
    values = {
        "last_auto_call_run_at": "04-16 10:00",
        "last_auto_call_sent_count": "2",
        "last_auto_call_failed_count": "1",
        "last_auto_call_selected_count": "3",
    }
    summary = app_module.build_auto_call_summary(values, "last")
    assert summary["run_at"] == "04-16 10:00"
    assert summary["sent_count"] == 2
    assert summary["failed_count"] == 1
    assert summary["selected_count"] == 3


def test_get_latest_wait_time_summary_empty(app_module):
    summary = app_module.get_latest_wait_time_summary({})
    assert summary["run_at"] == ""
    assert "算出中" in summary["message"]


def test_get_latest_wait_time_summary_with_values(app_module):
    values = {
        "last_wait_time_run_at": "04-17 10:00",
        "last_wait_time_estimated_seconds": "420",
        "last_wait_time_waiting_count": "3",
        "last_wait_time_avg_service_seconds": "140",
    }
    summary = app_module.get_latest_wait_time_summary(values)
    assert summary["run_at"] == "04-17 10:00"
    assert summary["estimated_seconds"] == 420
    assert summary["waiting_count"] == 3
    assert summary["avg_service_seconds"] == 140
    assert "7分" in summary["message"]


def test_calculate_wait_time_minutes_formula(app_module):
    assert app_module.calculate_wait_time_minutes(0) == 2
    assert app_module.calculate_wait_time_minutes(1) == 3
    assert app_module.calculate_wait_time_minutes(2) == 3
    assert app_module.calculate_wait_time_minutes(3) == 4


@pytest.mark.parametrize(
    ("people_ahead", "expected_minutes"),
    [
        (0, 2),
        (1, 3),
        (2, 3),
        (3, 4),
        (4, 4),
        (5, 5),
        (6, 5),
    ],
)
def test_calculate_wait_time_minutes_boundaries(
    app_module, people_ahead, expected_minutes
):
    assert app_module.calculate_wait_time_minutes(people_ahead) == expected_minutes


def test_verify_admin_password_rejects_empty_and_invalid_values(app_module, monkeypatch):
    monkeypatch.setattr(app_module.auth, "check_password_hash", lambda _hash, _candidate: True)
    assert app_module.verify_admin_password("secret") is True
    assert app_module.verify_admin_password("") is False
    monkeypatch.setattr(app_module.auth, "check_password_hash", lambda _hash, _candidate: False)
    assert app_module.verify_admin_password("secret") is False


def test_authenticate_admin_account_normalizes_login_id(app_module, monkeypatch):
    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, *args, **kwargs):
            pass

        def fetchone(self):
            return (1, "ADMIN", "hash", app_module.ROLE_ADMIN)

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

    monkeypatch.setattr(app_module.auth, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.auth, "check_password_hash", lambda _hash, _candidate: True)

    result = app_module.authenticate_admin_account(" ADMIN ", "secret")
    assert result == {"id": 1, "login_id": "ADMIN", "role": app_module.ROLE_ADMIN}


def test_validate_batch_runner_token_authorization_header(app_module):
    app_module.auth.BATCH_CALL_RUNNER_TOKEN = "token123"
    with app_module.app.test_request_context(
        "/tasks/process-call-queue", headers={"Authorization": "Bearer token123"}
    ):
        assert app_module.validate_batch_runner_token() is True


def test_validate_batch_runner_token_custom_header(app_module):
    app_module.auth.BATCH_CALL_RUNNER_TOKEN = "token123"
    with app_module.app.test_request_context(
        "/tasks/process-call-queue", headers={"X-Task-Token": "token123"}
    ):
        assert app_module.validate_batch_runner_token() is True


def test_validate_batch_runner_token_missing(app_module):
    app_module.auth.BATCH_CALL_RUNNER_TOKEN = ""
    with app_module.app.test_request_context("/tasks/process-call-queue"):
        assert app_module.validate_batch_runner_token() is False


def test_get_csrf_token_generates_and_reuses(app_module):
    with app_module.app.test_request_context("/"):
        first = app_module.get_csrf_token()
        second = app_module.get_csrf_token()
        assert first == second
        assert isinstance(first, str)
        assert len(first) > 20


def test_start_admin_session_preserves_existing_csrf_token(app_module):
    with app_module.app.test_request_context("/login"):
        app_module.session["_csrf_token"] = "shared-token"
        app_module.start_admin_session(app_module.ROLE_ADMIN, 1, "admin")

        assert app_module.session["_csrf_token"] == "shared-token"
        assert app_module.session["logged_in"] is True
        assert app_module.session["admin_role"] == app_module.ROLE_ADMIN


def test_validate_csrf_success(app_module):
    with app_module.app.test_request_context(
        "/dummy", method="POST", data={"_csrf_token": "abc"}
    ):
        app_module.session["_csrf_token"] = "abc"
        app_module.validate_csrf()


def test_validate_csrf_failure(app_module):
    with app_module.app.test_request_context(
        "/dummy", method="POST", data={"_csrf_token": "wrong"}
    ):
        app_module.session["_csrf_token"] = "abc"
        with pytest.raises(Forbidden):
            app_module.validate_csrf()


def test_csrf_protect_redirects_to_login_when_token_is_invalid(
    app_module, monkeypatch
):
    with app_module.app.test_request_context(
        "/admin/toggle-accepting", method="POST", data={"_csrf_token": "wrong"}
    ):
        app_module.session["logged_in"] = True
        app_module.session["admin_role"] = app_module.ROLE_ADMIN
        app_module.session["admin_account_id"] = 1
        app_module.session["last_activity"] = 1000.0
        app_module.session["_csrf_token"] = "expected-token"
        monkeypatch.setattr(app_module.time, "time", lambda: 1005.0)

        response = app_module.csrf_protect()

        assert response.status_code == 302
        assert "/login" in response.headers["Location"]
        assert "next=/admin/toggle-accepting" in response.headers["Location"]
        assert "notice=session_expired" in response.headers["Location"]


def test_admin_post_without_active_session_redirects_to_login(
    client, app_module, monkeypatch
):
    monkeypatch.setattr(app_module, "set_accepting_new", lambda *args, **kwargs: None)
    monkeypatch.setattr(app_module.admin_routes, "set_accepting_new", lambda *args, **kwargs: None)

    response = client.post("/admin/toggle-accepting")

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/login")


def test_admin_post_with_active_session_still_rejects_invalid_csrf(
    app_module, monkeypatch
):
    with app_module.app.test_request_context(
        "/admin/toggle-accepting", method="POST", data={"_csrf_token": "wrong"}
    ):
        app_module.session["logged_in"] = True
        app_module.session["admin_role"] = app_module.ROLE_ADMIN
        app_module.session["admin_account_id"] = 1
        app_module.session["last_activity"] = 1000.0
        app_module.session["_csrf_token"] = "expected-token"
        monkeypatch.setattr(app_module.time, "time", lambda: 1005.0)

        response = app_module.csrf_protect()

        assert response.status_code == 302
        assert response.headers["Location"].startswith("/login")


def test_is_authenticated_as_success(app_module, monkeypatch):
    with app_module.app.test_request_context("/"):
        now = 1000.0
        app_module.session["logged_in"] = True
        app_module.session["admin_role"] = app_module.ROLE_ADMIN
        app_module.session["admin_account_id"] = 1
        app_module.session["last_activity"] = now
        app_module.session["_csrf_token"] = "token"
        monkeypatch.setattr(
            app_module.auth,
            "get_admin_account_by_id",
            lambda _account_id: {
                "id": 1,
                "login_id": "admin",
                "role": app_module.ROLE_ADMIN,
                "active": True,
            },
        )
        monkeypatch.setattr(app_module.time, "time", lambda: now + 10)
        assert app_module.is_authenticated_as(app_module.ROLE_ADMIN) is True


def test_is_authenticated_as_timeout_clears_session(app_module, monkeypatch):
    with app_module.app.test_request_context("/"):
        now = 1000.0
        app_module.session["logged_in"] = True
        app_module.session["admin_role"] = app_module.ROLE_ADMIN
        app_module.session["admin_account_id"] = 1
        app_module.session["last_activity"] = now
        monkeypatch.setattr(
            app_module.auth,
            "get_admin_account_by_id",
            lambda _account_id: {
                "id": 1,
                "login_id": "admin",
                "role": app_module.ROLE_ADMIN,
                "active": True,
            },
        )
        monkeypatch.setattr(
            app_module.time,
            "time",
            lambda: now + app_module.SESSION_IDLE_TIMEOUT_SECONDS + 1,
        )
        assert app_module.is_authenticated_as(app_module.ROLE_ADMIN) is False
        assert app_module.session.get("logged_in") is None


def test_is_authenticated_as_inactive_account_clears_session(app_module, monkeypatch):
    with app_module.app.test_request_context("/"):
        now = 1000.0
        app_module.session["logged_in"] = True
        app_module.session["admin_role"] = app_module.ROLE_ADMIN
        app_module.session["admin_account_id"] = 1
        app_module.session["last_activity"] = now
        monkeypatch.setattr(
            app_module.auth,
            "get_admin_account_by_id",
            lambda _account_id: {
                "id": 1,
                "login_id": "admin",
                "role": app_module.ROLE_ADMIN,
                "active": False,
            },
        )
        monkeypatch.setattr(app_module.time, "time", lambda: now + 10)
        assert app_module.is_authenticated_as(app_module.ROLE_ADMIN) is False
        assert app_module.session.get("logged_in") is None


def test_apply_security_headers_admin_page(app_module):
    app_module.FORCE_HTTPS = True
    with app_module.app.test_request_context(
        "/admin", headers={"X-Forwarded-Proto": "https"}
    ):
        response = app_module.app.response_class("ok")
        result = app_module.apply_security_headers(response)
        assert "Content-Security-Policy" in result.headers
        assert result.headers.get("X-Frame-Options") == "DENY"
        assert "Strict-Transport-Security" in result.headers
        assert "no-store" in result.headers.get("Cache-Control", "")


def test_is_login_rate_limited_on_exception_returns_true(app_module, monkeypatch):
    monkeypatch.setattr(
        app_module.database,
        "get_connection",
        lambda: (_ for _ in ()).throw(RuntimeError("db error")),
    )
    assert app_module.is_login_rate_limited("127.0.0.1") is True


def test_record_login_failure_on_exception_does_not_raise(app_module, monkeypatch):
    monkeypatch.setattr(
        app_module.database,
        "get_connection",
        lambda: (_ for _ in ()).throw(RuntimeError("db error")),
    )
    app_module.record_login_failure("127.0.0.1")


def test_is_webhook_rate_limited_on_exception_returns_true(app_module, monkeypatch):
    monkeypatch.setattr(
        app_module.database,
        "get_connection",
        lambda: (_ for _ in ()).throw(RuntimeError("db error")),
    )
    assert app_module.is_webhook_rate_limited("127.0.0.1") is True


def test_is_webhook_rate_limited_uses_redis_when_configured(app_module, monkeypatch):
    class FakeRedis:
        def eval(self, script, numkeys, key, window_seconds):
            assert "INCR" in script
            assert numkeys == 1
            assert key == "webhook:rate:127.0.0.1"
            assert window_seconds == app_module.WEBHOOK_RATE_LIMIT_WINDOW_SECONDS
            return app_module.WEBHOOK_RATE_LIMIT_COUNT + 1

    monkeypatch.setattr(app_module.database, "REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setattr(app_module.database, "_REDIS_CLIENT", FakeRedis())
    monkeypatch.setattr(
        app_module.database,
        "get_connection",
        lambda: (_ for _ in ()).throw(AssertionError("PostgreSQL must not be used")),
    )

    assert app_module.is_webhook_rate_limited("127.0.0.1") is True


def test_login_get_ok(client):
    response = client.get("/login")
    assert response.status_code == 200
    text = response.get_data(as_text=True)
    assert 'id="login-form"' in text
    assert 'novalidate' in text
    assert 'id="login-submit"' in text
    assert 'autocomplete="username"' in text
    assert 'autocomplete="current-password"' in text


def test_login_post_admin_success_redirect(client, csrf_token, app_module, monkeypatch):
    monkeypatch.setattr(app_module, "is_login_rate_limited", lambda _ip: False)
    monkeypatch.setattr(
        app_module,
        "authenticate_admin_account",
        lambda _login_id, _pwd: {
            "id": 1,
            "login_id": "admin",
            "role": app_module.ROLE_ADMIN,
        },
    )
    monkeypatch.setattr(app_module, "record_admin_login", lambda *args, **kwargs: None)

    response = client.post(
        "/login",
        data={
            "login_id": "admin",
            "password": "admin-pass",
            "_csrf_token": csrf_token,
            "next": "/admin/types",
        },
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/admin/types")


def test_login_post_audit_success_redirect(client, csrf_token, app_module, monkeypatch):
    monkeypatch.setattr(app_module, "is_login_rate_limited", lambda _ip: False)
    monkeypatch.setattr(
        app_module,
        "authenticate_admin_account",
        lambda _login_id, _pwd: {
            "id": 2,
            "login_id": "audit",
            "role": app_module.ROLE_AUDIT_ADMIN,
        },
    )
    monkeypatch.setattr(app_module, "record_admin_login", lambda *args, **kwargs: None)

    response = client.post(
        "/login",
        data={
            "login_id": "audit",
            "password": "audit-pass",
            "_csrf_token": csrf_token,
            "next": "/admin/login-logs",
        },
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/admin/login-logs")


def test_login_post_failure_shows_error(client, csrf_token, app_module, monkeypatch):
    monkeypatch.setattr(app_module, "is_login_rate_limited", lambda _ip: False)
    monkeypatch.setattr(
        app_module, "authenticate_admin_account", lambda _login_id, _pwd: None
    )
    monkeypatch.setattr(app_module, "record_admin_login", lambda *args, **kwargs: None)
    monkeypatch.setattr(app_module, "record_login_failure", lambda _ip: None)

    response = client.post(
        "/login",
        data={"login_id": "admin", "password": "wrong", "_csrf_token": csrf_token},
    )
    assert response.status_code == 200
    assert "ログインIDまたはパスワードが正しくありません" in response.get_data(as_text=True)


def test_login_post_rate_limited(client, csrf_token, app_module, monkeypatch):
    monkeypatch.setattr(app_module, "is_login_rate_limited", lambda _ip: True)
    response = client.post(
        "/login",
        data={"login_id": "admin", "password": "x", "_csrf_token": csrf_token},
    )
    assert response.status_code == 429


def test_admin_page_shows_version_badge(client, app_module, monkeypatch):
    monkeypatch.setattr(app_module, "is_admin_authenticated", lambda: True)
    monkeypatch.setattr(app_module.admin_routes, "is_admin_authenticated", lambda: True)
    monkeypatch.setattr(app_module, "get_current_admin_account_id", lambda: 1)
    monkeypatch.setattr(app_module.admin_routes, "get_current_admin_account_id", lambda: 1)
    monkeypatch.setattr(
        app_module,
        "get_runtime_settings",
        lambda *args, **kwargs: {
            "accepting_new": True,
            "auto_call_count": 0,
            "last_auto_call": {},
            "latest_auto_call": {},
        },
    )
    monkeypatch.setattr(
        app_module.admin_routes,
        "get_runtime_settings",
        lambda *args, **kwargs: {
            "accepting_new": True,
            "auto_call_count": 0,
            "last_auto_call": {},
            "latest_auto_call": {},
        },
    )

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            normalized_query = " ".join(query.split())
            if normalized_query.startswith(
                "SELECT t.id, t.name, t.accepting, t.flavor_text, t.image_mime_type, t.image_filename, t.price, a.login_id FROM reservation_types t LEFT JOIN admin_accounts a ON a.id = t.owner_admin_id WHERE t.owner_admin_id = %s ORDER BY t.id ASC"
            ):
                self._rows = []
            elif (
                normalized_query.startswith("SELECT")
                and "FROM reservations" in normalized_query
            ):
                self._rows = []
            else:
                self._rows = []

        def fetchone(self):
            return None

        def fetchall(self):
            return getattr(self, "_rows", [])

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

    monkeypatch.setattr(app_module, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.admin_routes, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.database, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.queue_service, "get_connection", lambda: FakeConnection())
    with client.session_transaction() as session:
        session["logged_in"] = True
        session["admin_role"] = "admin"
        session["admin_account_id"] = 1
        session["admin_login_id"] = "manager01"
        session["issued_at"] = 1
        session["last_activity"] = 1

    response = client.get("/admin")
    assert response.status_code == 200


def test_types_page_shows_version_badge(client, app_module, monkeypatch):
    monkeypatch.setattr(app_module, "is_admin_authenticated", lambda: True)
    monkeypatch.setattr(app_module.admin_routes, "is_admin_authenticated", lambda: True)
    monkeypatch.setattr(app_module, "get_current_admin_account_id", lambda: 1)
    monkeypatch.setattr(app_module.admin_routes, "get_current_admin_account_id", lambda: 1)
    monkeypatch.setattr(app_module, "is_accepting_new", lambda *args, **kwargs: True)
    monkeypatch.setattr(app_module.admin_routes, "is_accepting_new", lambda *args, **kwargs: True)

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            self._rows = []

        def fetchall(self):
            return []

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

    monkeypatch.setattr(app_module, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.admin_routes, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.database, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.queue_service, "get_connection", lambda: FakeConnection())
    with client.session_transaction() as session:
        session["logged_in"] = True
        session["admin_role"] = "admin"
        session["admin_account_id"] = 1
        session["admin_login_id"] = "manager01"
        session["issued_at"] = 1
        session["last_activity"] = 1

    response = client.get("/admin/types")
    assert response.status_code == 200


def test_admin_types_update_name_changes_type_name(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "is_admin_authenticated", lambda: True)
    monkeypatch.setattr(app_module.admin_routes, "is_admin_authenticated", lambda: True)
    monkeypatch.setattr(app_module, "get_current_admin_account_id", lambda: 1)
    monkeypatch.setattr(app_module.admin_routes, "get_current_admin_account_id", lambda: 1)

    executed = []

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            executed.append((" ".join(query.split()), params))
            if query.strip().startswith("UPDATE reservation_types SET name = %s"):
                self.rowcount = 1
            else:
                raise AssertionError(f"Unexpected query: {query}")

        def fetchone(self):
            return None

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

        def commit(self):
            return None

    monkeypatch.setattr(app_module, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.admin_routes, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.database, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.queue_service, "get_connection", lambda: FakeConnection())

    with app_module.app.test_request_context(
        "/admin/types/7/name",
        method="POST",
        data={"name": "新メニュー"},
    ):
        response = app_module.admin_types_update_name(7)

    assert response.status_code == 302
    assert any(params and params[0] == "新メニュー" for _, params in executed)


def test_admin_types_delete_blocks_types_with_reservations(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "is_admin_authenticated", lambda: True)
    monkeypatch.setattr(app_module.admin_routes, "is_admin_authenticated", lambda: True)
    monkeypatch.setattr(app_module, "get_current_admin_account_id", lambda: 1)
    monkeypatch.setattr(app_module.admin_routes, "get_current_admin_account_id", lambda: 1)

    rollback_called = []

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            self._last = ("img/reservation_types/old.png",)
            if "DELETE FROM reservation_types" in query:
                raise app_module.psycopg2.IntegrityError("fk violation")

        def fetchone(self):
            return getattr(self, "_last", None)

        @property
        def rowcount(self):
            return 1

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

        def rollback(self):
            rollback_called.append(True)

        def commit(self):
            return None

    monkeypatch.setattr(app_module, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.admin_routes, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.database, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.queue_service, "get_connection", lambda: FakeConnection())

    with app_module.app.test_request_context("/admin/types/delete/1", method="POST"):
        response = app_module.admin_types_delete(1)

    assert response.status_code == 302
    assert "type_error=" in response.headers["Location"]


def test_admin_types_update_image_replaces_existing_file(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "is_admin_authenticated", lambda: True)
    monkeypatch.setattr(app_module.admin_routes, "is_admin_authenticated", lambda: True)
    monkeypatch.setattr(app_module, "get_current_admin_account_id", lambda: 1)
    monkeypatch.setattr(app_module.admin_routes, "get_current_admin_account_id", lambda: 1)

    deleted_paths = []
    saved_files = []

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            if "SELECT image_data, image_mime_type, image_filename FROM reservation_types" in query:
                self._last = (b"old-bytes", "image/png", "old.png")
            elif "UPDATE reservation_types" in query and "SET image_data = %s" in query:
                self.rowcount = 1
            else:
                raise AssertionError(f"Unexpected query: {query}")

        def fetchone(self):
            return getattr(self, "_last", None)

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

        def commit(self):
            return None

    monkeypatch.setattr(app_module, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.admin_routes, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.database, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.queue_service, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(
        app_module,
        "save_type_image_upload",
        lambda image_file: saved_files.append(image_file.filename)
        or (b"new-bytes", "image/png", "new.png"),
    )
    monkeypatch.setattr(
        app_module.admin_routes,
        "save_type_image_upload",
        lambda image_file: saved_files.append(image_file.filename)
        or (b"new-bytes", "image/png", "new.png"),
    )

    with app_module.app.test_request_context(
        "/admin/types/7/image",
        method="POST",
        data={"image": (BytesIO(b"fake-image"), "new.png")},
        content_type="multipart/form-data",
    ):
        response = app_module.admin_types_update_image(7)

    assert response.status_code == 302
    assert saved_files == ["new.png"]


def test_save_type_image_upload_resizes_and_compresses_jpeg(app_module):
    from PIL import Image

    image = Image.new("RGB", (4000, 3000), color="red")
    buf = BytesIO()
    image.save(buf, format="JPEG", quality=95)
    buf.seek(0)
    buf.filename = "large.jpg"
    buf.mimetype = "image/jpeg"

    data, mimetype, filename = app_module.save_type_image_upload(buf)

    assert mimetype == "image/jpeg"
    assert filename.endswith(".jpg")
    assert len(data) < len(buf.getvalue())
    with Image.open(BytesIO(data)) as saved:
        assert saved.width <= 1920
        assert saved.height <= 1080


def test_save_type_image_upload_normalizes_webp_to_flex_safe_extension(app_module):
    from PIL import Image

    image = Image.new("RGB", (1280, 720), color="blue")
    buf = BytesIO()
    image.save(buf, format="WEBP")
    buf.seek(0)
    buf.filename = "sample.webp"
    buf.mimetype = "image/webp"

    data, mimetype, filename = app_module.save_type_image_upload(buf)

    assert mimetype == "image/jpeg"
    assert filename.endswith(".jpg")
    with Image.open(BytesIO(data)) as saved:
        assert saved.width == 1280
        assert saved.height == 720


def test_save_type_image_upload_preserves_png_transparency(app_module):
    from PIL import Image

    image = Image.new("RGBA", (2000, 1000), color=(0, 128, 255, 64))
    buf = BytesIO()
    image.save(buf, format="PNG")
    buf.seek(0)
    buf.filename = "transparent.png"
    buf.mimetype = "image/png"

    data, mimetype, filename = app_module.save_type_image_upload(buf)

    assert mimetype == "image/png"
    assert filename.endswith(".png")
    with Image.open(BytesIO(data)) as saved:
        assert saved.width <= 1920
        assert saved.height <= 1080
        assert saved.mode in {"RGBA", "LA", "P"}


def test_static_assets_do_not_set_cookie(app_module, client):
    client.get("/login")

    response = client.get("/static/js/admin.js")
    assert response.status_code == 200
    assert "Set-Cookie" not in response.headers

    response = client.get("/static/js/nonexistent.js")
    assert "Set-Cookie" not in response.headers

    response = client.get("/favicon.ico")
    assert response.status_code in (200, 204)
    assert "Set-Cookie" not in response.headers

    response = client.get("/robots.txt")
    assert "Set-Cookie" not in response.headers


def test_admin_accounts_create_requires_audit_auth(app_module):
    with app_module.app.test_request_context(
        "/admin/admin-accounts",
        method="POST",
        data={"login_id": "manager01", "password": "superpower"},
    ):
        response = app_module.admin_accounts_create()
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/login")


def test_admin_accounts_create_success(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "is_audit_admin_authenticated", lambda: True)
    monkeypatch.setattr(app_module.admin_routes, "is_audit_admin_authenticated", lambda: True)

    calls = []

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            calls.append((query, params))

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

        def commit(self):
            return None

    monkeypatch.setattr(app_module, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.admin_routes, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.database, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.queue_service, "get_connection", lambda: FakeConnection())

    with app_module.app.test_request_context(
        "/admin/admin-accounts",
        method="POST",
        data={"login_id": "manager01", "password": "superpower"},
    ):
        response = app_module.admin_accounts_create()

    assert response.status_code == 302
    assert "account_success" in response.headers["Location"]
    assert calls


def test_admin_accounts_create_duplicate_login_id(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "is_audit_admin_authenticated", lambda: True)
    monkeypatch.setattr(app_module.admin_routes, "is_audit_admin_authenticated", lambda: True)

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, _query, _params=None):
            raise app_module.psycopg2.IntegrityError()

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

        def commit(self):
            return None

    monkeypatch.setattr(app_module, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.admin_routes, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.database, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.queue_service, "get_connection", lambda: FakeConnection())

    with app_module.app.test_request_context(
        "/admin/admin-accounts",
        method="POST",
        data={"login_id": "manager01", "password": "superpower"},
    ):
        response = app_module.admin_accounts_create()

    assert response.status_code == 302
    assert "account_error" in response.headers["Location"]


def test_admin_accounts_bulk_create_success(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "is_audit_admin_authenticated", lambda: True)
    monkeypatch.setattr(app_module.admin_routes, "is_audit_admin_authenticated", lambda: True)

    calls = []

    class FakeCursor:
        def __init__(self):
            self._rows = []

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            calls.append((query, params))
            if "SELECT login_id FROM admin_accounts" in query:
                self._rows = []

        def fetchall(self):
            return self._rows

        def fetchone(self):
            return None

        def fetchone(self):
            return None

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

        def commit(self):
            return None

    monkeypatch.setattr(app_module, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.admin_routes, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.database, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.queue_service, "get_connection", lambda: FakeConnection())

    with app_module.app.test_request_context(
        "/admin/admin-accounts",
        method="POST",
        data={
            "login_id": "",
            "password": "",
            "bulk_accounts": "manager01,superpower01\nmanager02,superpower02",
        },
    ):
        response = app_module.admin_accounts_create()

    assert response.status_code == 302
    assert "account_success" in response.headers["Location"]
    assert any("SELECT login_id FROM admin_accounts" in query for query, _ in calls)


def test_admin_accounts_bulk_create_invalid_line_format(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "is_audit_admin_authenticated", lambda: True)
    monkeypatch.setattr(app_module.admin_routes, "is_audit_admin_authenticated", lambda: True)

    with app_module.app.test_request_context(
        "/admin/admin-accounts",
        method="POST",
        data={
            "login_id": "",
            "password": "",
            "bulk_accounts": "manager01-superpower01",
        },
    ):
        response = app_module.admin_accounts_create()

    assert response.status_code == 302
    assert "account_error" in response.headers["Location"]


def test_admin_accounts_update_login_id_requires_audit_auth(app_module):
    with app_module.app.test_request_context(
        "/admin/admin-accounts/1/login-id",
        method="POST",
        data={"login_id": "manager02"},
    ):
        response = app_module.admin_accounts_update_login_id(1)
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/login")


def test_admin_accounts_update_login_id_success(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "is_audit_admin_authenticated", lambda: True)
    monkeypatch.setattr(app_module.admin_routes, "is_audit_admin_authenticated", lambda: True)

    calls = []

    class FakeCursor:
        rowcount = 1

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            calls.append((query, params))

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

        def commit(self):
            return None

    monkeypatch.setattr(app_module, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.admin_routes, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.database, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.queue_service, "get_connection", lambda: FakeConnection())

    with app_module.app.test_request_context(
        "/admin/admin-accounts/1/login-id",
        method="POST",
        data={"login_id": "manager02"},
    ):
        response = app_module.admin_accounts_update_login_id(1)

    assert response.status_code == 302
    assert "account_success" in response.headers["Location"]
    assert calls


def test_admin_accounts_update_login_id_duplicate(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "is_audit_admin_authenticated", lambda: True)
    monkeypatch.setattr(app_module.admin_routes, "is_audit_admin_authenticated", lambda: True)

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, _query, _params=None):
            raise app_module.psycopg2.IntegrityError()

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

        def commit(self):
            return None

    monkeypatch.setattr(app_module, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.admin_routes, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.database, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.queue_service, "get_connection", lambda: FakeConnection())

    with app_module.app.test_request_context(
        "/admin/admin-accounts/1/login-id",
        method="POST",
        data={"login_id": "manager02"},
    ):
        response = app_module.admin_accounts_update_login_id(1)

    assert response.status_code == 302
    assert "account_error" in response.headers["Location"]


def test_admin_accounts_toggle_active_success(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "is_audit_admin_authenticated", lambda: True)
    monkeypatch.setattr(app_module.admin_routes, "is_audit_admin_authenticated", lambda: True)
    monkeypatch.setattr(app_module, "get_current_admin_account_id", lambda: 9)
    monkeypatch.setattr(app_module.admin_routes, "get_current_admin_account_id", lambda: 9)
    monkeypatch.setattr(
        app_module,
        "get_admin_account_by_id",
        lambda account_id: {
            "id": account_id,
            "login_id": "manager01",
            "role": app_module.ROLE_ADMIN,
            "active": True,
        },
    )
    monkeypatch.setattr(
        app_module.admin_routes,
        "get_admin_account_by_id",
        lambda account_id: {
            "id": account_id,
            "login_id": "manager01",
            "role": app_module.ROLE_ADMIN,
            "active": True,
        },
    )
    monkeypatch.setattr(app_module, "get_active_admin_count", lambda role=None: 2)
    monkeypatch.setattr(app_module.admin_routes, "get_active_admin_count", lambda role=None: 2)

    calls = []

    class FakeCursor:
        rowcount = 1

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            calls.append((query, params))

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

        def commit(self):
            return None

    monkeypatch.setattr(app_module, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.admin_routes, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.database, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.queue_service, "get_connection", lambda: FakeConnection())

    with app_module.app.test_request_context(
        "/admin/admin-accounts/1/active",
        method="POST",
    ):
        response = app_module.admin_accounts_toggle_active(1)

    assert response.status_code == 302
    assert "account_success" in response.headers["Location"]
    assert any("UPDATE admin_accounts SET active = %s" in query for query, _ in calls)


def test_admin_accounts_delete_success(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "is_audit_admin_authenticated", lambda: True)
    monkeypatch.setattr(app_module.admin_routes, "is_audit_admin_authenticated", lambda: True)
    monkeypatch.setattr(app_module, "get_current_admin_account_id", lambda: 9)
    monkeypatch.setattr(app_module.admin_routes, "get_current_admin_account_id", lambda: 9)
    monkeypatch.setattr(
        app_module,
        "get_admin_account_by_id",
        lambda account_id: {
            "id": account_id,
            "login_id": "manager01",
            "role": app_module.ROLE_ADMIN,
            "active": True,
        },
    )
    monkeypatch.setattr(
        app_module.admin_routes,
        "get_admin_account_by_id",
        lambda account_id: {
            "id": account_id,
            "login_id": "manager01",
            "role": app_module.ROLE_ADMIN,
            "active": True,
        },
    )
    monkeypatch.setattr(app_module, "get_active_admin_count", lambda role=None: 2)
    monkeypatch.setattr(app_module.admin_routes, "get_active_admin_count", lambda role=None: 2)

    calls = []

    class FakeCursor:
        rowcount = 1

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            calls.append((query, params))

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

        def commit(self):
            return None

    monkeypatch.setattr(app_module, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.admin_routes, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.database, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.queue_service, "get_connection", lambda: FakeConnection())

    with app_module.app.test_request_context(
        "/admin/admin-accounts/1/delete",
        method="POST",
    ):
        response = app_module.admin_accounts_delete(1)

    assert response.status_code == 302
    assert "account_success" in response.headers["Location"]
    assert any("DELETE FROM admin_accounts WHERE id = %s" in query for query, _ in calls)


def test_admin_accounts_delete_blocks_self(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "is_audit_admin_authenticated", lambda: True)
    monkeypatch.setattr(app_module.admin_routes, "is_audit_admin_authenticated", lambda: True)
    monkeypatch.setattr(app_module, "get_current_admin_account_id", lambda: 1)
    monkeypatch.setattr(app_module.admin_routes, "get_current_admin_account_id", lambda: 1)

    with app_module.app.test_request_context(
        "/admin/admin-accounts/1/delete",
        method="POST",
    ):
        response = app_module.admin_accounts_delete(1)

    assert response.status_code == 302
    assert "account_error" in response.headers["Location"]


def test_admin_login_logs_page_shows_account_creation_ui(client, app_module, monkeypatch):
    monkeypatch.setattr(app_module, "is_audit_admin_authenticated", lambda: True)
    monkeypatch.setattr(app_module.admin_routes, "is_audit_admin_authenticated", lambda: True)
    monkeypatch.setattr(app_module, "has_audit_admin_account", lambda: True)
    monkeypatch.setattr(app_module.admin_routes, "has_audit_admin_account", lambda: True)
    monkeypatch.setattr(app_module, "get_current_admin_account_id", lambda: 1)
    monkeypatch.setattr(app_module.admin_routes, "get_current_admin_account_id", lambda: 1)

    class FakeCursor:
        def __init__(self):
            self._rows = []

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            if "FROM admin_login_logs" in query:
                self._rows = []
            elif "FROM admin_accounts" in query:
                self._rows = [(1, "admin", "admin", True, datetime(2026, 6, 21, 0, 0))]

        def fetchall(self):
            return self._rows

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

    monkeypatch.setattr(app_module, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.admin_routes, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.database, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.queue_service, "get_connection", lambda: FakeConnection())

    response = client.get("/admin/login-logs")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "単発作成" in body
    assert "一括作成" in body
    assert "この内容で作成" in body
    assert "一括で作成" in body


def test_admin_data_unauthorized(client):
    response = client.get("/admin/data")
    assert response.status_code == 401


def test_admin_data_includes_runtime_controls(client, app_module, monkeypatch):
    monkeypatch.setattr(app_module, "is_admin_authenticated", lambda: True)
    monkeypatch.setattr(app_module.admin_routes, "is_admin_authenticated", lambda: True)
    monkeypatch.setattr(app_module, "get_current_admin_account_id", lambda: 1)
    monkeypatch.setattr(app_module.admin_routes, "get_current_admin_account_id", lambda: 1)
    monkeypatch.setattr(app_module.admin_routes, "get_active_rows", lambda _cur, owner_admin_id=None: [])
    monkeypatch.setattr(
        app_module.admin_routes,
        "fetch_type_counts",
        lambda _cur, owner_admin_id: [],
    )
    monkeypatch.setattr(
        app_module,
        "get_runtime_settings",
        lambda *args, **kwargs: {
            "accepting_new": False,
            "auto_call_count": 7,
            "last_auto_call": {"message": "last"},
            "latest_auto_call": {"message": "latest"},
        },
    )
    monkeypatch.setattr(
        app_module.admin_routes,
        "get_runtime_settings",
        lambda *args, **kwargs: {
            "accepting_new": False,
            "auto_call_count": 7,
            "last_auto_call": {"message": "last"},
            "latest_auto_call": {"message": "latest"},
        },
    )

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

    monkeypatch.setattr(app_module, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.admin_routes, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.database, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.queue_service, "get_connection", lambda: FakeConnection())

    response = client.get("/admin/data")

    assert response.status_code == 200
    body = response.get_json()
    assert body["meta"]["accepting_new"] is False
    assert body["meta"]["auto_call_count"] == 7
    assert body["meta"]["last_auto_call"]["message"] == "last"
    assert body["meta"]["latest_auto_call"]["message"] == "latest"


def test_process_call_queue_task_without_token_returns_503(client, app_module):
    app_module.BATCH_CALL_RUNNER_TOKEN = ""
    response = client.post("/tasks/process-call-queue")
    assert response.status_code == 503


def test_process_call_queue_task_invalid_token_returns_403(
    client, app_module, monkeypatch
):
    app_module.BATCH_CALL_RUNNER_TOKEN = "token"
    monkeypatch.setattr(app_module, "validate_batch_runner_token", lambda: False)
    response = client.post("/tasks/process-call-queue")
    assert response.status_code == 403


def test_process_call_queue_task_success_returns_json(client, app_module, monkeypatch):
    app_module.BATCH_CALL_RUNNER_TOKEN = "token"
    monkeypatch.setattr(app_module, "validate_batch_runner_token", lambda: True)
    monkeypatch.setattr(
        app_module,
        "process_queued_calls",
        lambda: {"processed": True, "reason": "ok", "sent_count": 1, "failed_count": 0},
    )
    response = client.post("/tasks/process-call-queue")
    assert response.status_code == 200
    body = response.get_json()
    assert body["processed"] is True
    assert body["sent_count"] == 1


def test_callback_missing_signature_returns_400(client, app_module, monkeypatch):
    monkeypatch.setattr(app_module, "is_webhook_rate_limited", lambda _ip: False)
    monkeypatch.setattr(app_module.line_routes, "is_webhook_rate_limited", lambda _ip: False)
    response = client.post("/callback", data="{}", content_type="application/json")
    assert response.status_code == 400


def test_callback_invalid_signature_returns_400(client, app_module, monkeypatch):
    monkeypatch.setattr(app_module, "is_webhook_rate_limited", lambda _ip: False)
    monkeypatch.setattr(app_module.line_routes, "is_webhook_rate_limited", lambda _ip: False)

    class InvalidSignatureHandler:
        class parser:
            @staticmethod
            def parse(_body, _signature, as_payload=False):
                raise app_module.InvalidSignatureError("bad")

        @staticmethod
        def handle(_body, _signature):
            raise app_module.InvalidSignatureError("bad")

    monkeypatch.setattr(app_module, "handler", InvalidSignatureHandler())
    monkeypatch.setattr(app_module.line_routes, "handler", InvalidSignatureHandler())
    response = client.post(
        "/callback",
        data="{}",
        headers={"X-Line-Signature": "sig"},
        content_type="application/json",
    )
    assert response.status_code == 400


def test_callback_rate_limited_returns_429(client, app_module, monkeypatch):
    monkeypatch.setattr(app_module, "is_webhook_rate_limited", lambda _ip: True)
    response = client.post(
        "/callback",
        data="{}",
        headers={"X-Line-Signature": "sig"},
        content_type="application/json",
    )
    assert response.status_code == 429


def test_callback_success_returns_ok(client, app_module, monkeypatch, caplog):
    caplog.set_level(20, logger="line_routes")
    monkeypatch.setattr(app_module, "is_webhook_rate_limited", lambda _ip: False)
    monkeypatch.setattr(app_module.line_routes, "is_webhook_rate_limited", lambda _ip: False)
    handled = threading.Event()

    class DummyHandler:
        class parser:
            @staticmethod
            def parse(_body, _signature, as_payload=False):
                return object()

        @staticmethod
        def handle(_body, _signature):
            handled.set()

    monkeypatch.setattr(app_module, "handler", DummyHandler())
    monkeypatch.setattr(app_module.line_routes, "handler", DummyHandler())

    response = client.post(
        "/callback",
        data="{}",
        headers={"X-Line-Signature": "sig"},
        content_type="application/json",
    )
    assert response.status_code == 200
    assert response.get_data(as_text=True) == "OK"
    assert handled.wait(timeout=1)
    assert any(
        "metric=webhook_received" in record.message
        for record in caplog.records
    )
    assert any(
        "metric=webhook_request" in record.message
        and "result=accepted" in record.message
        for record in caplog.records
    )
    assert any(
        "metric=webhook_background" in record.message
        and "result=success" in record.message
        for record in caplog.records
    )


def test_callback_processing_error_returns_ok(client, app_module, monkeypatch):
    monkeypatch.setattr(app_module, "is_webhook_rate_limited", lambda _ip: False)
    monkeypatch.setattr(app_module.line_routes, "is_webhook_rate_limited", lambda _ip: False)

    class FailingHandler:
        class parser:
            @staticmethod
            def parse(_body, _signature, as_payload=False):
                return object()

        @staticmethod
        def handle(_body, _signature):
            raise RuntimeError("temporary downstream failure")

    monkeypatch.setattr(app_module, "handler", FailingHandler())
    monkeypatch.setattr(app_module.line_routes, "handler", FailingHandler())

    response = client.post(
        "/callback",
        data="{}",
        headers={"X-Line-Signature": "sig"},
        content_type="application/json",
    )
    assert response.status_code == 200
    assert response.get_data(as_text=True) == "OK"


def test_handle_message_keeps_processing_after_reservation_error(app_module, monkeypatch):
    processed_messages = []

    def fake_process_reservation(event, user_id, user_message):
        processed_messages.append(user_message)
        if len(processed_messages) == 1:
            raise RuntimeError("temporary failure")

    monkeypatch.setattr(app_module, "process_reservation", fake_process_reservation)
    monkeypatch.setattr(app_module.line_routes, "process_reservation", fake_process_reservation)

    first_event = SimpleNamespace(
        message=SimpleNamespace(text="予約 相談"),
        source=SimpleNamespace(user_id="U-1"),
        reply_token="reply-token-1",
    )
    second_event = SimpleNamespace(
        message=SimpleNamespace(text="待ち時間"),
        source=SimpleNamespace(user_id="U-1"),
        reply_token="reply-token-2",
    )

    app_module.handle_message(first_event)
    app_module.handle_message(second_event)

    assert processed_messages == ["予約 相談", "待ち時間"]


def test_managed_connection_rolls_back_on_exception(app_module, monkeypatch):
    rollback_called = []
    close_called = []

    class FakeConnection:
        def __init__(self):
            self.closed = 0

        def rollback(self):
            rollback_called.append(True)

        def close(self):
            close_called.append(True)
            self.closed = 1

    monkeypatch.setattr(app_module, "create_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.admin_routes, "create_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.database, "create_connection", lambda: FakeConnection())

    with pytest.raises(RuntimeError):
        with app_module.get_connection():
            raise RuntimeError("boom")

    assert rollback_called == [True]
    assert close_called == [True]


def test_should_run_call_batch_uses_localtime_when_now_none(app_module, monkeypatch):
    monkeypatch.setattr(
        app_module.time, "localtime", lambda: SimpleNamespace(tm_min=15)
    )
    assert app_module.should_run_call_batch() is True


def test_build_call_message_uses_ticket_ready_card(app_module):
    called_at = datetime(2026, 4, 19, 10, 0, tzinfo=ZoneInfo("Asia/Tokyo"))
    message = app_module.build_call_message(15, called_at=called_at)
    text = flex_message_text(message)
    assert "チケット番号" in text
    assert "0015" in text
    assert "admin" in text
    assert "ご用意ができました" in text
    assert f"{app_module.CALL_TIMEOUT_MINUTES}分以内" in text
    assert "10:15" in text
    assert "自動でキャンセル" in text
    assert "呼出中" not in text


def test_build_call_message_uses_admin_name(app_module):
    message = app_module.build_call_message(15, shop_name="manager01")
    assert "manager01" in flex_message_text(message)
    assert "admin" not in flex_message_text(message)


def test_build_call_message_includes_type_name(app_module):
    message = app_module.build_call_message(15, type_name="カレー")
    text = flex_message_text(message)
    assert "カレー" in text
    assert text.index("カレー") < text.index("ご用意ができました")


def test_expire_called_reservations_updates_called_rows(app_module, monkeypatch):
    sent_messages = []

    class FakeCursor:
        def __init__(self):
            self._rows = [(10, "U-1"), (11, "U-2"), (12, "U-3")]

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            assert "UPDATE reservations" in query
            assert "called_at <=" in query
            assert "RETURNING id, user_id" in query
            assert params[0] == app_module.STATUS_CANCELLED
            assert params[1] == app_module.STATUS_CALLED
            assert params[2] == app_module.CALL_TIMEOUT_MINUTES

        def fetchall(self):
            return self._rows

        def fetchone(self):
            return None

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

        def commit(self):
            return None

    monkeypatch.setattr(app_module, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.admin_routes, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.database, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.queue_service, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(
        app_module,
        "send_push_message",
        lambda user_id, message: sent_messages.append((user_id, message)),
    )
    monkeypatch.setattr(
        app_module.admin_routes,
        "send_push_message",
        lambda user_id, message: sent_messages.append((user_id, message)),
    )
    monkeypatch.setattr(
        app_module.queue_service,
        "send_push_message",
        lambda user_id, message: sent_messages.append((user_id, message)),
    )
    assert app_module.expire_called_reservations() == 3
    assert len(sent_messages) == 3
    assert sent_messages[0][0] == "U-1"
    assert "自動キャンセル" in flex_message_text(sent_messages[0][1])


def test_expire_called_reservations_ignores_push_failure(app_module, monkeypatch):
    class FakeCursor:
        def __init__(self):
            self._rows = [(20, "U-timeout")]

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, _query, _params=None):
            return None

        def fetchall(self):
            return self._rows

        def fetchone(self):
            return None

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

        def commit(self):
            return None

    monkeypatch.setattr(app_module, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.admin_routes, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.database, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.queue_service, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(
        app_module,
        "send_push_message",
        lambda _user_id, _text: (_ for _ in ()).throw(RuntimeError("push fail")),
    )
    monkeypatch.setattr(
        app_module.admin_routes,
        "send_push_message",
        lambda _user_id, _text: (_ for _ in ()).throw(RuntimeError("push fail")),
    )
    assert app_module.expire_called_reservations() == 1


def test_cancel_active_reservations_without_notification_updates_active_rows(
    app_module, monkeypatch
):
    class FakeCursor:
        def __init__(self):
            self._rows = [(30,), (31,), (32,)]

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            assert "UPDATE reservations" in query
            assert "WHERE status IN" in query
            assert params == (
                app_module.STATUS_CANCELLED,
                app_module.STATUS_WAITING,
                app_module.STATUS_CALLED,
            )

        def fetchall(self):
            return self._rows

        def fetchone(self):
            return None

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

        def commit(self):
            return None

    sent_messages = []
    monkeypatch.setattr(app_module, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.admin_routes, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.database, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.queue_service, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(
        app_module,
        "send_push_message",
        lambda user_id, message: sent_messages.append((user_id, message)),
    )
    monkeypatch.setattr(
        app_module.admin_routes,
        "send_push_message",
        lambda user_id, message: sent_messages.append((user_id, message)),
    )

    assert app_module.cancel_active_reservations_without_notification() == 3
    assert sent_messages == []


def test_process_queued_calls_midnight_cancels_without_push(app_module, monkeypatch):
    executed = []
    sent_messages = []

    class FakeCursor:
        def __init__(self):
            self._rows = []

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            normalized_query = " ".join(query.split())
            executed.append((normalized_query, params))
            if "WHERE status IN" in normalized_query:
                self._rows = [(40,), (41,)]
            elif "RETURNING id, user_id" in normalized_query:
                self._rows = []
            else:
                self._rows = []

        def fetchall(self):
            return self._rows

        def fetchone(self):
            return None

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

        def commit(self):
            return None

    monkeypatch.setattr(app_module, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.admin_routes, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.database, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.queue_service, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module, "should_run_call_batch", lambda _now: True)
    monkeypatch.setattr(app_module.queue_service, "should_run_call_batch", lambda _now: True)
    monkeypatch.setattr(app_module, "expire_called_reservations", lambda: 99)
    monkeypatch.setattr(app_module.queue_service, "expire_called_reservations", lambda: 99)
    monkeypatch.setattr(
        app_module,
        "refresh_wait_time_estimate",
        lambda _now=None: {
            "message": "現在の目安待ち時間: 2分",
            "estimated_seconds": 120,
        },
    )
    monkeypatch.setattr(
        app_module.queue_service,
        "refresh_wait_time_estimate",
        lambda _now=None: {
            "message": "現在の目安待ち時間: 2分",
            "estimated_seconds": 120,
        },
    )
    monkeypatch.setattr(app_module, "ensure_database_schema", lambda: None)
    monkeypatch.setattr(app_module.queue_service, "ensure_database_schema", lambda: None)
    monkeypatch.setattr(
        app_module,
        "get_runtime_settings",
        lambda: {
            "auto_call_count": 0,
            "latest_auto_call": {
                "run_at": "",
                "sent_count": 0,
                "failed_count": 0,
                "selected_count": 0,
            },
        },
    )
    monkeypatch.setattr(
        app_module.admin_routes,
        "get_runtime_settings",
        lambda: {
            "auto_call_count": 0,
            "latest_auto_call": {
                "run_at": "",
                "sent_count": 0,
                "failed_count": 0,
                "selected_count": 0,
            },
        },
    )
    monkeypatch.setattr(
        app_module.queue_service,
        "get_runtime_settings",
        lambda: {
            "auto_call_count": 0,
            "latest_auto_call": {
                "run_at": "",
                "sent_count": 0,
                "failed_count": 0,
                "selected_count": 0,
            },
        },
    )
    monkeypatch.setattr(app_module, "set_settings", lambda _values: None)
    monkeypatch.setattr(app_module.queue_service, "set_settings", lambda _values: None)
    monkeypatch.setattr(
        app_module,
        "send_push_message",
        lambda user_id, text: sent_messages.append((user_id, text)),
    )
    monkeypatch.setattr(
        app_module.admin_routes,
        "send_push_message",
        lambda user_id, text: sent_messages.append((user_id, text)),
    )

    now = datetime(2026, 4, 16, 0, 0, tzinfo=ZoneInfo("Asia/Tokyo"))
    result = app_module.process_queued_calls(now=now)

    assert result["midnight_cancel_count"] == 2
    assert result["timed_out_count"] == 0
    assert sent_messages == []
    assert any("WHERE status IN" in query for query, _ in executed)
    assert all("called_at <=" not in query for query, _ in executed)


def test_process_queued_calls_not_due_returns_early(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "should_run_call_batch", lambda _now: False)
    monkeypatch.setattr(app_module.queue_service, "should_run_call_batch", lambda _now: False)
    monkeypatch.setattr(app_module, "expire_called_reservations", lambda: 2)
    monkeypatch.setattr(app_module.queue_service, "expire_called_reservations", lambda: 2)
    monkeypatch.setattr(
        app_module,
        "refresh_wait_time_estimate",
        lambda _now=None: {
            "message": "現在の目安待ち時間: 6分0秒",
            "estimated_seconds": 360,
        },
    )
    monkeypatch.setattr(
        app_module.queue_service,
        "refresh_wait_time_estimate",
        lambda _now=None: {
            "message": "現在の目安待ち時間: 6分0秒",
            "estimated_seconds": 360,
        },
    )
    now = datetime(2026, 4, 16, 10, 1, tzinfo=ZoneInfo("Asia/Tokyo"))
    result = app_module.process_queued_calls(now=now)
    assert result["processed"] is False
    assert result["reason"] == "not_due"
    assert result["timed_out_count"] == 2
    assert result["wait_time"]["estimated_seconds"] == 360


def test_process_queued_calls_rolls_back_failed_push_rows(app_module, monkeypatch):
    executed = []
    commits = []

    class FakeCursor:
        def __init__(self):
            self._rows = []

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            normalized_query = " ".join(query.split())
            executed.append((normalized_query, params))
            if "RETURNING id, user_id" in normalized_query:
                self._rows = [(10, "U-ok"), (11, "U-fail")]
            else:
                self._rows = []

        def fetchall(self):
            return self._rows

        def fetchone(self):
            return None

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

        def commit(self):
            commits.append(True)

    def fake_send_push(user_id, _text):
        if user_id == "U-fail":
            raise RuntimeError("push failed")

    monkeypatch.setattr(app_module, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.admin_routes, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.database, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.queue_service, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module, "should_run_call_batch", lambda _now: True)
    monkeypatch.setattr(app_module.queue_service, "should_run_call_batch", lambda _now: True)
    monkeypatch.setattr(app_module, "expire_called_reservations", lambda: 0)
    monkeypatch.setattr(app_module.queue_service, "expire_called_reservations", lambda: 0)
    monkeypatch.setattr(app_module, "cleanup_rate_limit_records", lambda: None)
    monkeypatch.setattr(app_module.queue_service, "cleanup_rate_limit_records", lambda: None)
    monkeypatch.setattr(
        app_module,
        "refresh_wait_time_estimate",
        lambda _now=None: {
            "message": "現在の目安待ち時間: 2分",
            "estimated_seconds": 120,
        },
    )
    monkeypatch.setattr(
        app_module.queue_service,
        "refresh_wait_time_estimate",
        lambda _now=None: {
            "message": "現在の目安待ち時間: 2分",
            "estimated_seconds": 120,
        },
    )
    monkeypatch.setattr(app_module, "ensure_database_schema", lambda: None)
    monkeypatch.setattr(app_module.queue_service, "ensure_database_schema", lambda: None)
    monkeypatch.setattr(
        app_module,
        "get_runtime_settings",
        lambda: {
            "auto_call_count": 2,
            "latest_auto_call": {
                "run_at": "",
                "sent_count": 0,
                "failed_count": 0,
                "selected_count": 0,
            },
        },
    )
    monkeypatch.setattr(
        app_module.admin_routes,
        "get_runtime_settings",
        lambda: {
            "auto_call_count": 2,
            "latest_auto_call": {
                "run_at": "",
                "sent_count": 0,
                "failed_count": 0,
                "selected_count": 0,
            },
        },
    )
    monkeypatch.setattr(
        app_module.queue_service,
        "get_runtime_settings",
        lambda: {
            "auto_call_count": 2,
            "latest_auto_call": {
                "run_at": "",
                "sent_count": 0,
                "failed_count": 0,
                "selected_count": 0,
            },
        },
    )
    saved_settings = {}
    monkeypatch.setattr(
        app_module, "set_settings", lambda values: saved_settings.update(values)
    )
    monkeypatch.setattr(
        app_module.queue_service, "set_settings", lambda values: saved_settings.update(values)
    )
    monkeypatch.setattr(app_module, "send_push_message", fake_send_push)
    monkeypatch.setattr(app_module.admin_routes, "send_push_message", fake_send_push)
    monkeypatch.setattr(app_module.queue_service, "send_push_message", fake_send_push)

    now = datetime(2026, 4, 16, 10, 0, tzinfo=ZoneInfo("Asia/Tokyo"))
    result = app_module.process_queued_calls(now=now)

    assert result["sent_count"] == 1
    assert result["failed_count"] == 1
    assert result["failed_ids"] == [11]
    rollback_queries = [item for item in executed if "called_at = NULL" in item[0]]
    assert rollback_queries == [
        (
            "UPDATE reservations SET status = %s, called_at = NULL, call_origin = NULL WHERE id = ANY(%s) AND status = %s",
            (app_module.STATUS_WAITING, [11], app_module.STATUS_CALLED),
        )
    ]
    assert saved_settings["last_auto_call_failed_count"] == "1"
    assert len(commits) == 2


def test_process_queued_calls_uses_skip_locked_and_total_limit(app_module, monkeypatch):
    executed = []
    sent_messages = []

    class FakeCursor:
        def __init__(self):
            self._rows = []

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            normalized_query = " ".join(query.split())
            executed.append((normalized_query, params))
            if "RETURNING id, user_id" in normalized_query:
                assert "FOR UPDATE SKIP LOCKED" in normalized_query
                assert "AND status = %s" in normalized_query
                self._rows = [(10, "U-total-1")]
            else:
                self._rows = []

        def fetchall(self):
            return self._rows

        def fetchone(self):
            return None

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

        def commit(self):
            return None

    monkeypatch.setattr(app_module, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.admin_routes, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.database, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.queue_service, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module, "should_run_call_batch", lambda _now: True)
    monkeypatch.setattr(app_module.queue_service, "should_run_call_batch", lambda _now: True)
    monkeypatch.setattr(app_module, "expire_called_reservations", lambda: 0)
    monkeypatch.setattr(app_module.queue_service, "expire_called_reservations", lambda: 0)
    monkeypatch.setattr(
        app_module,
        "refresh_wait_time_estimate",
        lambda _now=None: {
            "message": "現在の目安待ち時間: 2分",
            "estimated_seconds": 120,
        },
    )
    monkeypatch.setattr(
        app_module.queue_service,
        "refresh_wait_time_estimate",
        lambda _now=None: {
            "message": "現在の目安待ち時間: 2分",
            "estimated_seconds": 120,
        },
    )
    monkeypatch.setattr(
        app_module,
        "get_runtime_settings",
        lambda: {
            "auto_call_count": 1,
            "latest_auto_call": {
                "run_at": "",
                "sent_count": 0,
                "failed_count": 0,
                "selected_count": 0,
            },
        },
    )
    monkeypatch.setattr(
        app_module.admin_routes,
        "get_runtime_settings",
        lambda: {
            "auto_call_count": 1,
            "latest_auto_call": {
                "run_at": "",
                "sent_count": 0,
                "failed_count": 0,
                "selected_count": 0,
            },
        },
    )
    monkeypatch.setattr(
        app_module.queue_service,
        "get_runtime_settings",
        lambda: {
            "auto_call_count": 1,
            "latest_auto_call": {
                "run_at": "",
                "sent_count": 0,
                "failed_count": 0,
                "selected_count": 0,
            },
        },
    )
    monkeypatch.setattr(app_module, "set_settings", lambda _values: None)
    monkeypatch.setattr(app_module.queue_service, "set_settings", lambda _values: None)
    monkeypatch.setattr(
        app_module,
        "send_push_message",
        lambda user_id, text: sent_messages.append((user_id, text)),
    )
    monkeypatch.setattr(
        app_module.admin_routes,
        "send_push_message",
        lambda user_id, text: sent_messages.append((user_id, text)),
    )
    monkeypatch.setattr(
        app_module.queue_service,
        "send_push_message",
        lambda user_id, text: sent_messages.append((user_id, text)),
    )

    now = datetime(2026, 4, 16, 10, 0, tzinfo=ZoneInfo("Asia/Tokyo"))
    result = app_module.process_queued_calls(now=now)

    assert result["sent_count"] == 1
    assert result["failed_count"] == 0
    assert result["auto_selected_count"] == 1
    assert [item[0] for item in sent_messages] == ["U-total-1"]


def test_process_reservation_new_booking_replies_with_latest_wait_time(
    app_module, monkeypatch
):
    queries = []

    class FakeCursor:
        def __init__(self):
            self._last = None

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            queries.append((query, params))
            if "FROM reservation_types" in query and "WHERE name = %s" in query:
                self._last = (1, "相談", True, 7, "説明", "")
            elif "WHERE r.user_id = %s AND r.status IN" in query:
                self._last = None
            elif "next_reservation_no" in query and "FROM admin_accounts" in query:
                self._last = (1,)
            elif "FROM admin_accounts" in query and "login_id" in query:
                self._last = None
            elif "UPDATE admin_accounts" in query and "next_reservation_no" in query:
                self._last = None
            elif "SELECT reservation_no FROM reservations" in query:
                self._last_all = []
            elif "INSERT INTO reservations" in query:
                self._last = (10,)
            elif "SELECT 1 FROM reservations" in query:
                self._last = None
            elif "FROM app_settings" in query:
                self._last = None
            elif (
                "JOIN reservation_types t ON r.type_id = t.id" in query
                and "r.id < %s" in query
            ):
                self._last = (2,)
            else:
                raise AssertionError(f"Unexpected query: {query}")

        def fetchone(self):
            return self._last

        def fetchall(self):
            return getattr(self, "_last_all", [])

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

        def commit(self):
            return None

    monkeypatch.setattr(app_module, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.line_routes, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.admin_routes, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.database, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.queue_service, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module, "is_accepting_new", lambda *args, **kwargs: True)
    monkeypatch.setattr(app_module.line_routes, "is_accepting_new", lambda *args, **kwargs: True)
    monkeypatch.setattr(app_module.admin_routes, "is_accepting_new", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        app_module,
        "refresh_wait_time_estimate",
        lambda now=None, owner_admin_id=None: {
            "message": "現在の目安待ち時間: 6分",
            "estimated_seconds": 360,
        },
    )
    monkeypatch.setattr(
        app_module.line_routes,
        "refresh_wait_time_estimate",
        lambda now=None, owner_admin_id=None: {
            "message": "現在の目安待ち時間: 6分",
            "estimated_seconds": 360,
        },
    )

    sent_texts = []

    monkeypatch.setattr(
        app_module,
        "send_flex_notice",
        lambda *args, **kwargs: sent_texts.append(
            args[2] if len(args) > 2 else kwargs.get("body")
        ),
    )
    monkeypatch.setattr(
        app_module.line_routes,
        "send_flex_notice",
        lambda *args, **kwargs: sent_texts.append(
            args[2] if len(args) > 2 else kwargs.get("body")
        ),
    )

    event = SimpleNamespace(reply_token="reply-token")
    app_module.process_reservation(event, "U-123", "予約 相談")

    assert sent_texts
    assert "【受付完了】チケット番号: 001" in sent_texts[0]
    assert " / 種類: 相談 / 待ち: 2人" in sent_texts[0]
    assert "現在の目安待ち時間: 3分" in sent_texts[0]



def test_process_reservation_wait_time_reply_for_waiting_user(app_module, monkeypatch):
    class FakeCursor:
        def __init__(self):
            self._last = None

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            if (
                "FROM reservations r" in query
                and "WHERE r.user_id = %s AND r.status IN" in query
            ):
                self._last = (12, 12, app_module.STATUS_WAITING, "相談", 7)
            elif (
                "JOIN reservation_types t ON r.type_id = t.id" in query
                and "r.id < %s" in query
            ):
                self._last = (3,)
            else:
                raise AssertionError(f"Unexpected query: {query}")

        def fetchone(self):
            return self._last

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

        def commit(self):
            return None

    monkeypatch.setattr(app_module, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.line_routes, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.admin_routes, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.database, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.queue_service, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module, "is_accepting_new", lambda *args, **kwargs: True)
    monkeypatch.setattr(app_module.line_routes, "is_accepting_new", lambda *args, **kwargs: True)
    monkeypatch.setattr(app_module.admin_routes, "is_accepting_new", lambda *args, **kwargs: True)

    sent_texts = []
    monkeypatch.setattr(
        app_module,
        "send_flex_notice",
        lambda *args, **kwargs: sent_texts.append(
            args[2] if len(args) > 2 else kwargs.get("body")
        ),
    )
    monkeypatch.setattr(
        app_module.line_routes,
        "send_flex_notice",
        lambda *args, **kwargs: sent_texts.append(
            args[2] if len(args) > 2 else kwargs.get("body")
        ),
    )

    event = SimpleNamespace(reply_token="reply-token")
    app_module.process_reservation(event, "U-789", "待ち時間")

    assert sent_texts
    assert "あなたの前: 3人" in sent_texts[-1]
    assert "現在の目安待ち時間: 4分" in sent_texts[-1]


def test_process_reservation_wait_time_reply_without_active_reservation(
    app_module, monkeypatch
):
    class FakeCursor:
        def __init__(self):
            self._last = None

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            if (
                "FROM reservations r" in query
                and "WHERE r.user_id = %s AND r.status IN" in query
            ):
                self._last = None
            else:
                raise AssertionError(f"Unexpected query: {query}")

        def fetchone(self):
            return self._last

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

        def commit(self):
            return None

    monkeypatch.setattr(app_module, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.line_routes, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.admin_routes, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.database, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.queue_service, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module, "is_accepting_new", lambda *args, **kwargs: True)
    monkeypatch.setattr(app_module.line_routes, "is_accepting_new", lambda *args, **kwargs: True)
    monkeypatch.setattr(app_module.admin_routes, "is_accepting_new", lambda *args, **kwargs: True)

    sent_texts = []
    monkeypatch.setattr(
        app_module,
        "send_flex_notice",
        lambda *args, **kwargs: sent_texts.append(
            args[2] if len(args) > 2 else kwargs.get("body")
        ),
    )
    monkeypatch.setattr(
        app_module.line_routes,
        "send_flex_notice",
        lambda *args, **kwargs: sent_texts.append(
            args[2] if len(args) > 2 else kwargs.get("body")
        ),
    )

    event = SimpleNamespace(reply_token="reply-token")
    app_module.process_reservation(event, "U-999", "待ち時間")

    assert sent_texts
    assert "待ち時間を確認できる予約がありません" in sent_texts[-1]


def test_process_reservation_cancel_commits_when_cancelled(app_module, monkeypatch):
    commits = []

    class FakeCursor:
        def __init__(self):
            self._last = None

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            if (
                "UPDATE reservations SET status = %s" in query
                and "RETURNING id" in query
            ):
                self._last = (42,)
            else:
                raise AssertionError(f"Unexpected query: {query}")

        def fetchone(self):
            return self._last

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

        def commit(self):
            commits.append(True)

    monkeypatch.setattr(app_module, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.line_routes, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.admin_routes, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.database, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.queue_service, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module, "is_accepting_new", lambda *args, **kwargs: True)
    monkeypatch.setattr(app_module.line_routes, "is_accepting_new", lambda *args, **kwargs: True)
    monkeypatch.setattr(app_module.admin_routes, "is_accepting_new", lambda *args, **kwargs: True)

    sent_texts = []
    monkeypatch.setattr(
        app_module,
        "send_flex_notice",
        lambda *args, **kwargs: sent_texts.append(
            args[2] if len(args) > 2 else kwargs.get("body")
        ),
    )
    monkeypatch.setattr(
        app_module.line_routes,
        "send_flex_notice",
        lambda *args, **kwargs: sent_texts.append(
            args[2] if len(args) > 2 else kwargs.get("body")
        ),
    )

    event = SimpleNamespace(reply_token="reply-token")
    app_module.process_reservation(event, "U-cancel", "キャンセル")

    assert sent_texts
    assert "受付チケット番号 000042 をキャンセルしました。" in sent_texts[-1]
    assert commits



def test_admin_history_export_includes_extended_columns(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "is_admin_authenticated", lambda: True)
    monkeypatch.setattr(app_module.admin_routes, "is_admin_authenticated", lambda: True)
    monkeypatch.setattr(app_module, "get_current_admin_account_id", lambda: 7)
    monkeypatch.setattr(app_module.admin_routes, "get_current_admin_account_id", lambda: 7)

    queries = []

    class FakeCursor:
        def __init__(self):
            self._rows = [
                (
                    12,
                    12,
                    "相談",
                    app_module.STATUS_DONE,
                        "auto",
                    datetime(2026, 4, 20, 2, 15, tzinfo=ZoneInfo("UTC")),
                    datetime(2026, 4, 20, 2, 25, tzinfo=ZoneInfo("UTC")),
                    datetime(2026, 4, 20, 3, 0, tzinfo=ZoneInfo("UTC")),
                    600,
                    2700,
                    2100,
                )
            ]

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            queries.append((query, params))

        def __iter__(self):
            return iter(self._rows)

    class FakeConnection:
        def __init__(self):
            self.closed = False

        def cursor(self, name=None):
            return FakeCursor()

        def close(self):
            self.closed = True

    monkeypatch.setattr(app_module, "create_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.admin_routes, "create_connection", lambda: FakeConnection())

    with app_module.app.test_request_context("/admin/history/export.csv"):
        response = app_module.admin_history_export()
        text = response.get_data(as_text=True)

    rows = list(csv.reader(text.splitlines()))
    assert rows[0] == [
        "チケット番号",
        "種類",
        "状態",
        "呼出方法",
        "受付時刻",
        "呼出時刻",
        "完了時刻",
        "受付から呼出",
        "受付から完了",
        "呼出から完了",
    ]
    assert rows[1] == [
        "000012",
        "相談",
        app_module.STATUS_DONE,
        "自動",
        "04-20 11:15",
        "04-20 11:25",
        "04-20 12:00",
        "10分0秒",
        "45分0秒",
        "35分0秒",
    ]
    assert any("r.called_at" in query for query, _ in queries)


def test_admin_history_export_requires_login(client):
    response = client.get("/admin/history/export.csv")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/login")


def test_admin_history_export_null_values_are_formatted_safely(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "is_admin_authenticated", lambda: True)
    monkeypatch.setattr(app_module.admin_routes, "is_admin_authenticated", lambda: True)
    monkeypatch.setattr(app_module, "get_current_admin_account_id", lambda: 7)
    monkeypatch.setattr(app_module.admin_routes, "get_current_admin_account_id", lambda: 7)

    class FakeCursor:
        def __init__(self):
            self._rows = [
                (
                    99,
                    99,
                    "",
                    app_module.STATUS_CANCELLED,
                        None,
                    datetime(2026, 4, 21, 0, 0, tzinfo=ZoneInfo("UTC")),
                    None,
                    None,
                    None,
                    None,
                    None,
                )
            ]

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, _query, _params=None):
            return None

        def __iter__(self):
            return iter(self._rows)

    class FakeConnection:
        def __init__(self):
            self.closed = False

        def cursor(self, name=None):
            return FakeCursor()

        def close(self):
            self.closed = True

    monkeypatch.setattr(app_module, "create_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.admin_routes, "create_connection", lambda: FakeConnection())

    with app_module.app.test_request_context("/admin/history/export.csv"):
        response = app_module.admin_history_export()
        text = response.get_data(as_text=True)

    rows = list(csv.reader(text.splitlines()))
    assert rows[1] == [
        "000099",
        "",
        app_module.STATUS_CANCELLED,
        "不明",
        "04-21 09:00",
        "",
        "",
        "-",
        "-",
        "-",
    ]


def test_admin_history_export_invalid_query_params_fall_back_to_defaults(
    app_module, monkeypatch
):
    monkeypatch.setattr(app_module, "is_admin_authenticated", lambda: True)
    monkeypatch.setattr(app_module.admin_routes, "is_admin_authenticated", lambda: True)
    monkeypatch.setattr(app_module, "get_current_admin_account_id", lambda: 7)
    monkeypatch.setattr(app_module.admin_routes, "get_current_admin_account_id", lambda: 7)

    calls = []

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            calls.append((query, params))

        def __iter__(self):
            return iter([])

    class FakeConnection:
        def __init__(self):
            self.closed = False

        def cursor(self, name=None):
            return FakeCursor()

        def close(self):
            self.closed = True

    monkeypatch.setattr(app_module, "create_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.admin_routes, "create_connection", lambda: FakeConnection())

    with app_module.app.test_request_context(
        "/admin/history/export.csv?sort_by=unknown&sort_order=sideways&type_id=abc"
    ):
        response = app_module.admin_history_export()
        _ = response.get_data(as_text=True)

    assert calls
    query, params = calls[0]
    assert "ORDER BY COALESCE(r.reservation_no, r.id) DESC, r.id DESC" in query
    assert params == [app_module.STATUS_DONE, app_module.STATUS_CANCELLED, 7]


def test_process_reservation_replies_with_carousel_when_no_type_specified(
    app_module, monkeypatch
):
    queries = []
    sent_replies = []

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            queries.append((query, params))

        def fetchall(self):
            if "FROM reservation_types" in queries[-1][0] and "image_mime_type" in queries[-1][0]:
                return [
                    (
                        1,
                        "相談",
                        "個別相談を受け付けます。",
                        True,
                        "image/png",
                    ),
                    (2, "体験", "体験ブースへの案内です。", False, ""),
                ]
            return []

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

        def commit(self):
            return None

    monkeypatch.setattr(app_module, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.line_routes, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.admin_routes, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.database, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.queue_service, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module, "is_accepting_new", lambda *args, **kwargs: True)
    monkeypatch.setattr(app_module.line_routes, "is_accepting_new", lambda *args, **kwargs: True)
    monkeypatch.setattr(app_module.admin_routes, "is_accepting_new", lambda *args, **kwargs: True)
    
    # Mock send_reply_message to capture the reply
    monkeypatch.setattr(
        app_module,
        "send_reply_message",
        lambda reply_token, message: sent_replies.append((reply_token, message))
    )
    monkeypatch.setattr(
        app_module.line_routes,
        "send_reply_message",
        lambda reply_token, message: sent_replies.append((reply_token, message))
    )

    event = SimpleNamespace(reply_token="test-reply-token")
    app_module.process_reservation(event, "U-carousel-user", "予約")

    assert len(sent_replies) == 1
    reply_token, flex_msg = sent_replies[0]
    assert reply_token == "test-reply-token"
    
    # Verify the flex message structure
    assert flex_msg["type"] == "flex"
    assert flex_msg["altText"] == "予約の種類一覧"
    carousel = flex_msg["contents"]
    assert carousel["type"] == "carousel"
    
    bubbles = carousel["contents"]
    assert len(bubbles) == 2
    
    # First bubble (相談 - Accepting)
    bubble_1 = bubbles[0]
    assert bubble_1["header"]["contents"][0]["text"] == "相談"
    assert bubble_1["hero"]["url"].endswith("/reservation-type-images/1?v=1")
    assert bubble_1["body"]["contents"][0]["contents"][0]["contents"][0]["text"] == "受付中"
    assert bubble_1["body"]["contents"][2]["text"] == "個別相談を受け付けます。"
    assert bubble_1["footer"]["contents"][0]["type"] == "button"
    assert bubble_1["footer"]["contents"][0]["action"]["text"] == "予約 相談"
    
    # Second bubble (体験 - Not accepting)
    bubble_2 = bubbles[1]
    assert bubble_2["header"]["contents"][0]["text"] == "体験"
    assert bubble_2["body"]["contents"][0]["contents"][0]["contents"][0]["text"] == "受付停止中"
    assert bubble_2["body"]["contents"][2]["text"] == "体験ブースへの案内です。"
    assert bubble_2["footer"]["contents"][0]["contents"][0]["text"] == "現在受付停止中"


def test_admin_types_registration_optional_price(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "is_admin_authenticated", lambda: True)
    monkeypatch.setattr(app_module.admin_routes, "is_admin_authenticated", lambda: True)
    monkeypatch.setattr(app_module, "get_current_admin_account_id", lambda: 1)
    monkeypatch.setattr(app_module.admin_routes, "get_current_admin_account_id", lambda: 1)
    monkeypatch.setattr(app_module, "is_accepting_new", lambda *args, **kwargs: True)
    monkeypatch.setattr(app_module.admin_routes, "is_accepting_new", lambda *args, **kwargs: True)

    executed = []

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            executed.append((" ".join(query.split()), params))
            if "INSERT INTO reservation_types" in query:
                self.rowcount = 1
                self._id = 1
            else:
                self._id = None

        def fetchone(self):
            if hasattr(self, "_id") and self._id is not None:
                return (self._id,)
            return None

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

        def commit(self):
            return None

    monkeypatch.setattr(app_module, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.admin_routes, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.database, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.queue_service, "get_connection", lambda: FakeConnection())

    with app_module.app.test_request_context(
        "/admin/types",
        method="POST",
        data={"name": "新サービス", "price": "", "flavor_text": "説明文"},
    ):
        response = app_module.admin_types_page()

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/admin/types?type_success=%E7%A8%AE%E9%A1%9E%E3%82%92%E8%BF%BD%E5%8A%A0%E3%81%97%E3%81%BE%E3%81%97%E3%81%9F%E3%80%82")

    insert_queries = [p for q, p in executed if "INSERT INTO reservation_types" in q]
    assert len(insert_queries) == 1
    params = insert_queries[0]
    assert params[0] == "新サービス"
    assert params[6] is None


def test_admin_types_update_optional_price(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "is_admin_authenticated", lambda: True)
    monkeypatch.setattr(app_module.admin_routes, "is_admin_authenticated", lambda: True)
    monkeypatch.setattr(app_module, "get_current_admin_account_id", lambda: 1)
    monkeypatch.setattr(app_module.admin_routes, "get_current_admin_account_id", lambda: 1)

    executed = []

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            executed.append((" ".join(query.split()), params))
            if "UPDATE reservation_types SET price = %s" in query:
                self.rowcount = 1

        def fetchone(self):
            return None

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

        def commit(self):
            return None

    monkeypatch.setattr(app_module, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.admin_routes, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.database, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.queue_service, "get_connection", lambda: FakeConnection())

    with app_module.app.test_request_context(
        "/admin/types/1/price",
        method="POST",
        data={"price": ""},
    ):
        response = app_module.admin_types_update_price(1)

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/admin/types?type_success=%E4%BE%A1%E6%A0%BC%E3%82%92%E6%9B%B4%E6%96%B0%E3%81%97%E3%81%BE%E3%81%97%E3%81%9F%E3%80%82")

    update_queries = [p for q, p in executed if "UPDATE reservation_types SET price = %s" in q]
    assert len(update_queries) == 1
    params = update_queries[0]
    assert params[0] is None
    assert params[1] == 1


def test_admin_types_registration_price_limit(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "is_admin_authenticated", lambda: True)
    monkeypatch.setattr(app_module.admin_routes, "is_admin_authenticated", lambda: True)
    monkeypatch.setattr(app_module, "get_current_admin_account_id", lambda: 1)
    monkeypatch.setattr(app_module.admin_routes, "get_current_admin_account_id", lambda: 1)
    monkeypatch.setattr(app_module, "is_accepting_new", lambda *args, **kwargs: True)
    monkeypatch.setattr(app_module.admin_routes, "is_accepting_new", lambda *args, **kwargs: True)

    with app_module.app.test_request_context(
        "/admin/types",
        method="POST",
        data={"name": "高額サービス", "price": "99999"},
    ):
        response = app_module.admin_types_page()

    assert response.status_code == 302
    assert "type_error" in response.headers["Location"]
    from urllib.parse import unquote
    assert "99,998円以下" in unquote(response.headers["Location"])


def test_admin_types_update_price_limit(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "is_admin_authenticated", lambda: True)
    monkeypatch.setattr(app_module.admin_routes, "is_admin_authenticated", lambda: True)
    monkeypatch.setattr(app_module, "get_current_admin_account_id", lambda: 1)
    monkeypatch.setattr(app_module.admin_routes, "get_current_admin_account_id", lambda: 1)

    with app_module.app.test_request_context(
        "/admin/types/1/price",
        method="POST",
        data={"price": "100000"},
    ):
        response = app_module.admin_types_update_price(1)

    assert response.status_code == 302
    assert "type_error" in response.headers["Location"]
    from urllib.parse import unquote
    assert "99,998円以下" in unquote(response.headers["Location"])


def test_is_accepting_new_per_admin(app_module, monkeypatch):
    queries = []

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            queries.append((query, params))

        def fetchone(self):
            last_query = queries[-1][0]
            if "SELECT accepting_new" in last_query:
                params = queries[-1][1]
                if params and params[0] == 1:
                    return (False, True)  # accepting_new=False, active=True
                return (True, True)  # accepting_new=True, active=True
            elif "SELECT EXISTS" in last_query:
                return (True,)
            return None

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

        def commit(self):
            pass

    monkeypatch.setattr(app_module, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.admin_routes, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.database, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.queue_service, "get_connection", lambda: FakeConnection())

    assert app_module.is_accepting_new(1) is False
    assert "SELECT accepting_new" in queries[-1][0]
    assert queries[-1][1] == (1,)

    assert app_module.is_accepting_new(2) is True
    assert queries[-1][1] == (2,)

    assert app_module.is_accepting_new() is True
    assert "SELECT EXISTS" in queries[-1][0]


def test_set_accepting_new_per_admin(app_module, monkeypatch):
    queries = []

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            queries.append((query, params))

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

        def commit(self):
            pass

    monkeypatch.setattr(app_module, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.admin_routes, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.database, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.queue_service, "get_connection", lambda: FakeConnection())

    app_module.set_accepting_new(False, 1)
    assert "UPDATE admin_accounts SET accepting_new" in queries[-1][0]
    assert queries[-1][1] == (False, 1)

    app_module.set_accepting_new(True)
    assert "INSERT INTO app_settings" in queries[-1][0]
    assert queries[-1][1] == ("accepting_new", "true")


def test_expire_called_reservations_records_completed_at(app_module, monkeypatch):
    executed_queries = []

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            executed_queries.append((query, params))

        def fetchall(self):
            return [(1, "U123", 1)]

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

        def commit(self):
            pass

    monkeypatch.setattr(app_module, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.admin_routes, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.database, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.queue_service, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module, "send_push_message", lambda uid, flex: None)
    monkeypatch.setattr(app_module.admin_routes, "send_push_message", lambda uid, flex: None)

    count = app_module.expire_called_reservations()
    assert count == 1
    assert "completed_at = CURRENT_TIMESTAMP" in executed_queries[0][0]


def test_cancel_active_reservations_without_notification_records_completed_at(app_module, monkeypatch):
    executed_queries = []

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            executed_queries.append((query, params))

        def fetchall(self):
            return [(1,), (2,)]

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

        def commit(self):
            pass

    monkeypatch.setattr(app_module, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.admin_routes, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.database, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.queue_service, "get_connection", lambda: FakeConnection())

    count = app_module.cancel_active_reservations_without_notification()
    assert count == 2
    assert "completed_at = CURRENT_TIMESTAMP" in executed_queries[0][0]


def test_admin_cancel_route_success(app_module, monkeypatch):
    executed_queries = []

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            executed_queries.append((query, params))

        def fetchone(self):
            return (1,)

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

        def commit(self):
            pass

    monkeypatch.setattr(app_module, "is_admin_authenticated", lambda: True)
    monkeypatch.setattr(app_module.admin_routes, "is_admin_authenticated", lambda: True)
    monkeypatch.setattr(app_module, "get_current_admin_account_id", lambda: 1)
    monkeypatch.setattr(app_module.admin_routes, "get_current_admin_account_id", lambda: 1)
    monkeypatch.setattr(app_module, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.admin_routes, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.database, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.queue_service, "get_connection", lambda: FakeConnection())

    with app_module.app.test_client() as client:
        res = client.post("/admin/cancel/10")
        assert res.status_code == 302
        assert "/admin" in res.location
        assert "completed_at = CURRENT_TIMESTAMP" in executed_queries[0][0]
        assert "owner_admin_id = COALESCE(r.owner_admin_id, %s)" in executed_queries[0][0]


def test_admin_history_allows_null_owner_admin_id_and_returns_completed_at(app_module, monkeypatch):
    executed_queries = []

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            executed_queries.append((query, params))

        def fetchall(self):
            if executed_queries and "FROM reservations" in executed_queries[-1][0]:
                # 10 columns: id, display_no, status, name, type_id, created_at, call_origin, called_at, completed_at, service_duration
                from datetime import datetime, timezone
                now = datetime.now(timezone.utc)
                return [(1, 1, "done", "一般", 1, now, "manual", now, now, 60.0)]
            return []

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

    monkeypatch.setattr(app_module, "is_admin_authenticated", lambda: True)
    monkeypatch.setattr(app_module.admin_routes, "is_admin_authenticated", lambda: True)
    monkeypatch.setattr(app_module, "get_current_admin_account_id", lambda: 1)
    monkeypatch.setattr(app_module.admin_routes, "get_current_admin_account_id", lambda: 1)
    monkeypatch.setattr(app_module, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.admin_routes, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.database, "get_connection", lambda: FakeConnection())
    monkeypatch.setattr(app_module.queue_service, "get_connection", lambda: FakeConnection())

    with app_module.app.test_client() as client:
        res = client.get("/admin/history")
        assert res.status_code == 200
        history_query = executed_queries[0][0]
        assert "OR COALESCE(r.owner_admin_id, t.owner_admin_id) IS NULL" in history_query
