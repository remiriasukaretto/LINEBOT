"""LINE Webhook（/callback）とメッセージ処理・予約ロジック。

注: main_routes.py等と同じ理由で、Flask Blueprintではなく素のビュー関数
として定義し、main.py側でapp.add_url_rule()により元のエンドポイント名で
登録する。handle_messageもhandler.add()をデコレータではなく関数呼び出しの
形でmain.py側で登録する。
"""
import logging
import time
from concurrent.futures import ThreadPoolExecutor

import psycopg2  # type: ignore
from flask import request, abort  # type: ignore
from linebot.v3 import WebhookHandler  # type: ignore
from linebot.v3.exceptions import InvalidSignatureError  # type: ignore

from config import (
    CHANNEL_SECRET,
    MAX_TYPE_NAME_LENGTH,
    MAX_USER_MESSAGE_CHARS,
    STATUS_WAITING,
    STATUS_CALLED,
    STATUS_CANCELLED,
    WEBHOOK_ASYNC_WORKERS,
)
from database import get_connection, is_accepting_new, is_webhook_rate_limited, get_accepting_type_names
import services.line_service as line_service
from services.line_service import build_type_image_url, send_flex_notice, send_reply_message
import services.queue_service as queue_service
from services.queue_service import (
    fmt_no,
    allocate_admin_reservation_no,
    calculate_wait_time_minutes,
    count_waiting_people_ahead_by_owner,
    refresh_wait_time_estimate,
)
from validators import normalize_type_name, validate_type_name

logger = logging.getLogger("line_routes")
logger.setLevel(logging.INFO)

handler = WebhookHandler(CHANNEL_SECRET)
_WEBHOOK_EXECUTOR = ThreadPoolExecutor(
    max_workers=WEBHOOK_ASYNC_WORKERS,
    thread_name_prefix="line-webhook",
)


def _process_webhook(webhook_handler, body: str, signature: str, ip: str) -> None:
    started_at = time.perf_counter()
    try:
        webhook_handler.handle(body, signature)
        result = "success"
    except Exception:
        result = "error"
        # 受付後の処理失敗は再送を誘発しないようログだけ記録する。
        logger.exception(
            "Failed to process LINE webhook event ip=%s signature=%s body_len=%s",
            ip,
            (signature or "")[:64],
            len(body) if body is not None else 0,
        )
    finally:
        logger.info(
            "metric=webhook_background duration_ms=%.2f result=%s body_len=%s",
            (time.perf_counter() - started_at) * 1000,
            result,
            len(body) if body is not None else 0,
        )


def callback():
    started_at = time.perf_counter()
    ip = request.remote_addr or "unknown"
    rate_limit_started_at = time.perf_counter()
    if is_webhook_rate_limited(ip):
        logger.info(
            "metric=webhook_request duration_ms=%.2f rate_limit_ms=%.2f result=rate_limited",
            (time.perf_counter() - started_at) * 1000,
            (time.perf_counter() - rate_limit_started_at) * 1000,
        )
        abort(429)
    rate_limit_ms = (time.perf_counter() - rate_limit_started_at) * 1000
    signature = request.headers.get("X-Line-Signature")
    if not signature:
        abort(400)
    body = request.get_data(as_text=True)
    validation_started_at = time.perf_counter()
    try:
        # 署名検証とJSON解析だけを同期で行い、予約処理は応答後に実行する。
        handler.parser.parse(body, signature, as_payload=True)
    except InvalidSignatureError:
        abort(400)
    except Exception:
        logger.exception(
            "Failed to validate LINE webhook event ip=%s signature=%s body_len=%s",
            ip,
            (signature or "")[:64],
            len(body) if body is not None else 0,
        )
        return "OK"
    validation_ms = (time.perf_counter() - validation_started_at) * 1000
    _WEBHOOK_EXECUTOR.submit(_process_webhook, handler, body, signature, ip)
    logger.info(
        "metric=webhook_request duration_ms=%.2f rate_limit_ms=%.2f validation_ms=%.2f result=accepted body_len=%s",
        (time.perf_counter() - started_at) * 1000,
        rate_limit_ms,
        validation_ms,
        len(body) if body is not None else 0,
    )
    return "OK"

IGNORED_REPLY_MESSAGE = "https://ukweb.ikura.workers.dev/"


def should_ignore_reply_message(message: str) -> bool:
    normalized = message.strip()
    return normalized in {IGNORED_REPLY_MESSAGE, "使い方"}


def handle_message(event):
    user_message = event.message.text.strip()
    if should_ignore_reply_message(user_message):
        return
    user_id = event.source.user_id
    try:
        process_reservation(event, user_id, user_message)
    except Exception:
        logger.exception(
            "Failed to process LINE message user_id=%s message=%s",
            user_id,
            user_message,
        )


def process_reservation(event, user_id, user_message):
    normalized = user_message.strip()
    if not normalized:
        send_flex_notice(
            event.reply_token,
            "ご案内",
            "メッセージを受け付けました。予約は「予約」、キャンセルは「キャンセル」、待ち時間は「待ち時間」と送信してください。",
        )
        return
    if len(normalized) > MAX_USER_MESSAGE_CHARS:
        send_flex_notice(
            event.reply_token,
            "エラー",
            f"メッセージは{MAX_USER_MESSAGE_CHARS}文字以内で送信してください。",
        )
        return

    with get_connection() as conn:
        with conn.cursor() as cur:
            if normalized.startswith("予約"):
                if not is_accepting_new():
                    send_flex_notice(
                        event.reply_token,
                        "予約停止中",
                        "現在、新規の予約受付は停止中です。",
                    )
                    return
                requested_type_name = normalize_type_name(normalized[2:])
                type_id = None
                type_name = None
                if requested_type_name:
                    if not validate_type_name(requested_type_name):
                        send_flex_notice(
                            event.reply_token,
                            "種類名エラー",
                            f"種類名は1〜{MAX_TYPE_NAME_LENGTH}文字で指定してください。\n例: 予約 相談",
                        )
                        return
                    cur.execute(
                        """
                            SELECT id, name, accepting, owner_admin_id, flavor_text, image_mime_type, image_version, price
                            FROM reservation_types
                            WHERE name = %s
                        """,
                        (requested_type_name,),
                    )
                    type_row = cur.fetchone()
                    if not type_row:
                        names = get_accepting_type_names(cur)
                        if names:
                            body = (
                                f"指定した種類「{requested_type_name}」は存在しません。\n利用可能: "
                                + " / ".join(names)
                            )
                        else:
                            body = "予約の種類がまだ登録されていません。管理画面で追加してください。"
                        send_flex_notice(event.reply_token, "種類がありません", body)
                        return
                    type_id = type_row[0]
                    type_name = type_row[1]
                    type_accepting = type_row[2]
                    type_owner_admin_id = type_row[3]
                    type_flavor_text = type_row[4]
                    type_image_mime_type = type_row[5]
                    type_image_version = type_row[6] if len(type_row) > 6 else 1
                    type_price = type_row[7] if len(type_row) > 7 else 0
                    type_owner_login_id = None
                    owner_accepting = True
                    if type_owner_admin_id is not None:
                        cur.execute(
                            "SELECT login_id, accepting_new, active FROM admin_accounts WHERE id = %s",
                            (type_owner_admin_id,),
                        )
                        owner_row = cur.fetchone()
                        if owner_row:
                            type_owner_login_id = owner_row[0]
                            owner_accepting = bool(owner_row[1]) and bool(owner_row[2])
                    type_image_url = build_type_image_url(type_id, type_image_version)
                    if not owner_accepting:
                        send_flex_notice(
                            event.reply_token,
                            "予約停止中",
                            "現在、新規の予約受付は停止中です。",
                        )
                        return
                    if not type_accepting:
                        names = get_accepting_type_names(cur)
                        if names:
                            body = (
                                f"「{type_name}」の新規受付は停止中です。\n利用可能: "
                                + " / ".join(names)
                            )
                        else:
                            body = f"「{type_name}」の新規受付は停止中です。"
                        send_flex_notice(event.reply_token, "受付停止", body)
                        return
                    if type_owner_admin_id is None:
                        send_flex_notice(
                            event.reply_token,
                            "受付不可",
                            "この種類は管理者に割り当てられていないため予約できません。管理者へお問い合わせください。",
                        )
                        return
                else:
                    cur.execute(
                        """
                            SELECT id, name, flavor_text, accepting, image_mime_type, image_version, price, owner_admin_id
                            FROM reservation_types
                            ORDER BY id ASC
                            LIMIT 10
                        """
                    )
                    type_rows = cur.fetchall()
                    owner_admin_ids = {
                        row[6] for row in type_rows if len(row) > 6 and row[6] is not None
                    }
                    owner_login_ids = {}
                    owner_accepting_states = {}
                    if owner_admin_ids:
                        cur.execute(
                            "SELECT id, login_id, accepting_new, active FROM admin_accounts WHERE id = ANY(%s)",
                            (list(owner_admin_ids),),
                        )
                        for row in cur.fetchall():
                            owner_login_ids[row[0]] = row[1]
                            owner_accepting_states[row[0]] = bool(row[2]) and bool(row[3])
                    if not type_rows:
                        send_flex_notice(
                            event.reply_token,
                            "種類がありません",
                            "現在、予約可能な種類が登録されていません。",
                        )
                        return

                    carousel_bubbles = []
                    for type_row in type_rows[:10]:
                        type_id = type_row[0]
                        name = type_row[1]
                        flavor_text = type_row[2]
                        accepting = type_row[3]
                        image_mime_type = type_row[4]
                        image_version = type_row[5] if len(type_row) > 5 else 1
                        price = type_row[6] if len(type_row) > 6 else 0
                        owner_admin_id = type_row[7] if len(type_row) > 7 else None
                        owner_login_id = owner_login_ids.get(owner_admin_id)
                        owner_accepting = owner_accepting_states.get(owner_admin_id, True)
                        effective_accepting = accepting and owner_accepting
                        image_url = (
                            build_type_image_url(type_id, image_version)
                            if image_mime_type
                            else None
                        )
                        # header box
                        header = {
                            "type": "box",
                            "layout": "vertical",
                            "backgroundColor": "#1e293b",
                            "contents": [
                                {
                                    "type": "text",
                                    "text": name,
                                    "weight": "bold",
                                    "size": "xl",
                                    "color": "#ffffff",
                                    "wrap": True,
                                }
                            ],
                            "paddingAll": "20px",
                        }
                        
                        # status pill
                        status_color = "#10b981" if effective_accepting else "#ef4444"
                        status_bg = "#d1fae5" if effective_accepting else "#fee2e2"
                        status_text = "受付中" if effective_accepting else "受付停止中"
                        
                        body_contents = [
                            {
                                "type": "box",
                                "layout": "horizontal",
                                "contents": [
                                    {
                                        "type": "box",
                                        "layout": "vertical",
                                        "backgroundColor": status_bg,
                                        "cornerRadius": "md",
                                        "paddingStart": "8px",
                                        "paddingEnd": "8px",
                                        "paddingTop": "2px",
                                        "paddingBottom": "2px",
                                        "contents": [
                                            {
                                                "type": "text",
                                                "text": status_text,
                                                "color": status_color,
                                                "size": "xs",
                                                "weight": "bold",
                                                "align": "center",
                                            }
                                        ],
                                    }
                                ],
                            }
                        ]
                        
                        body_contents.append(
                            {
                                "type": "text",
                                "text": f"設定者: {owner_login_id}" if owner_login_id else "設定者: 不明",
                                "wrap": True,
                                "size": "xs",
                                "color": "#64748b",
                                "margin": "sm",
                            }
                        )
                        body_contents.append(
                            {
                                "type": "text",
                                "text": flavor_text if flavor_text else "説明はありません。",
                                "wrap": True,
                                "size": "sm",
                                "color": "#475569" if flavor_text else "#94a3b8",
                                "style": "normal" if flavor_text else "italic",
                                "margin": "lg",
                            }
                        )
                        if price:
                            body_contents.append(
                                {
                                    "type": "box",
                                    "layout": "horizontal",
                                    "margin": "md",
                                    "contents": [
                                        {
                                            "type": "text",
                                            "text": "価格",
                                            "size": "sm",
                                            "color": "#64748b",
                                            "flex": 1,
                                        },
                                        {
                                            "type": "text",
                                            "text": f"{price:,}円",
                                            "size": "sm",
                                            "color": "#0f172a",
                                            "weight": "bold",
                                            "align": "end",
                                            "flex": 2,
                                        },
                                    ],
                                }
                            )
                        
                        body = {
                            "type": "box",
                            "layout": "vertical",
                            "contents": body_contents,
                            "paddingAll": "20px",
                        }
                        
                        # footer
                        if effective_accepting:
                            footer = {
                                "type": "box",
                                "layout": "vertical",
                                "contents": [
                                    {
                                        "type": "button",
                                        "action": {
                                            "type": "message",
                                            "label": "この種類で予約する",
                                            "text": f"予約 {name}",
                                        },
                                        "style": "primary",
                                        "color": "#0284c7",
                                    }
                                ],
                                "paddingAll": "10px",
                            }
                        else:
                            footer = {
                                "type": "box",
                                "layout": "vertical",
                                "contents": [
                                    {
                                        "type": "box",
                                        "layout": "vertical",
                                        "backgroundColor": "#f1f5f9",
                                        "cornerRadius": "md",
                                        "paddingTop": "10px",
                                        "paddingBottom": "10px",
                                        "contents": [
                                            {
                                                "type": "text",
                                                "text": "現在受付停止中",
                                                "color": "#94a3b8",
                                                "align": "center",
                                                "weight": "bold",
                                                "size": "sm",
                                            }
                                        ],
                                    }
                                ],
                                "paddingAll": "10px",
                            }
                        
                        bubble = {
                            "type": "bubble",
                            "size": "mega",
                            "header": header,
                            "body": body,
                            "footer": footer,
                        }
                        if image_url:
                            bubble["hero"] = {
                                "type": "image",
                                "url": image_url,
                                "size": "full",
                                "aspectRatio": "16:9",
                                "aspectMode": "cover",
                            }
                        carousel_bubbles.append(bubble)
                    
                    flex_msg = {
                        "type": "flex",
                        "altText": "予約の種類一覧",
                        "contents": {
                            "type": "carousel",
                            "contents": carousel_bubbles,
                        },
                    }
                    send_reply_message(event.reply_token, flex_msg)
                    return

                cur.execute(
                    """
                        SELECT r.id, COALESCE(r.reservation_no, r.id), r.status, r.type_id, t.name, COALESCE(r.owner_admin_id, t.owner_admin_id)
                        FROM reservations r
                        LEFT JOIN reservation_types t ON r.type_id = t.id
                        WHERE r.user_id = %s AND r.status IN (%s, %s)
                        ORDER BY r.id DESC LIMIT 1
                    """,
                    (user_id, STATUS_WAITING, STATUS_CALLED),
                )
                existing = cur.fetchone()
                if existing:
                    (
                        res_id,
                        display_no,
                        status,
                        existing_type_id,
                        existing_type_name,
                        existing_owner_admin_id,
                    ) = existing
                    if status == STATUS_WAITING:
                        if existing_owner_admin_id is not None:
                            waiting_people_ahead = count_waiting_people_ahead_by_owner(
                                cur,
                                reservation_id=res_id,
                                owner_admin_id=existing_owner_admin_id,
                            )
                            body = f"予約済みです。チケット番号: {fmt_no(display_no)} / 種類: {existing_type_name} / 待ち: {waiting_people_ahead}人"
                        else:
                            cur.execute(
                                "SELECT COUNT(*) FROM reservations WHERE status = %s AND owner_admin_id IS NULL AND id < %s",
                                (STATUS_WAITING, res_id),
                            )
                            body = f"予約済みです。チケット番号: {fmt_no(display_no)} / 待ち: {cur.fetchone()[0]}人"
                    elif status == STATUS_CALLED:
                        if existing_type_name:
                            body = f"【呼出中】チケット番号: {fmt_no(display_no)} / 種類: {existing_type_name} 会場へお越しください！"
                        else:
                            body = f"【呼出中】チケット番号: {fmt_no(display_no)} 会場へお越しください！"
                    send_flex_notice(event.reply_token, "予約状況", body)
                    return
                else:
                    try:
                        reservation_no = allocate_admin_reservation_no(
                            cur, type_owner_admin_id
                        )
                        cur.execute(
                            """
                                INSERT INTO reservations (
                                    user_id, message, type_id, owner_admin_id, reservation_no
                                ) VALUES (%s, %s, %s, %s, %s)
                                RETURNING id
                            """,
                            (user_id, "", type_id, type_owner_admin_id, reservation_no),
                        )
                        new_id = cur.fetchone()[0]
                    except psycopg2.IntegrityError:
                        conn.rollback()
                        cur.execute(
                            """
                                SELECT r.id, COALESCE(r.reservation_no, r.id), r.status, r.type_id, t.name, COALESCE(r.owner_admin_id, t.owner_admin_id)
                                FROM reservations r
                                LEFT JOIN reservation_types t ON r.type_id = t.id
                                WHERE r.user_id = %s AND r.status IN (%s, %s)
                                ORDER BY r.id DESC LIMIT 1
                            """,
                            (user_id, STATUS_WAITING, STATUS_CALLED),
                        )
                        existing_after_conflict = cur.fetchone()
                        if existing_after_conflict:
                            (
                                res_id,
                                display_no,
                                status,
                                _existing_type_id,
                                existing_type_name,
                                existing_owner_admin_id,
                            ) = existing_after_conflict
                            if status == STATUS_WAITING:
                                if existing_owner_admin_id is not None:
                                    waiting_people_ahead = (
                                        count_waiting_people_ahead_by_owner(
                                            cur,
                                            reservation_id=res_id,
                                            owner_admin_id=existing_owner_admin_id,
                                        )
                                    )
                                    body = f"予約済みです。チケット番号: {fmt_no(display_no)} / 種類: {existing_type_name} / 待ち: {waiting_people_ahead}人"
                                else:
                                    cur.execute(
                                        "SELECT COUNT(*) FROM reservations WHERE status = %s AND owner_admin_id IS NULL AND id < %s",
                                        (STATUS_WAITING, res_id),
                                    )
                                    body = f"予約済みです。チケット番号: {fmt_no(display_no)} / 待ち: {cur.fetchone()[0]}人"
                            elif status == STATUS_CALLED:
                                if existing_type_name:
                                    body = f"【呼出中】チケット番号: {fmt_no(display_no)} / 種類: {existing_type_name} 会場へお越しください！"
                                else:
                                    body = f"【呼出中】チケット番号: {fmt_no(display_no)} 会場へお越しください！"
                            send_flex_notice(event.reply_token, "予約状況", body)
                            return
                        raise
                    conn.commit()
                    logger.info(
                        "Created reservation %s by user %s type_id=%s",
                        new_id,
                        user_id,
                        type_id,
                    )
                    if type_owner_admin_id:
                        waiting_people_ahead = count_waiting_people_ahead_by_owner(
                            cur,
                            reservation_id=new_id,
                            owner_admin_id=type_owner_admin_id,
                        )
                        price_text = f" / 価格: {type_price:,}円" if type_price else ""
                        owner_text = (
                            f" / 設定者: {type_owner_login_id}"
                            if type_owner_login_id
                            else ""
                        )
                        body = f"【受付完了】チケット番号: {fmt_no(reservation_no)} / 種類: {type_name}{owner_text}{price_text} / 待ち: {waiting_people_ahead}人"
                    else:
                        cur.execute(
                            "SELECT COUNT(*) FROM reservations WHERE status = %s AND id < %s",
                            (STATUS_WAITING, new_id),
                        )
                        waiting_people_ahead = int(cur.fetchone()[0] or 0)
                        body = f"【受付完了】チケット番号: {fmt_no(reservation_no)} / 待ち: {waiting_people_ahead}人"
                    refresh_wait_time_estimate(owner_admin_id=type_owner_admin_id)
                    estimated_minutes = calculate_wait_time_minutes(
                        waiting_people_ahead
                    )
                    body += f"\n現在の目安待ち時間: {estimated_minutes}分"
                    send_flex_notice(
                        event.reply_token,
                        "受付完了",
                        body,
                        hero_url=type_image_url,
                    )
                    return
            elif normalized == "キャンセル":
                cur.execute(
                    """
                        UPDATE reservations SET status = %s, completed_at = CURRENT_TIMESTAMP
                        WHERE id = (
                            SELECT id FROM reservations
                            WHERE user_id = %s AND status IN (%s, %s)
                            ORDER BY id DESC LIMIT 1
                        )
                        RETURNING id, COALESCE(reservation_no, id)
                    """,
                    (STATUS_CANCELLED, user_id, STATUS_WAITING, STATUS_CALLED),
                )
                cancelled = cur.fetchone()
                if cancelled:
                    conn.commit()
                    cancelled_no = cancelled[1] if len(cancelled) > 1 else cancelled[0]
                    send_flex_notice(
                        event.reply_token,
                        "キャンセル完了",
                        f"受付チケット番号 {fmt_no(cancelled_no)} をキャンセルしました。",
                    )
                else:
                    send_flex_notice(
                        event.reply_token,
                        "キャンセル",
                        "キャンセル対象の予約はありません。",
                    )
                return
            elif normalized == "待ち時間":
                cur.execute(
                    """
                        SELECT r.id, COALESCE(r.reservation_no, r.id), r.status, t.name, COALESCE(r.owner_admin_id, t.owner_admin_id)
                        FROM reservations r
                        LEFT JOIN reservation_types t ON r.type_id = t.id
                        WHERE r.user_id = %s AND r.status IN (%s, %s)
                        ORDER BY r.id DESC LIMIT 1
                    """,
                    (user_id, STATUS_WAITING, STATUS_CALLED),
                )
                existing = cur.fetchone()
                if not existing:
                    send_flex_notice(
                        event.reply_token,
                        "待ち時間",
                        "待ち時間を確認できる予約がありません。まず「予約 種類名」と送信してください。",
                    )
                else:
                    res_id = existing[0]
                    display_no = existing[1]
                    status = existing[2]
                    type_name = existing[3] if len(existing) > 3 else None
                    owner_admin_id = existing[4] if len(existing) > 4 else None
                    if status == STATUS_WAITING:
                        if owner_admin_id is not None:
                            waiting_people_ahead = count_waiting_people_ahead_by_owner(
                                cur,
                                reservation_id=res_id,
                                owner_admin_id=owner_admin_id,
                            )
                        else:
                            cur.execute(
                                "SELECT COUNT(*) FROM reservations WHERE status = %s AND owner_admin_id IS NULL AND id < %s",
                                (STATUS_WAITING, res_id),
                            )
                            waiting_people_ahead = int(cur.fetchone()[0] or 0)
                        estimated_minutes = calculate_wait_time_minutes(
                            waiting_people_ahead
                        )
                        if type_name:
                            body = (
                                f"チケット番号: {fmt_no(display_no)} / 種類: {type_name} / あなたの前: {waiting_people_ahead}人"
                                f"\n現在の目安待ち時間: {estimated_minutes}分"
                            )
                        else:
                            body = (
                                f"チケット番号: {fmt_no(display_no)} / あなたの前: {waiting_people_ahead}人"
                                f"\n現在の目安待ち時間: {estimated_minutes}分"
                            )
                        send_flex_notice(event.reply_token, "待ち時間", body)
                    else:
                        send_flex_notice(
                            event.reply_token,
                            "呼出中",
                            f"【呼出中】チケット番号: {fmt_no(display_no)} です。会場へお越しください。",
                        )
                return
            else:
                send_flex_notice(
                    event.reply_token,
                    "ご案内",
                    "メッセージを受け付けました。予約は「予約」、キャンセルは「キャンセル」、待ち時間は「待ち時間」と送信してください。",
                )
