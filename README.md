# Muse QQ Mail Connector (prototype)

A single-account, self-hosted REST bridge for a **personal QQ Mail inbox**. It can list recent messages, search the sender/subject of the latest 300 messages, read a plain-text email, and optionally send an email. No third-party Python packages are required. This is a prototype API intended for Meta Muse connector onboarding, **not an approved or published Muse integration**.

Meta's [Muse Connector Platform](https://muse.ai/platform) accepts submissions for review. Meta's [developer information](https://dev.meta.ai/products/connectors) describes REST API onboarding and access limits. GitHub publication of this repository does not put a connector into the Muse directory. A production, multi-user connector would additionally need individual account linking, isolation, hosted HTTPS, and Meta's review. This single-user bridge keeps the QQ Mail authorization code on your own server.

## Setup

Use Python 3.11 or later. In the QQ Mail web interface, open **Settings → Account**, enable IMAP/SMTP, and generate a **client authorization code**. Use that code here, **never your QQ login password**. The personal QQ Mail endpoints are `imap.qq.com:993` and `smtp.qq.com:465`, both over TLS. Enterprise QQ Mail is a different service and is not supported by this project.

```bash
cp .env.example .env
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
```

Edit `.env` with your address, authorization code and the random `CONNECTOR_TOKEN` just generated. The program does not automatically load `.env`; on a Linux shell, load it privately for the current process:

```bash
set -a
source .env
set +a
python3 qq_mail_connector.py
```

The API listens on `127.0.0.1:8787` by default. Never commit `.env` (it is gitignored). If Meta's onboarding needs a public endpoint, put it behind **HTTPS** and access control on your own server; expose only to intended clients. Binding `CONNECTOR_HOST=0.0.0.0` without a correctly configured HTTPS reverse proxy sends the bearer token and message data over plain HTTP. For a Linux service, supply the variables through a root-readable service environment file rather than embedding them in a unit committed to Git.

## API

Pass `Authorization: Bearer <CONNECTOR_TOKEN>` on every request, including health checks. Responses are JSON; no browser CORS headers are emitted. The included [OpenAPI 3 specification](openapi.yaml) describes the endpoints for connector onboarding.

```bash
curl -H "Authorization: Bearer $CONNECTOR_TOKEN" 'http://127.0.0.1:8787/messages?limit=10&query=invoice'
curl -H "Authorization: Bearer $CONNECTOR_TOKEN" 'http://127.0.0.1:8787/messages/12345'
```

| Operation | Behavior |
| --- | --- |
| `GET /health` | Process health; does not log in to QQ Mail. |
| `GET /messages?limit=10&query=text` | Latest 1–50 matching messages, searched across the latest 300 inbox messages' sender and subject. Empty query lists latest messages. |
| `GET /messages/{uid}` | Read one message's plain-text body, up to 30,000 characters. Does not mark it read. HTML and attachments are not returned. Messages larger than 5 MB are rejected. |
| `POST /messages/send` | Optional sending, disabled by default. Set `QQ_MAIL_ALLOW_SEND=true` and provide `confirm_send: true` after reviewing recipients and content. |

Example send body (enable sending only if you need it):

```json
{"to":["person@example.com"],"subject":"Hello","body":"Hi!","confirm_send":true}
```

The bridge requires explicit `confirm_send`, but the caller must still ensure the **human user actually approved** the message; this field is not a substitute for a client-side approval flow. Do not give the bearer token to an untrusted agent. A single authorization code and bearer token grant access to one mailbox, so this bridge must not be presented as a general multi-user connector.

## Test

```bash
python3 -m unittest discover -s tests -v
```

Tests mock IMAP and SMTP; they do not access your account. Live mail access requires your own credentials and network reachability from your server. If QQ Mail rejects login, confirm IMAP/SMTP is enabled and regenerate its authorization code.
