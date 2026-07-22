"""Connecting somebody's mailbox, whoever hosts it.

Three ways in, one shape out:

  - **microsoft** - Graph, OAuth authorization code flow. Multitenant, so any
    Microsoft 365 organisation can consent without being registered here
    first.
  - **google** - Gmail API, same flow, different endpoints. Note that Google
    treats Gmail scopes as *restricted*: a production app needs verification
    plus an annual security assessment, and is capped at a handful of test
    users until that clears. The code works the moment those are done; the
    queue is Google's, not ours.
  - **imap** - anything else. Company mail on cPanel, Zoho, Fastmail, Yahoo,
    or Gmail via an app password. No vendor approval, no consent screen, but
    it means holding a password rather than a revocable token - which is why
    nothing here stores a credential unless secretstore has a working key.

Not called `providers.py` - that name is already taken by the AI model
providers, and confusing the two would be easy.

Every provider exposes the same three things to the rest of the app:

    connect_*(...)        -> CONNECTION dict, ready to store
    refresh(cfg, creds)   -> fresh creds, or None if nothing needed doing
    list_recent(...)      -> [{id, subject, from, received, unread}]

Stdlib only apart from the encryption, which lives in secretstore.
"""

from __future__ import annotations

import email.header
import email.utils
import base64
import html.parser
import imaplib
import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

EXPIRY_SKEW_SECONDS = 120
MAX_BODY_CHARS = 20_000
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024

PROVIDERS = ("microsoft", "google", "imap")

LABELS = {
    "microsoft": "Outlook / Microsoft 365",
    "google": "Gmail / Google Workspace",
    "imap": "Webmail / IMAP",
}


class MailboxError(Exception):
    """A user-facing mailbox problem - not configured, denied, expired."""


# --------------------------------------------------------------- OAuth shared
#
# Microsoft and Google differ in endpoints, scope names and one query
# parameter each; everything else about the dance is identical, so it lives
# once here rather than twice.

OAUTH = {
    "microsoft": {
        # /common is the audience the app registration is set to: any Entra ID
        # tenant *and* personal Microsoft accounts. If that's ever narrowed to
        # organisations only, this has to become /organizations - otherwise a
        # personal account gets through the account picker and fails after.
        "authorize": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "token": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        # offline_access is what makes a refresh token come back; without it
        # the connection quietly dies about an hour after it's made.
        "scopes": ["offline_access", "User.Read", "Mail.ReadWrite"],
        "client_id_key": "AZURE_CLIENT_ID",
        "client_secret_key": "AZURE_CLIENT_SECRET",
        # Someone already signed in to the wrong account would otherwise be
        # connected silently, with no way to tell.
        "extra_authorize": {"prompt": "select_account"},
    },
    "google": {
        "authorize": "https://accounts.google.com/o/oauth2/v2/auth",
        "token": "https://oauth2.googleapis.com/token",
        "scopes": [
            "https://www.googleapis.com/auth/gmail.modify",
            "https://www.googleapis.com/auth/userinfo.email",
        ],
        "client_id_key": "GOOGLE_OAUTH_CLIENT_ID",
        "client_secret_key": "GOOGLE_OAUTH_CLIENT_SECRET",
        # Google only returns a refresh token on the first consent unless
        # both of these are set - and a connection with no refresh token is
        # one that breaks within the hour.
        "extra_authorize": {"access_type": "offline", "prompt": "consent"},
    },
}

REDIRECT_KEYS = {
    "microsoft": "AZURE_REDIRECT_URI",
    "google": "GOOGLE_REDIRECT_URI",
}


def is_configured(cfg: dict, provider: str) -> bool:
    """Whether this deployment can offer this provider at all."""
    if provider == "imap":
        return True  # nothing to register with anyone
    spec = OAUTH.get(provider)
    if not spec:
        return False
    return bool(cfg.get(spec["client_id_key"], "").strip()
                and cfg.get(spec["client_secret_key"], "").strip())


def available(cfg: dict) -> list[str]:
    return [p for p in PROVIDERS if is_configured(cfg, p)]


def redirect_uri(cfg: dict, provider: str) -> str:
    """Must match the registered redirect character for character - hence
    configuration, not something derived from the request's Host header,
    which is attacker-supplied."""
    return cfg.get(REDIRECT_KEYS.get(provider, ""), "").strip()


def authorize_url(cfg: dict, provider: str, state: str, *, login_hint: str = "") -> str:
    spec = OAUTH[provider]
    params = {
        "client_id": cfg[spec["client_id_key"]].strip(),
        "response_type": "code",
        "redirect_uri": redirect_uri(cfg, provider),
        "scope": " ".join(spec["scopes"]),
        "state": state,
        **spec["extra_authorize"],
    }
    if login_hint:
        params["login_hint"] = login_hint
    return spec["authorize"] + "?" + urllib.parse.urlencode(params)


def _post_form(url: str, form: dict, what: str) -> dict:
    req = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(form).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            # error_description is a paragraph with a correlation id in it -
            # useful in a log, far too much for a dashboard banner.
            detail = (json.loads(exc.read().decode("utf-8")) or {}).get("error") or ""
        except Exception:
            pass
        raise MailboxError(f"{what} was rejected ({detail or exc.code}). Try again.") from exc
    except urllib.error.URLError as exc:
        raise MailboxError(f"Could not reach the mail provider to {what.lower()}.") from exc


def _api_get(access_token: str, url: str, params: dict | None = None,
             headers: dict | None = None) -> dict:
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {access_token}", **(headers or {})
    }, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise MailboxError("The mailbox connection has expired.") from exc
        if exc.code == 403:
            raise MailboxError(
                "The mail provider refused that request - the connection may "
                "not have the permissions it needs. Reconnecting asks for "
                "them again."
            ) from exc
        raise MailboxError(f"The mail provider returned an error ({exc.code}).") from exc
    except urllib.error.URLError as exc:
        raise MailboxError("Could not reach the mail provider.") from exc


def exchange_code(cfg: dict, provider: str, code: str) -> dict:
    """Swap the one-time callback code for tokens, and identify the account.

    Returns the CONNECTION dict the caller stores: non-secret metadata plus a
    `credentials` dict that gets encrypted before it touches the database.
    """
    spec = OAUTH[provider]
    payload = _post_form(spec["token"], {
        "client_id": cfg[spec["client_id_key"]].strip(),
        "client_secret": cfg[spec["client_secret_key"]].strip(),
        "code": code,
        "redirect_uri": redirect_uri(cfg, provider),
        "grant_type": "authorization_code",
    }, "The sign-in")

    if not payload.get("refresh_token"):
        raise MailboxError(
            "No refresh token came back, so the connection wouldn't survive "
            "the hour. Check the app registration's offline access settings, "
            "then remove this app from your account and connect again."
        )

    access = payload.get("access_token") or ""
    who = _whoami(provider, access)
    return {
        "provider": provider,
        "account_email": who["email"],
        "account_name": who["name"],
        "imap_host": "",
        "imap_port": 0,
        "scopes": payload.get("scope") or " ".join(spec["scopes"]),
        "credentials": {
            "refresh_token": payload["refresh_token"],
            "access_token": access,
            "expires_at": time.time() + float(payload.get("expires_in") or 3600),
        },
    }


def _whoami(provider: str, access_token: str) -> dict:
    if provider == "microsoft":
        me = _api_get(access_token, "https://graph.microsoft.com/v1.0/me")
        return {
            # mail is null on some accounts; userPrincipalName is reliable.
            "email": me.get("mail") or me.get("userPrincipalName") or "",
            "name": me.get("displayName") or "",
        }
    me = _api_get(access_token, "https://gmail.googleapis.com/gmail/v1/users/me/profile")
    return {"email": me.get("emailAddress") or "", "name": ""}


def refresh(cfg: dict, provider: str, creds: dict) -> dict | None:
    """Renew the access token if it's close to expiring.

    Returns updated credentials, or None when the current ones are still
    good. Raises MailboxError if the grant is gone for good - revoked,
    password changed, admin removed the app - which the caller treats as a
    disconnection rather than a retryable failure.
    """
    if provider == "imap":
        return None  # a password doesn't expire on a timer
    if creds.get("access_token") and creds.get("expires_at", 0) - EXPIRY_SKEW_SECONDS > time.time():
        return None
    spec = OAUTH[provider]
    payload = _post_form(spec["token"], {
        "client_id": cfg[spec["client_id_key"]].strip(),
        "client_secret": cfg[spec["client_secret_key"]].strip(),
        "refresh_token": creds["refresh_token"],
        "grant_type": "refresh_token",
    }, "The mailbox connection")
    return {
        # Providers rotate refresh tokens sometimes and not others; keep the
        # new one when offered, keep the old one when not.
        "refresh_token": payload.get("refresh_token") or creds["refresh_token"],
        "access_token": payload.get("access_token") or "",
        "expires_at": time.time() + float(payload.get("expires_in") or 3600),
    }


# ---------------------------------------------------------------------- IMAP


def _imap_connect(host: str, port: int, username: str, password: str) -> imaplib.IMAP4_SSL:
    try:
        conn = imaplib.IMAP4_SSL(host, port or 993, timeout=20,
                                 ssl_context=ssl.create_default_context())
    except (OSError, ssl.SSLError) as exc:
        raise MailboxError(
            f"Couldn't reach {host} on port {port or 993}. Check the server "
            "address and port."
        ) from exc
    try:
        conn.login(username, password)
    except imaplib.IMAP4.error as exc:
        try:
            conn.logout()
        except Exception:
            pass
        raise MailboxError(
            "That address and password were refused by the mail server. If "
            "the account uses two-factor authentication, you'll need an app "
            "password rather than the normal one."
        ) from exc
    return conn


def connect_imap(host: str, port: int, username: str, password: str) -> dict:
    """Verify IMAP details by actually logging in, then hand back a
    CONNECTION dict. Credentials that don't work never reach the database."""
    host = host.strip()
    username = username.strip()
    if not host or not username or not password:
        raise MailboxError("Server, email address and password are all needed.")
    conn = _imap_connect(host, port, username, password)
    try:
        conn.logout()
    except Exception:
        pass
    return {
        "provider": "imap",
        "account_email": username,
        "account_name": "",
        "imap_host": host,
        "imap_port": port or 993,
        "scopes": "imap",
        "credentials": {"password": password},
    }


def _decode_header(raw: str) -> str:
    """MIME-encoded headers (=?UTF-8?B?...?=) turned back into text."""
    if not raw:
        return ""
    out = []
    for chunk, enc in email.header.decode_header(raw):
        if isinstance(chunk, bytes):
            try:
                out.append(chunk.decode(enc or "utf-8", errors="replace"))
            except LookupError:
                out.append(chunk.decode("utf-8", errors="replace"))
        else:
            out.append(chunk)
    return "".join(out).strip()


class _TextFromHtml(html.parser.HTMLParser):
    """Small, dependency-free HTML-to-text converter for email bodies."""

    BREAKS = {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() in self.BREAKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self.BREAKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def text(self) -> str:
        lines = [" ".join(line.split()) for line in "".join(self.parts).splitlines()]
        return "\n".join(line for line in lines if line).strip()


def _plain_body(value: str, content_type: str = "text/plain") -> str:
    if not value:
        return ""
    if "html" in (content_type or "").lower():
        parser = _TextFromHtml()
        try:
            parser.feed(value)
            value = parser.text()
        except Exception:
            value = ""
    return value.replace("\x00", "").strip()[:MAX_BODY_CHARS]


def _mime_body(msg) -> str:
    plain, html = [], []
    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        kind = part.get_content_type()
        if kind not in ("text/plain", "text/html"):
            continue
        raw = part.get_payload(decode=True)
        if not raw:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            text = raw.decode(charset, errors="replace")
        except LookupError:
            text = raw.decode("utf-8", errors="replace")
        (plain if kind == "text/plain" else html).append(text)
    if plain:
        return _plain_body("\n\n".join(plain))
    return _plain_body("\n\n".join(html), "text/html")


def _mime_attachments(msg) -> list[dict]:
    """Return attachment metadata without placing file data in inbox JSON."""
    attachments = []
    parts = msg.walk() if msg.is_multipart() else [msg]
    for index, part in enumerate(parts):
        filename = _decode_header(part.get_filename() or "")
        if not filename and part.get_content_disposition() != "attachment":
            continue
        raw = part.get_payload(decode=True) or b""
        attachments.append({
            "part": index,
            "name": filename or f"attachment-{len(attachments) + 1}",
            "media_type": part.get_content_type() or "application/octet-stream",
            "size": len(raw),
            "downloadable": 0 < len(raw) <= MAX_ATTACHMENT_BYTES,
        })
    return attachments


def get_attachment(connection: dict, creds: dict, message_id: str,
                   part_index: int) -> dict:
    """Fetch one attachment from the signed-in user's IMAP inbox on demand."""
    if connection["provider"] != "imap":
        raise MailboxError("Attachment downloads are currently available for Webmail / IMAP.")
    if not message_id.isdigit() or part_index < 0:
        raise MailboxError("That attachment reference is invalid.")
    conn = _imap_connect(connection["imap_host"], connection["imap_port"],
                         connection["account_email"], creds["password"])
    try:
        conn.select("INBOX", readonly=True)
        typ, fetched = conn.fetch(message_id.encode("ascii"), "(BODY.PEEK[])")
        if typ != "OK" or not fetched or not isinstance(fetched[0], tuple):
            raise MailboxError("That email is no longer available in the inbox.")
        msg = email.message_from_bytes(fetched[0][1])
        parts = list(msg.walk() if msg.is_multipart() else [msg])
        if part_index >= len(parts):
            raise MailboxError("That attachment is no longer available.")
        part = parts[part_index]
        filename = _decode_header(part.get_filename() or "")
        if not filename and part.get_content_disposition() != "attachment":
            raise MailboxError("That message part is not an attachment.")
        raw = part.get_payload(decode=True) or b""
        if not raw:
            raise MailboxError("That attachment is empty.")
        if len(raw) > MAX_ATTACHMENT_BYTES:
            raise MailboxError("That attachment is larger than the 10 MB download limit.")
        return {"name": filename or "attachment",
                "media_type": part.get_content_type() or "application/octet-stream",
                "data": raw}
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def _gmail_part_body(payload: dict) -> str:
    plain, html = [], []

    def visit(part: dict) -> None:
        kind = part.get("mimeType") or ""
        data = (part.get("body") or {}).get("data")
        if data and kind in ("text/plain", "text/html"):
            try:
                raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
                text = raw.decode("utf-8", errors="replace")
                (plain if kind == "text/plain" else html).append(text)
            except Exception:
                pass
        for child in part.get("parts") or []:
            visit(child)

    visit(payload or {})
    if plain:
        return _plain_body("\n\n".join(plain))
    return _plain_body("\n\n".join(html), "text/html")


# ------------------------------------------------------------------- reading


def list_recent(cfg: dict, connection: dict, creds: dict, limit: int = 10,
                include_body: bool = False) -> list[dict]:
    """The most recent messages, in one shape whoever hosts the mailbox.

    The default is headers only. Triage opts into safe, length-limited plain
    text bodies; the lightweight Overview card does not download them.
    """
    provider = connection["provider"]
    if provider == "microsoft":
        data = _api_get(
            creds["access_token"],
            "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages",
            {"$top": limit,
             "$select": "subject,from,receivedDateTime,isRead" + (",body,bodyPreview" if include_body else ""),
             "$orderby": "receivedDateTime desc"},
            {"Prefer": 'outlook.body-content-type="text"'} if include_body else None,
        )
        out = []
        for m in data.get("value", []):
            sender = (m.get("from") or {}).get("emailAddress") or {}
            item = {
                "id": m.get("id", ""),
                "subject": m.get("subject") or "(no subject)",
                "from": sender.get("address") or sender.get("name") or "",
                "received": m.get("receivedDateTime") or "",
                "unread": not m.get("isRead", True),
            }
            if include_body:
                body = m.get("body") or {}
                item["body"] = (
                    _plain_body(body.get("content") or "", body.get("contentType") or "")
                    or _plain_body(m.get("bodyPreview") or "")
                )
            out.append(item)
        return out

    if provider == "google":
        listing = _api_get(
            creds["access_token"],
            "https://gmail.googleapis.com/gmail/v1/users/me/messages",
            {"maxResults": limit, "labelIds": "INBOX"},
        )
        out = []
        for ref in listing.get("messages", [])[:limit]:
            m = _api_get(
                creds["access_token"],
                f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{ref['id']}",
                ({"format": "full"} if include_body else
                 {"format": "metadata", "metadataHeaders": "Subject,From,Date"}),
            )
            headers = {h["name"].lower(): h["value"]
                       for h in (m.get("payload") or {}).get("headers", [])}
            item = {
                "id": m.get("id", ""),
                "subject": headers.get("subject") or "(no subject)",
                "from": headers.get("from") or "",
                "received": headers.get("date") or "",
                "unread": "UNREAD" in (m.get("labelIds") or []),
            }
            if include_body:
                item["body"] = _gmail_part_body(m.get("payload") or {}) or _plain_body(m.get("snippet") or "")
            out.append(item)
        return out

    conn = _imap_connect(connection["imap_host"], connection["imap_port"],
                         connection["account_email"], creds["password"])
    try:
        conn.select("INBOX", readonly=True)
        typ, data = conn.search(None, "ALL")
        if typ != "OK":
            return []
        ids = data[0].split()[-limit:][::-1]  # newest first
        unread = set()
        typ, undata = conn.search(None, "UNSEEN")
        if typ == "OK":
            unread = set(undata[0].split())
        out = []
        for mid in ids:
            query = "(BODY.PEEK[])" if include_body else "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE)])"
            typ, fetched = conn.fetch(mid, query)
            if typ != "OK" or not fetched or not isinstance(fetched[0], tuple):
                continue
            msg = email.message_from_bytes(fetched[0][1])
            item = {
                "id": mid.decode(),
                "subject": _decode_header(msg.get("Subject", "")) or "(no subject)",
                "from": _decode_header(msg.get("From", "")),
                "received": msg.get("Date", ""),
                "unread": mid in unread,
            }
            if include_body:
                item["body"] = _mime_body(msg)
                item["attachments"] = _mime_attachments(msg)
            out.append(item)
        return out
    finally:
        try:
            conn.logout()
        except Exception:
            pass
