"""A single-account, self-hosted QQ Mail REST bridge for agent connectors."""

from __future__ import annotations

import hmac
import imaplib
import json
import os
import re
import smtplib
import ssl
from email import policy
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parseaddr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


MAX_MESSAGE_BYTES = 5_000_000
MAX_BODY_CHARS = 30_000


def config() -> dict[str, str]:
    required = ("QQ_MAIL_ADDRESS", "QQ_MAIL_AUTH_CODE", "CONNECTOR_TOKEN")
    missing = [key for key in required if not os.environ.get(key)]
    if missing:
        raise ValueError("Missing required environment variables: " + ", ".join(missing))
    return {key: os.environ[key] for key in required}


def decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except (UnicodeError, ValueError):
        return value


def parse_email(value: str) -> str:
    if not isinstance(value, str) or any(c in value for c in "\r\n,;"):
        raise ValueError("Invalid email address")
    display, address = parseaddr(value)
    if display or address != value.strip() or not re.fullmatch(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+", address):
        raise ValueError("Invalid email address")
    return address


def imap_connect(settings: dict[str, str]):
    conn = imaplib.IMAP4_SSL("imap.qq.com", 993, ssl_context=ssl.create_default_context(), timeout=20)
    try:
        conn.login(settings["QQ_MAIL_ADDRESS"], settings["QQ_MAIL_AUTH_CODE"])
        status, _ = conn.select("INBOX", readonly=True)
        if status != "OK":
            raise RuntimeError("Cannot open inbox")
        return conn
    except Exception:
        conn.logout()
        raise


def response_bytes(data) -> bytes:
    for item in data or []:
        if isinstance(item, tuple) and isinstance(item[1], bytes):
            return item[1]
    raise RuntimeError("Unexpected IMAP response")


def summary(uid: str, raw: bytes) -> dict:
    message = BytesParser(policy=policy.default).parsebytes(raw)
    return {
        "uid": uid,
        "from": decode(str(message.get("From", ""))),
        "to": decode(str(message.get("To", ""))),
        "subject": decode(str(message.get("Subject", ""))),
        "date": str(message.get("Date", "")),
    }


def list_messages(settings: dict[str, str], limit: int, query: str) -> list[dict]:
    conn = imap_connect(settings)
    try:
        status, data = conn.uid("search", None, "ALL")
        if status != "OK":
            raise RuntimeError("Cannot list messages")
        uids = (data[0] or b"").split()[-300:]
        results = []
        for uid in reversed(uids):
            status, data = conn.uid("fetch", uid, "(BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE)])")
            if status != "OK":
                continue
            item = summary(uid.decode("ascii"), response_bytes(data))
            if query.casefold() in (item["subject"] + " " + item["from"]).casefold():
                results.append(item)
            if len(results) >= limit:
                break
        return results
    finally:
        conn.logout()


def body_text(message) -> str:
    if message.is_multipart():
        parts = [part for part in message.walk() if not part.is_multipart() and part.get_content_disposition() != "attachment"]
    else:
        parts = [message]
    for part in parts:
        if part.get_content_type() == "text/plain":
            try:
                return str(part.get_content())[:MAX_BODY_CHARS]
            except (UnicodeError, LookupError):
                continue
    return ""  # HTML and attachments are intentionally not exposed.


def get_message(settings: dict[str, str], uid: str) -> dict | None:
    conn = imap_connect(settings)
    try:
        status, data = conn.uid("fetch", uid, "(RFC822.SIZE)")
        if status != "OK" or not data or data[0] is None:
            return None
        size = re.search(rb"RFC822\.SIZE (\d+)", data[0][0] if isinstance(data[0], tuple) else data[0])
        if not size:
            return None
        if int(size.group(1)) > MAX_MESSAGE_BYTES:
            raise ValueError("Message exceeds 5 MB read limit")
        status, data = conn.uid("fetch", uid, "(BODY.PEEK[])")
        if status != "OK":
            return None
        raw = response_bytes(data)
        if len(raw) > MAX_MESSAGE_BYTES:
            raise ValueError("Message exceeds 5 MB read limit")
        message = BytesParser(policy=policy.default).parsebytes(raw)
        return {**summary(uid, raw), "body": body_text(message), "has_attachments": any(
            part.get_content_disposition() == "attachment" for part in message.walk()
        )}
    finally:
        conn.logout()


def send_message(settings: dict[str, str], payload: dict) -> dict:
    if os.environ.get("QQ_MAIL_ALLOW_SEND", "false").lower() != "true":
        raise PermissionError("Sending is disabled; set QQ_MAIL_ALLOW_SEND=true to enable")
    if payload.get("confirm_send") is not True:
        raise ValueError("confirm_send must be true after the user approves recipients and content")
    recipients = payload.get("to")
    if not isinstance(recipients, list) or not 1 <= len(recipients) <= 10:
        raise ValueError("to must contain 1 to 10 email addresses")
    recipients = [parse_email(value) for value in recipients]
    subject, body = payload.get("subject"), payload.get("body")
    if not isinstance(subject, str) or not subject.strip() or len(subject) > 200 or "\r" in subject or "\n" in subject:
        raise ValueError("subject must be 1 to 200 characters on one line")
    if not isinstance(body, str) or not body.strip() or len(body) > MAX_BODY_CHARS:
        raise ValueError("body must be 1 to 30000 characters")
    mail = EmailMessage()
    mail["From"] = settings["QQ_MAIL_ADDRESS"]
    mail["To"] = ", ".join(recipients)
    mail["Subject"] = subject
    mail.set_content(body)
    with smtplib.SMTP_SSL("smtp.qq.com", 465, context=ssl.create_default_context(), timeout=20) as smtp:
        smtp.login(settings["QQ_MAIL_ADDRESS"], settings["QQ_MAIL_AUTH_CODE"])
        smtp.send_message(mail)
    return {"sent": True, "to": recipients}


class Handler(BaseHTTPRequestHandler):
    settings: dict[str, str] = {}

    def log_message(self, format, *args):
        # Avoid logging URLs, headers, email addresses, or message bodies.
        pass

    def reply(self, status: int, result: dict):
        output = json.dumps(result, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(output)))
        self.end_headers()
        self.wfile.write(output)

    def authorized(self) -> bool:
        token = self.headers.get("Authorization", "")
        if not hmac.compare_digest(token, "Bearer " + self.settings["CONNECTOR_TOKEN"]):
            self.reply(401, {"error": "Unauthorized"})
            return False
        return True

    def do_GET(self):
        if not self.authorized():
            return
        try:
            parsed = urlsplit(self.path)
            if parsed.path == "/health":
                self.reply(200, {"status": "ok"})
            elif parsed.path == "/messages":
                params = parse_qs(parsed.query)
                limit = int(params.get("limit", ["10"])[0])
                query = params.get("query", [""])[0]
                if not 1 <= limit <= 50 or len(query) > 100:
                    raise ValueError("limit must be 1 to 50 and query at most 100 characters")
                self.reply(200, {"messages": list_messages(self.settings, limit, query)})
            elif re.fullmatch(r"/messages/[0-9]{1,20}", parsed.path):
                message = get_message(self.settings, parsed.path.rsplit("/", 1)[1])
                self.reply(200, message) if message else self.reply(404, {"error": "Message not found"})
            else:
                self.reply(404, {"error": "Not found"})
        except (ValueError, UnicodeError) as exc:
            self.reply(400, {"error": str(exc)})
        except Exception:
            self.reply(502, {"error": "Mail server request failed"})

    def do_POST(self):
        if not self.authorized():
            return
        if urlsplit(self.path).path != "/messages/send":
            self.reply(404, {"error": "Not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 100_000:
                raise ValueError("Request must be at most 100 KB")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("Expected JSON object")
            self.reply(200, send_message(self.settings, payload))
        except PermissionError as exc:
            self.reply(403, {"error": str(exc)})
        except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
            self.reply(400, {"error": str(exc)})
        except Exception:
            self.reply(502, {"error": "Mail server request failed"})


def main():
    Handler.settings = config()
    host = os.environ.get("CONNECTOR_HOST", "127.0.0.1")
    port = int(os.environ.get("CONNECTOR_PORT", "8787"))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"QQ Mail connector listening on {host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
