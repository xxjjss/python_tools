#!/usr/bin/env python3
"""Shared mail downloading and MIME serialization helpers."""

import base64
import email
import imaplib
import json
import logging
import os
import re
import sys
from email.header import decode_header
from email.message import Message
from email.utils import parseaddr
from pathlib import Path


OUTPUT_ROOT = Path("~/.log/email/download").expanduser()
LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def enable_terminal_logging(logger):
    """Add an idempotent terminal handler to a provider logger."""
    if any(getattr(handler, "_email_terminal_handler", False) for handler in logger.handlers):
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
    handler._email_terminal_handler = True
    logger.addHandler(handler)


def decode_mime(value):
    """Decode an RFC 2047 header into readable text."""
    if not value:
        return ""
    parts = []
    for content, charset in decode_header(value):
        if isinstance(content, bytes):
            try:
                parts.append(content.decode(charset or "utf-8", errors="replace"))
            except LookupError:
                parts.append(content.decode("utf-8", errors="replace"))
        else:
            parts.append(content)
    return "".join(parts)


def safe_filename(value, fallback="unnamed"):
    """Make a header value safe to use as one filename component."""
    value = re.sub(r"[\x00-\x1f\x7f]", "_", value or "")
    value = re.sub(r"[\\/:*?\"<>|]+", "_", value)
    value = re.sub(r"\s+", "_", value).strip(" ._")
    value = re.sub(r"_+", "_", value)
    return value[:180] or fallback


def resolve_token(token_env_name):
    """Read a mail credential from the selected environment variable."""
    token = os.environ.get(token_env_name)
    return token.strip() if token and token.strip() else None


def decode_part_payload(part):
    payload = part.get_payload(decode=True)
    if payload is None:
        raw_payload = part.get_payload()
        return raw_payload if isinstance(raw_payload, str) else ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except (LookupError, UnicodeDecodeError):
        return payload.decode("utf-8", errors="replace")


def extract_message(message: Message, raw_message):
    """Convert one MIME message into JSON-compatible data."""
    sender_header = decode_mime(message.get("From", ""))
    sender_name, sender_email = parseaddr(sender_header)
    subject = decode_mime(message.get("Subject", ""))
    body_parts = []
    attachments = []

    for part in message.walk():
        if part.is_multipart():
            continue
        filename = decode_mime(part.get_filename())
        disposition = (part.get_content_disposition() or "").lower()
        is_attachment = bool(filename) or disposition == "attachment"
        if is_attachment:
            payload = part.get_payload(decode=True) or b""
            attachments.append(
                {
                    "filename": filename or "attachment",
                    "content_type": part.get_content_type(),
                    "size": len(payload),
                    "data_base64": base64.b64encode(payload).decode("ascii"),
                }
            )
            continue
        if part.get_content_type() == "text/plain":
            body_parts.append(decode_part_payload(part))
        elif part.get_content_type() == "text/html" and not body_parts:
            body_parts.append(decode_part_payload(part))

    return {
        "message_id": message.get("Message-ID", "").strip(),
        "date": message.get("Date", "").strip(),
        "sender": sender_email or sender_header,
        "sender_name": sender_name,
        "subject": subject,
        "body": "\n\n".join(body_parts),
        "attachments": attachments,
        "raw_eml_base64": base64.b64encode(raw_message).decode("ascii"),
    }


def unique_path(directory, sender, subject):
    base = f"{safe_filename(sender, 'unknown-sender')}-{safe_filename(subject, 'no-subject')}"
    return directory / f"{base}.json"


def read_day(email_address, target_date, imap_host, token_env_name, logger, provider_name):
    token = resolve_token(token_env_name)
    if not token:
        raise RuntimeError(
            f"未找到 {email_address} 的 {provider_name} token 环境变量: {token_env_name}"
        )

    output_dir = OUTPUT_ROOT / f"{email_address}-{target_date.isoformat()}"
    output_dir.mkdir(parents=True, exist_ok=True)
    connection = imaplib.IMAP4_SSL(imap_host)
    try:
        connection.login(email_address, token)
        status, _ = connection.select("INBOX", readonly=True)
        if status != "OK":
            raise RuntimeError(f"无法读取 {provider_name} INBOX")

        imap_date = target_date.strftime("%d-%b-%Y")
        status, data = connection.uid("search", None, f"ON {imap_date}")
        if status != "OK":
            raise RuntimeError(f"搜索邮件失败: {data!r}")

        message_uids = data[0].split() if data and data[0] else []
        logger.info("找到 %d 封邮件，日期: %s", len(message_uids), target_date.isoformat())
        saved = 0
        for message_uid in message_uids:
            status, fetch_data = connection.uid("fetch", message_uid, "(RFC822)")
            if status != "OK":
                logger.warning("读取 UID %s 失败", message_uid.decode(errors="replace"))
                continue
            raw_message = next(
                (item[1] for item in fetch_data if isinstance(item, tuple) and len(item) > 1),
                None,
            )
            if not raw_message:
                logger.warning("UID %s 没有 RFC822 数据", message_uid.decode(errors="replace"))
                continue
            message = email.message_from_bytes(raw_message)
            record = extract_message(message, raw_message)
            path = unique_path(output_dir, record["sender"], record["subject"])
            path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            saved += 1
            logger.info("已保存: %s", path)
        return saved
    finally:
        try:
            connection.close()
        except imaplib.IMAP4.error:
            pass
        connection.logout()
