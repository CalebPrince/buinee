"""Connecting the other tools a team already works in.

`mailbox.py` does this for email, where each provider needs enough bespoke
handling (IMAP, Graph, the Gmail API) to be worth writing out by hand. The
tools here are the opposite case: eighteen vendors that all do the same
OAuth 2.0 authorization-code dance and differ only in endpoints, scope
names and a couple of quirks each. So they're data, not code - adding a
vendor is a new entry in CATALOG, not a new function.

Two things are deliberately separate and easy to confuse:

  - **Whether this deployment can offer a tool at all** - does it hold a
    client id and secret for that vendor? That's `is_configured`. No amount
    of code makes credentials appear; each vendor has to be registered with
    separately, and several (WhatsApp Business, Salesforce, Slack's
    directory) put a review queue in front of that.
  - **Whether a given company is entitled to it** - that's their plan, and
    it lives in the database where the platform owner can edit it, not
    here. See db.plan_tool_ids().

A tool the deployment can't offer is shown as unavailable; a tool the plan
doesn't include is shown as an upgrade. They're different messages because
they're different problems, and only one of them is the customer's.

The endpoints below were correct when written, but vendors move them and
rename scopes. Confirm each against that vendor's current documentation
when you register the app - the registration is manual anyway, so it's the
natural moment to check.

Stdlib only apart from the encryption, which lives in secretstore.
"""

from __future__ import annotations

import base64
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

EXPIRY_SKEW_SECONDS = 120

# One callback for every vendor. They each have to be told a redirect URI at
# registration time, but there's no reason it has to be a different one -
# which tool a callback belongs to is carried in the signed state, not in
# the URL, so it can't be swapped by whoever is holding the browser.
REDIRECT_KEY = "TOOLS_REDIRECT_URI"

CATEGORIES = (
    ("chat", "Chat and messaging"),
    ("files", "Files and storage"),
    ("projects", "Projects and tasks"),
    ("crm", "CRM and pipeline"),
    ("calendar", "Calendars and scheduling"),
)


class ToolError(Exception):
    """A user-facing problem connecting a tool - refused, expired, misconfigured."""


# --------------------------------------------------------------------- catalog
#
# Per entry:
#   label/category/blurb  what the dashboard shows.
#   auth                  "oauth2" - the shared dance below.
#                         "api_key" - the vendor has no usable OAuth2 flow, so
#                         the person pastes a token from their own account.
#   authorize/token       endpoints.
#   scopes                asked for at consent. Read-oriented on purpose: this
#                         product summarizes and cross-checks, it doesn't post.
#   token_auth            "post" (credentials in the form body) or "basic"
#                         (HTTP Basic), which some vendors require instead.
#   refresh               False where the vendor issues long-lived tokens and
#                         no refresh token, so a missing one isn't a fault.
#   extra_authorize       vendor-specific query parameters, usually the one
#                         that makes a refresh token actually come back.
#   whoami                optional {url, fields} to label the connection with
#                         the account it belongs to. Best-effort - a vendor
#                         that won't say must not cost us the connection.
#   note                  shown in the admin UI: what registering will involve.

CATALOG = (
    # ------------------------------------------------------------------ chat
    {
        "id": "slack", "label": "Slack", "category": "chat",
        "blurb": "Read channel activity so Ada can summarize what a team agreed.",
        "auth": "oauth2",
        "authorize": "https://slack.com/oauth/v2/authorize",
        "token": "https://slack.com/api/oauth.v2.access",
        "scopes": ["channels:read", "channels:history", "users:read", "team:read"],
        "token_auth": "post", "refresh": False,
        "whoami": {"url": "https://slack.com/api/auth.test", "name": "team", "account": "user"},
        "note": "Create a Slack app, add these bot scopes, then install it to the workspace.",
    },
    {
        "id": "teams", "label": "Microsoft Teams", "category": "chat",
        "blurb": "Read channel messages alongside the mail already being triaged.",
        "auth": "oauth2",
        "authorize": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "token": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "scopes": ["offline_access", "User.Read", "Team.ReadBasic.All",
                   "Channel.ReadBasic.All", "ChannelMessage.Read.All"],
        "token_auth": "post", "refresh": True,
        "extra_authorize": {"prompt": "select_account"},
        "whoami": {"url": "https://graph.microsoft.com/v1.0/me",
                   "name": "displayName", "account": "userPrincipalName"},
        "note": "Same Entra ID app registration as Outlook mail can be reused, with the Teams scopes added.",
    },
    {
        "id": "whatsapp", "label": "WhatsApp Business", "category": "chat",
        "blurb": "Read business-account conversations for customer follow-ups.",
        "auth": "oauth2",
        "authorize": "https://www.facebook.com/v21.0/dialog/oauth",
        "token": "https://graph.facebook.com/v21.0/oauth/access_token",
        "scopes": ["whatsapp_business_management", "business_management"],
        # Meta issues long-lived tokens rather than refresh tokens, and the
        # exchange is a different call entirely - so a connection here expires
        # on Meta's schedule and is reconnected, not silently renewed.
        "token_auth": "post", "refresh": False,
        "whoami": {"url": "https://graph.facebook.com/v21.0/me", "name": "name", "account": "id"},
        "note": "Needs a Meta app with WhatsApp Business, and Meta business verification before it leaves test mode.",
    },
    # ----------------------------------------------------------------- files
    {
        "id": "google_drive", "label": "Google Drive", "category": "files",
        "blurb": "Pull the spreadsheet or invoice a message refers to.",
        "auth": "oauth2",
        "authorize": "https://accounts.google.com/o/oauth2/v2/auth",
        "token": "https://oauth2.googleapis.com/token",
        "scopes": ["https://www.googleapis.com/auth/drive.readonly",
                   "https://www.googleapis.com/auth/userinfo.email"],
        "token_auth": "post", "refresh": True,
        "extra_authorize": {"access_type": "offline", "prompt": "consent"},
        "whoami": {"url": "https://www.googleapis.com/oauth2/v2/userinfo",
                   "name": "name", "account": "email"},
        "note": "Google Cloud OAuth client. drive.readonly is a sensitive scope and needs app verification for production.",
    },
    {
        "id": "onedrive", "label": "OneDrive / SharePoint", "category": "files",
        "blurb": "Reach documents kept in Microsoft 365 rather than attached to mail.",
        "auth": "oauth2",
        "authorize": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "token": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "scopes": ["offline_access", "User.Read", "Files.Read.All", "Sites.Read.All"],
        "token_auth": "post", "refresh": True,
        "extra_authorize": {"prompt": "select_account"},
        "whoami": {"url": "https://graph.microsoft.com/v1.0/me",
                   "name": "displayName", "account": "userPrincipalName"},
        "note": "Entra ID app registration. Sites.Read.All usually needs a tenant admin to consent.",
    },
    {
        "id": "dropbox", "label": "Dropbox", "category": "files",
        "blurb": "Read shared folders where scanned paperwork lands.",
        "auth": "oauth2",
        "authorize": "https://www.dropbox.com/oauth2/authorize",
        "token": "https://api.dropboxapi.com/oauth2/token",
        "scopes": ["files.metadata.read", "files.content.read", "account_info.read"],
        "token_auth": "post", "refresh": True,
        # Without this Dropbox returns a short-lived token and no refresh token.
        "extra_authorize": {"token_access_type": "offline"},
        "note": "Dropbox app console, scoped access. Submit for production once past the 50-user development cap.",
    },
    {
        "id": "notion", "label": "Notion", "category": "files",
        "blurb": "Read pages and databases a team keeps its process notes in.",
        "auth": "oauth2",
        "authorize": "https://api.notion.com/v1/oauth/authorize",
        "token": "https://api.notion.com/v1/oauth/token",
        "scopes": [],  # Notion grants per-page access at consent, not by scope.
        "token_auth": "basic", "refresh": False,
        "extra_authorize": {"owner": "user"},
        "note": "Public integration in Notion. The person picks which pages to share during consent.",
    },
    # -------------------------------------------------------------- projects
    {
        "id": "jira", "label": "Jira", "category": "projects",
        "blurb": "See the issue behind a request without leaving the workspace.",
        "auth": "oauth2",
        "authorize": "https://auth.atlassian.com/authorize",
        "token": "https://auth.atlassian.com/oauth/token",
        "scopes": ["read:jira-work", "read:jira-user", "offline_access"],
        "token_auth": "post", "refresh": True,
        "extra_authorize": {"audience": "api.atlassian.com", "prompt": "consent"},
        "whoami": {"url": "https://api.atlassian.com/me", "name": "name", "account": "email"},
        "note": "Atlassian developer console, OAuth 2.0 (3LO).",
    },
    {
        "id": "asana", "label": "Asana", "category": "projects",
        "blurb": "Match incoming work to the task already tracking it.",
        "auth": "oauth2",
        "authorize": "https://app.asana.com/-/oauth_authorize",
        "token": "https://app.asana.com/-/oauth_token",
        "scopes": ["default"],
        "token_auth": "post", "refresh": True,
        "note": "Asana developer console app.",
    },
    {
        "id": "clickup", "label": "ClickUp", "category": "projects",
        "blurb": "Read spaces and tasks for status without asking anyone.",
        "auth": "oauth2",
        "authorize": "https://app.clickup.com/api",
        "token": "https://api.clickup.com/api/v2/oauth/token",
        "scopes": [],
        "token_auth": "post", "refresh": False,
        "whoami": {"url": "https://api.clickup.com/api/v2/user", "name": "user.username", "account": "user.email"},
        "note": "ClickUp app. Tokens do not expire, so there is nothing to refresh.",
    },
    {
        "id": "monday", "label": "Monday.com", "category": "projects",
        "blurb": "Read boards so a status question answers itself.",
        "auth": "oauth2",
        "authorize": "https://auth.monday.com/oauth2/authorize",
        "token": "https://auth.monday.com/oauth2/token",
        "scopes": ["boards:read", "users:read"],
        "token_auth": "post", "refresh": True,
        "note": "monday.com developer centre app.",
    },
    {
        "id": "trello", "label": "Trello", "category": "projects",
        "blurb": "Read boards and cards for lighter-weight task tracking.",
        # Trello never moved to OAuth 2.0 - it's OAuth 1.0a, or an API key
        # plus a token the person generates themselves. The second is far
        # less machinery for a read-only connection, so that's what this is.
        "auth": "api_key",
        "note": "The person pastes a Trello API key and token from trello.com/app-key.",
    },
    # ------------------------------------------------------------------- crm
    {
        "id": "hubspot", "label": "HubSpot", "category": "crm",
        "blurb": "Line a conversation up against the deal it belongs to.",
        "auth": "oauth2",
        "authorize": "https://app.hubspot.com/oauth/authorize",
        "token": "https://api.hubapi.com/oauth/v1/token",
        "scopes": ["crm.objects.contacts.read", "crm.objects.deals.read",
                   "crm.objects.companies.read"],
        "token_auth": "post", "refresh": True,
        "note": "HubSpot public app. Scopes must match the app's configured scopes exactly.",
    },
    {
        "id": "salesforce", "label": "Salesforce", "category": "crm",
        "blurb": "Read accounts and opportunities for the same cross-check.",
        "auth": "oauth2",
        "authorize": "https://login.salesforce.com/services/oauth2/authorize",
        "token": "https://login.salesforce.com/services/oauth2/token",
        "scopes": ["api", "refresh_token"],
        "token_auth": "post", "refresh": True,
        "note": "Connected App. Sandboxes authorize against test.salesforce.com instead - a separate entry if you need both.",
    },
    {
        "id": "pipedrive", "label": "Pipedrive", "category": "crm",
        "blurb": "Read deals and contacts from a lighter CRM.",
        "auth": "oauth2",
        "authorize": "https://oauth.pipedrive.com/oauth/authorize",
        "token": "https://oauth.pipedrive.com/oauth/token",
        "scopes": ["deals:read", "contacts:read"],
        "token_auth": "basic", "refresh": True,
        "note": "Pipedrive marketplace app.",
    },
    # -------------------------------------------------------------- calendar
    {
        "id": "google_calendar", "label": "Google Calendar", "category": "calendar",
        "blurb": "Know what is already booked before a follow-up is promised.",
        "auth": "oauth2",
        "authorize": "https://accounts.google.com/o/oauth2/v2/auth",
        "token": "https://oauth2.googleapis.com/token",
        "scopes": ["https://www.googleapis.com/auth/calendar.readonly",
                   "https://www.googleapis.com/auth/userinfo.email"],
        "token_auth": "post", "refresh": True,
        "extra_authorize": {"access_type": "offline", "prompt": "consent"},
        "whoami": {"url": "https://www.googleapis.com/oauth2/v2/userinfo",
                   "name": "name", "account": "email"},
        "note": "Same Google Cloud OAuth client as Drive can be reused, with the calendar scope added.",
    },
    {
        "id": "outlook_calendar", "label": "Outlook Calendar", "category": "calendar",
        "blurb": "The same, for teams living in Microsoft 365.",
        "auth": "oauth2",
        "authorize": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "token": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "scopes": ["offline_access", "User.Read", "Calendars.Read"],
        "token_auth": "post", "refresh": True,
        "extra_authorize": {"prompt": "select_account"},
        "whoami": {"url": "https://graph.microsoft.com/v1.0/me",
                   "name": "displayName", "account": "userPrincipalName"},
        "note": "Entra ID app registration, Calendars.Read added.",
    },
    {
        "id": "calendly", "label": "Calendly", "category": "calendar",
        "blurb": "See booked meetings without a second tab open all day.",
        "auth": "oauth2",
        "authorize": "https://auth.calendly.com/oauth/authorize",
        "token": "https://auth.calendly.com/oauth/token",
        "scopes": [],
        "token_auth": "basic", "refresh": True,
        "whoami": {"url": "https://api.calendly.com/users/me",
                   "name": "resource.name", "account": "resource.email"},
        "note": "Calendly developer app. OAuth requires a paid Calendly plan on the customer's side.",
    },
)

BY_ID = {tool["id"]: tool for tool in CATALOG}
TOOL_IDS = tuple(tool["id"] for tool in CATALOG)
LABELS = {tool["id"]: tool["label"] for tool in CATALOG}
CATEGORY_LABELS = dict(CATEGORIES)


def client_id_key(tool_id: str) -> str:
    return f"TOOL_{tool_id.upper()}_CLIENT_ID"


def client_secret_key(tool_id: str) -> str:
    return f"TOOL_{tool_id.upper()}_CLIENT_SECRET"


def is_configured(cfg: dict, tool_id: str) -> bool:
    """Whether this deployment holds credentials for this vendor.

    Nothing to do with whether a customer's plan includes it - that's a
    database question, asked separately, and the two failures read very
    differently to the person looking at the screen.
    """
    tool = BY_ID.get(tool_id)
    if not tool:
        return False
    if tool["auth"] == "api_key":
        # Nothing to register: the person brings their own token.
        return True
    if not cfg.get(REDIRECT_KEY, "").strip():
        return False
    return bool(cfg.get(client_id_key(tool_id), "").strip()
                and cfg.get(client_secret_key(tool_id), "").strip())


def available(cfg: dict) -> list[str]:
    return [t for t in TOOL_IDS if is_configured(cfg, t)]


def redirect_uri(cfg: dict) -> str:
    """Configuration, not derived from the request's Host header - that's
    attacker-supplied, and it has to match the vendor registration exactly."""
    return cfg.get(REDIRECT_KEY, "").strip()


def authorize_url(cfg: dict, tool_id: str, state: str) -> str:
    tool = BY_ID[tool_id]
    params = {
        "client_id": cfg[client_id_key(tool_id)].strip(),
        "response_type": "code",
        "redirect_uri": redirect_uri(cfg),
        "state": state,
        **tool.get("extra_authorize", {}),
    }
    if tool["scopes"]:
        params["scope"] = " ".join(tool["scopes"])
    return tool["authorize"] + "?" + urllib.parse.urlencode(params)


# ----------------------------------------------------------------- HTTP plumbing

def _post_form(url: str, form: dict, *, basic: tuple[str, str] | None = None) -> dict:
    headers = {"Content-Type": "application/x-www-form-urlencoded",
               "Accept": "application/json"}
    if basic:
        pair = base64.b64encode(f"{basic[0]}:{basic[1]}".encode()).decode()
        headers["Authorization"] = f"Basic {pair}"
    req = urllib.request.Request(
        url, data=urllib.parse.urlencode(form).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            body = json.loads(exc.read().decode("utf-8")) or {}
            detail = str(body.get("error") or body.get("message") or "")
        except Exception:
            pass
        raise ToolError(f"The sign-in was rejected ({detail or exc.code}). Try again.") from exc
    except urllib.error.URLError as exc:
        raise ToolError("Could not reach that service to complete the sign-in.") from exc


def _api_get(access_token: str, url: str, headers: dict | None = None) -> dict:
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {access_token}", "Accept": "application/json",
        **(headers or {})}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise ToolError("That connection has expired. Reconnect it.") from exc
        raise ToolError(f"That service returned an error ({exc.code}).") from exc
    except urllib.error.URLError as exc:
        raise ToolError("Could not reach that service.") from exc


def _dig(payload: dict, path: str) -> str:
    """Read a dotted path out of a response, tolerating anything missing -
    this only ever feeds a display label."""
    node = payload
    for part in path.split("."):
        if not isinstance(node, dict):
            return ""
        node = node.get(part)
    return str(node) if isinstance(node, (str, int, float)) else ""


def _whoami(tool: dict, access_token: str) -> dict:
    """Label the connection with the account it was made from.

    Entirely best-effort. A vendor that won't identify the account, or that
    needs an extra scope to do it, must not cost somebody a connection that
    is otherwise working - so every failure here is swallowed.
    """
    spec = tool.get("whoami")
    if not spec:
        return {"name": "", "account": ""}
    try:
        headers = {"Notion-Version": "2022-06-28"} if tool["id"] == "notion" else None
        payload = _api_get(access_token, spec["url"], headers)
    except ToolError:
        return {"name": "", "account": ""}
    return {"name": _dig(payload, spec.get("name", "")),
            "account": _dig(payload, spec.get("account", ""))}


# --------------------------------------------------------------- the OAuth dance

def exchange_code(cfg: dict, tool_id: str, code: str) -> dict:
    """Swap the one-time callback code for tokens and identify the account.

    Returns the CONNECTION dict the caller stores: non-secret metadata plus
    a `credentials` dict that gets encrypted before it touches the database.
    """
    tool = BY_ID[tool_id]
    client_id = cfg[client_id_key(tool_id)].strip()
    client_secret = cfg[client_secret_key(tool_id)].strip()
    form = {"code": code, "redirect_uri": redirect_uri(cfg),
            "grant_type": "authorization_code"}
    basic = None
    if tool["token_auth"] == "basic":
        basic = (client_id, client_secret)
    else:
        form |= {"client_id": client_id, "client_secret": client_secret}

    payload = _post_form(tool["token"], form, basic=basic)

    # Slack answers 200 with {"ok": false} rather than an HTTP error.
    if payload.get("ok") is False:
        raise ToolError(f"The sign-in was rejected ({payload.get('error') or 'refused'}).")

    access = str(payload.get("access_token")
                 or (payload.get("authed_user") or {}).get("access_token") or "")
    if not access:
        raise ToolError("No access token came back from that service.")

    refresh_token = str(payload.get("refresh_token") or "")
    if tool["refresh"] and not refresh_token:
        raise ToolError(
            "No refresh token came back, so the connection wouldn't survive the "
            "hour. Check the app registration's offline access settings, then "
            "remove this app from your account and connect again.")

    who = _whoami(tool, access)
    return {
        "tool": tool_id,
        "account_label": who["account"] or who["name"] or tool["label"],
        "account_name": who["name"],
        "scopes": str(payload.get("scope") or " ".join(tool["scopes"])),
        "credentials": _credentials(payload, access, refresh_token, tool),
    }


def _credentials(payload: dict, access: str, refresh_token: str, tool: dict) -> dict:
    creds = {"access_token": access}
    if refresh_token:
        creds["refresh_token"] = refresh_token
    expires_in = payload.get("expires_in")
    # A vendor that issues non-expiring tokens says so by omitting this, and
    # inventing an expiry for it would force pointless reconnections.
    if expires_in:
        creds["expires_at"] = time.time() + float(expires_in)
    # Jira needs the cloud id for every subsequent call, and Salesforce the
    # instance url - both come back here and nowhere else.
    for extra in ("instance_url", "cloud_id", "bot_user_id", "workspace_id", "team"):
        if payload.get(extra):
            creds[extra] = payload[extra] if isinstance(payload[extra], str) else json.dumps(payload[extra])
    return creds


def connect_api_key(tool_id: str, key: str, token: str, label: str) -> dict:
    """The api_key tools: the person brings a credential from their own
    account instead of consenting to an app. Shaped identically to
    `exchange_code` so callers can't tell the difference."""
    tool = BY_ID.get(tool_id)
    if not tool or tool["auth"] != "api_key":
        raise ToolError("That tool is not connected with a key.")
    key, token, label = key.strip(), token.strip(), label.strip()
    if not key or not token:
        raise ToolError("Both the API key and the token are needed.")
    return {
        "tool": tool_id,
        "account_label": label or tool["label"],
        "account_name": label,
        "scopes": "",
        "credentials": {"api_key": key, "api_token": token},
    }


def refresh(cfg: dict, tool_id: str, creds: dict) -> dict | None:
    """Renew the access token if it's close to expiring.

    Returns fresh credentials to store, or None when nothing needed doing -
    which is the answer for every vendor that issues tokens that don't
    expire, not an error.
    """
    tool = BY_ID.get(tool_id)
    if not tool or not tool.get("refresh") or not creds.get("refresh_token"):
        return None
    expires_at = float(creds.get("expires_at") or 0)
    if expires_at and time.time() < expires_at - EXPIRY_SKEW_SECONDS:
        return None

    client_id = cfg.get(client_id_key(tool_id), "").strip()
    client_secret = cfg.get(client_secret_key(tool_id), "").strip()
    form = {"grant_type": "refresh_token", "refresh_token": creds["refresh_token"]}
    basic = None
    if tool["token_auth"] == "basic":
        basic = (client_id, client_secret)
    else:
        form |= {"client_id": client_id, "client_secret": client_secret}

    payload = _post_form(tool["token"], form, basic=basic)
    access = str(payload.get("access_token") or "")
    if not access:
        raise ToolError("That connection could not be renewed. Reconnect it.")
    fresh = dict(creds)
    fresh["access_token"] = access
    # Some vendors rotate the refresh token on every use; keeping the old one
    # would break the *next* renewal rather than this one, which is a much
    # harder fault to trace back to here.
    if payload.get("refresh_token"):
        fresh["refresh_token"] = str(payload["refresh_token"])
    if payload.get("expires_in"):
        fresh["expires_at"] = time.time() + float(payload["expires_in"])
    return fresh


# ------------------------------------------------------------------- reading
#
# Connecting a tool that then does nothing is just a stored token, so each
# vendor gets a reader as it comes online. They return one shape whatever the
# tool is - the dashboard and, later, Ada's context shouldn't have to know
# whether an item started life as a Slack message or a Dropbox file:
#
#     {id, title, subtitle, when, url}
#
# `when` is an ISO-8601 string or "" - vendors disagree wildly about time
# formats, so normalising once here beats every caller guessing.
#
# READERS is deliberately partial. A tool in CATALOG with no reader can still
# be connected; `can_read` says whether anything will come back, so the UI can
# be honest about it instead of showing a permanently empty list.

MAX_READ_ITEMS = 20


def _api_post_json(access_token: str, url: str, payload: dict,
                   headers: dict | None = None) -> dict:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={
        "Authorization": f"Bearer {access_token}", "Content-Type": "application/json",
        "Accept": "application/json", **(headers or {})}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise ToolError("That connection has expired. Reconnect it.") from exc
        raise ToolError(f"That service returned an error ({exc.code}).") from exc
    except urllib.error.URLError as exc:
        raise ToolError("Could not reach that service.") from exc


def _slack_ts(value) -> str:
    """Slack timestamps are "1701432000.001200" - epoch seconds with a
    sequence number glued on, which is not a time format anything else
    understands."""
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(str(value).split(".")[0])))
    except (TypeError, ValueError):
        return ""


def _read_slack(cfg: dict, creds: dict, limit: int) -> list[dict]:
    token = creds["access_token"]
    listing = _api_get(token, "https://slack.com/api/conversations.list"
                              "?types=public_channel&exclude_archived=true&limit=10")
    if listing.get("ok") is False:
        raise ToolError(f"Slack refused the request ({listing.get('error') or 'unknown'}).")
    # Only the channels the app was actually added to have history worth
    # asking for; the rest answer not_in_channel and cost a round trip each.
    channels = [c for c in listing.get("channels", []) if c.get("is_member")][:3]
    items = []
    for channel in channels:
        history = _api_get(
            token, "https://slack.com/api/conversations.history"
                   f"?channel={urllib.parse.quote(str(channel.get('id') or ''))}&limit=10")
        if history.get("ok") is False:
            continue
        for m in history.get("messages", []):
            text = str(m.get("text") or "").strip()
            if not text or m.get("subtype"):
                continue
            items.append({
                "id": f"{channel.get('id')}:{m.get('ts')}",
                "title": text[:180],
                "subtitle": "#" + str(channel.get("name") or "channel"),
                "when": _slack_ts(m.get("ts")),
                "url": "",
            })
    items.sort(key=lambda i: i["when"], reverse=True)
    return items[:limit]


def _read_dropbox(cfg: dict, creds: dict, limit: int) -> list[dict]:
    data = _api_post_json(creds["access_token"],
                          "https://api.dropboxapi.com/2/files/list_folder",
                          {"path": "", "recursive": False, "limit": limit})
    items = []
    for entry in data.get("entries", []):
        is_file = entry.get(".tag") == "file"
        items.append({
            "id": str(entry.get("id") or entry.get("path_lower") or ""),
            "title": str(entry.get("name") or "(unnamed)"),
            "subtitle": str(entry.get("path_display") or "") if is_file else "Folder",
            "when": str(entry.get("client_modified") or entry.get("server_modified") or ""),
            "url": "",
        })
    items.sort(key=lambda i: i["when"], reverse=True)
    return items[:limit]


def _notion_title(page: dict) -> str:
    """Notion has no title field - it's whichever property happens to be of
    type "title", and its text is split across rich-text runs."""
    for prop in (page.get("properties") or {}).values():
        if isinstance(prop, dict) and prop.get("type") == "title":
            runs = prop.get("title") or []
            text = "".join(str(r.get("plain_text") or "") for r in runs if isinstance(r, dict))
            if text.strip():
                return text.strip()
    return "(untitled)"


def _read_notion(cfg: dict, creds: dict, limit: int) -> list[dict]:
    data = _api_post_json(
        creds["access_token"], "https://api.notion.com/v1/search",
        {"sort": {"direction": "descending", "timestamp": "last_edited_time"},
         "page_size": limit},
        {"Notion-Version": "2022-06-28"})
    items = []
    for result in data.get("results", []):
        items.append({
            "id": str(result.get("id") or ""),
            "title": _notion_title(result) if result.get("object") == "page" else "(database)",
            "subtitle": "Database" if result.get("object") == "database" else "Page",
            "when": str(result.get("last_edited_time") or ""),
            "url": str(result.get("url") or ""),
        })
    return items[:limit]


def _read_trello(cfg: dict, creds: dict, limit: int) -> list[dict]:
    """Trello authenticates with key and token in the query string rather
    than a bearer header, so it can't use the shared helpers."""
    params = urllib.parse.urlencode({
        "key": creds.get("api_key", ""), "token": creds.get("api_token", ""),
        "fields": "name,dateLastActivity,url,idBoard", "limit": limit})
    req = urllib.request.Request(
        f"https://api.trello.com/1/members/me/cards?{params}",
        headers={"Accept": "application/json"}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            cards = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise ToolError("Trello rejected that key and token. Reconnect it.") from exc
        raise ToolError(f"Trello returned an error ({exc.code}).") from exc
    except urllib.error.URLError as exc:
        raise ToolError("Could not reach Trello.") from exc
    if not isinstance(cards, list):
        raise ToolError("Trello returned something unexpected.")
    items = [{
        "id": str(c.get("id") or ""),
        "title": str(c.get("name") or "(unnamed card)"),
        "subtitle": "Card",
        "when": str(c.get("dateLastActivity") or ""),
        "url": str(c.get("url") or ""),
    } for c in cards if isinstance(c, dict)]
    items.sort(key=lambda i: i["when"], reverse=True)
    return items[:limit]


READERS = {
    "slack": _read_slack,
    "dropbox": _read_dropbox,
    "notion": _read_notion,
    "trello": _read_trello,
}

# What makes a chat message worth spending a vendor round trip on. Reading
# every connected tool on every message would add seconds of latency to
# questions that have nothing to do with them, so a question earns the read.
# Erring towards reading is the cheaper mistake: a needless read costs a
# second, whereas a missed one makes Ada look like she can't see what the
# person can plainly see is connected.
CONTEXT_KEYWORDS = {
    "slack": ("slack", "channel"),
    "dropbox": ("dropbox",),
    "notion": ("notion", "wiki"),
    "trello": ("trello", "board", "card"),
}

# Asked about the connections in general rather than one by one.
GENERIC_KEYWORDS = ("connected tool", "connected tools", "integration",
                    "integrations", "my tools")


def can_read(tool_id: str) -> bool:
    return tool_id in READERS


def relevant_to(message: str, connected_ids) -> list[str]:
    """Which of somebody's connected tools a message is plausibly about.

    Only ever narrows to tools they actually have - naming Salesforce when
    nothing is connected to it returns nothing rather than an empty heading
    that invites the model to speculate.
    """
    text = " " + message.lower() + " "
    connected = [t for t in connected_ids if t in READERS]
    if any(word in text for word in GENERIC_KEYWORDS):
        return connected
    hits = []
    for tool_id in connected:
        words = CONTEXT_KEYWORDS.get(tool_id, (tool_id,))
        if any(re.search(rf"\b{re.escape(w)}s?\b", text) for w in words):
            hits.append(tool_id)
    return hits


def list_recent(cfg: dict, tool_id: str, creds: dict, limit: int = 10) -> list[dict]:
    """Recent items from a connected tool, in one shape whatever the tool is.

    Read-only by construction: there is no counterpart that writes, and every
    scope asked for at consent is a read scope.
    """
    reader = READERS.get(tool_id)
    if not reader:
        raise ToolError(f"{LABELS.get(tool_id, 'That tool')} can be connected, "
                        "but Buinee can't read from it yet.")
    return reader(cfg, creds, max(1, min(limit, MAX_READ_ITEMS)))


def public_catalog(cfg: dict) -> list[dict]:
    """The catalog as the browser sees it. No endpoints, no scopes, no keys -
    just what a person needs to decide whether to connect something."""
    return [{
        "id": t["id"], "label": t["label"], "category": t["category"],
        "category_label": CATEGORY_LABELS[t["category"]],
        "blurb": t["blurb"], "auth": t["auth"],
        "configured": is_configured(cfg, t["id"]),
        "readable": can_read(t["id"]),
    } for t in CATALOG]
