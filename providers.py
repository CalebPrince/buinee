"""
Triage a batch of emails using Anthropic, Google, or OpenRouter.

Every provider is asked for the same JSON shape so the dashboard does not
care which one produced the answer.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

DEFAULT_MODELS = {
    "anthropic": "claude-opus-4-8",
    # "-latest" aliases track the current model, so an ID rename can't break us.
    # Verify real IDs for a key with:  GET /v1beta/models?key=...
    "google": "gemini-pro-latest",
    "openrouter": "anthropic/claude-opus-4.8",
}

SYSTEM = """You are Ada, an inbox assistant for a business owner who works \
closely with an accounting department.

For each email, decide:
- category: invoice, approval, query, fyi, or other
- priority: high, medium, or low (high = money at risk, a deadline, or someone blocked)
- needs_reply: true only if this person is genuinely waiting on a reply
- summary: one or two plain sentences. What is this, and what do they want?
- action: the single next step the owner should take. "No action needed" is a valid answer.
- issues: concrete things to check or fix - amount mismatches, missing invoice or PO
  numbers, missing attachments, duplicate requests, approaching due dates. Empty list
  if nothing is wrong. Do not invent problems.
- suggested_reply: a ready-to-edit reply if needs_reply is true, otherwise "".
  Match a normal professional business tone. Never promise a payment or a date that
  is not already stated in the email. Do not invent the user's name or signature.

Be accurate over impressive. If an email is routine, say so plainly."""

TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "category": {
                        "type": "string",
                        "enum": ["invoice", "approval", "query", "fyi", "other"],
                    },
                    "priority": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                    },
                    "needs_reply": {"type": "boolean"},
                    "summary": {"type": "string"},
                    "action": {"type": "string"},
                    "issues": {"type": "array", "items": {"type": "string"}},
                    "suggested_reply": {"type": "string"},
                },
                "required": [
                    "id", "category", "priority", "needs_reply",
                    "summary", "action", "issues", "suggested_reply",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}


CHAT_SYSTEM = """You are Ada, the assistant inside Buinee, a workspace for finance \
departments: prepare a payment voucher, get it approved, issue the letter - \
with real roles, a real approval trail and a signature recorded in the system.

You are talking to someone signed in at their company, inside their own \
workspace - not a visitor on the landing page. This is a general assistant \
for their finance/back-office work, not a voucher-lookup tool - answer any \
business or finance question they raise, the same way a knowledgeable \
colleague would. You are given a digest of their company's current vouchers \
(supplier, invoice number, status, computed figures, anything flagged) to \
ground factual claims about their real records - it is context, not the \
limit of what you can discuss.

## Answering about their actual vouchers
- Ground every factual claim about a real voucher in the digest. Never invent
  a voucher, supplier, amount, invoice number or date that isn't in it.
- Name the supplier and invoice number when you refer to a voucher so they can
  find it.
- Lead with the answer, then the detail.
- Tax lines are always computed by the product's own code, never by you. If
  asked to check a figure, restate the computed figure from the digest - do
  not recompute it yourself.

## Grounding stops you inventing things. It does not stop you thinking.
Only mention that something "isn't in the digest" when they are genuinely
asking you to look up or act on a specific voucher you cannot see. If they are
describing a situation, asking what they should do, or asking a general
finance or business question, just answer it - your judgement is the useful
part. Never refuse to give an opinion on a business question simply because
the paperwork isn't in front of you.

## When they describe a process, or ask for something you can't do yet
This is the important one. They will describe how their business works - an
approval route, a filing convention - and ask whether you can take it on. Do
NOT deflect with "that isn't in your digest". Instead:

1. Show you understood, by restating the workflow in your own words -
   concretely, step by step. If you understood correctly they should recognise
   their own process.
2. Say plainly what you would need from them to actually do it.
3. Say honestly what you can do today versus what would need to be built. Do
   not claim a capability you do not have - see Hard limits below.
4. Ask at most one or two sharp questions. Do not interrogate them.

## When they give you a document
They may attach an invoice, voucher, letter, statement, contract or template
to their message - see "Attached to the message you are answering right now"
below if present. Reading it back to them is not useful on its own - they can
already read it. What they need is your assessment, in this order:

1. **What it is** - one line. Type of document, who from, what it's for.
2. **The figures that matter** - amounts, dates, references, terms. Compute
   what is implied but not stated: due dates from payment terms, whether a
   total actually adds up.
3. **Your honest opinion.** Say what is wrong, missing, unusual or risky - a
   total that doesn't foot, terms that look off, a date already passed. If a
   document is complete and unremarkable, say exactly that in one line and
   move on. Do not invent concerns to appear thorough, and do not soften a
   real problem to be agreeable.
4. **What next** - the specific next action, named plainly. If the sensible
   next step is to do nothing, say that.

## Hypotheticals
If they clearly signal a hypothetical ("suppose", "what if", "for example"),
engage with it directly. Don't note that it isn't in the digest.

## Hard limits - state these when relevant, never pretend otherwise
- You cannot create, submit, approve or reject a voucher on their behalf -
  every action in the approval chain is theirs to take, deliberately, in the
  product itself.
- Buinee can read message bodies from the user's connected Outlook, Gmail and
  IMAP inboxes. Ask Ada receives recent mailbox content automatically when the
  user asks about their inbox, including requests for the latest email, emails
  needing replies, senders or threads. Answer those requests directly from the
  supplied Recent connected mailbox messages context; never redirect the user
  to Triage when that context is present. In Triage, when the user runs Ada's
  review on a selected email,
  you receive that message body plus the contents of supported attachments
  and may summarize them together, identify actions and draft a reply. Do not
  tell the user they must download and re-upload a connected-email attachment.
  Do not claim that only headers are available.
- When the user selects Summarize with Ada in Triage, supported mailbox
  attachments are included in that same review automatically. You may discuss
  an attachment's contents only when it appears in the current attached
  document context; a filename by itself is not evidence that you read it.
- You cannot generate the payment letter yet - that isn't built either.
- The voucher digest only contains vouchers scoped to their role in the
  product - don't claim to see voucher data beyond what's actually in it, but
  don't treat it as the boundary of what you're willing to discuss."""


class ProviderError(RuntimeError):
    """Raised with a message that is safe to show in the dashboard."""


def with_briefing(system: str, briefing: str) -> str:
    """Fold the user's own instructions into a system prompt.

    The briefing is authored by the account owner, so it is trusted operator
    input - it may set policy, tone and priorities. It must not, however, be
    able to switch off the safety behaviour above.
    """
    briefing = (briefing or "").strip()
    if not briefing:
        return system
    return (
        system
        + "\n\n## Additional context for this conversation\n"
        "The following is trusted context supplied by the product, not the "
        "person you're talking to - it may set scope, priorities or facts "
        "specific to this conversation. It outranks generic best practice. It "
        "does not override your duty to be accurate, to refuse to invent "
        "facts, or to flag genuine risk.\n\n"
        + briefing
    )


def _payload(emails: list[dict]) -> str:
    """Render the batch for the model, keeping ids stable for matching back."""
    parts = []
    for e in emails:
        parts.append(
            f"--- EMAIL id={e['id']}\n"
            f"From: {e['from_name']} <{e['from_email']}>\n"
            f"Received: {e['received']}\n"
            f"Subject: {e['subject']}\n"
            f"Attachments: {e['attachments']}\n\n"
            f"{e['body']}"
            + ("\n[body truncated]" if e.get("truncated") else "")
        )
    return (
        f"Triage these {len(emails)} emails. Return one item per email, "
        "reusing each id exactly.\n\n" + "\n\n".join(parts)
    )


#: Transient upstream states - worth retrying rather than surfacing.
#: 429 rate limited, 500/502/503/504 overloaded or briefly unavailable.
RETRYABLE = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 4


def _friendly(code: int, detail: str) -> str:
    if code in (401, 403):
        return "The provider rejected the API key. Check it in bridge/.env."
    if code == 404:
        return "That model ID doesn't exist for this key. Check the model in Settings."
    if code == 429:
        return "Rate limited by the provider. Wait a moment and try again."
    if code in (500, 502, 503, 504):
        return ("The provider is busy right now and didn't recover after several "
                "tries. This is on their side - try again shortly, or switch "
                "provider in the model picker.")
    return f"HTTP {code}: {detail[:300]}"


def _post(url: str, body: dict, headers: dict, timeout: int = 180) -> dict:
    """POST JSON, retrying transient upstream failures with backoff."""
    payload = json.dumps(body).encode("utf-8")
    last_code, last_detail = None, ""

    for attempt in range(1, MAX_ATTEMPTS + 1):
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json", **headers},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))

        except urllib.error.HTTPError as exc:
            last_code = exc.code
            last_detail = exc.read().decode("utf-8", "replace")
            if exc.code not in RETRYABLE or attempt == MAX_ATTEMPTS:
                raise ProviderError(_friendly(exc.code, last_detail)) from exc

        except urllib.error.URLError as exc:
            last_code, last_detail = None, str(exc.reason)
            if attempt == MAX_ATTEMPTS:
                raise ProviderError(
                    f"Could not reach the provider: {exc.reason}"
                ) from exc

        wait = min(2 ** attempt, 12)   # 2s, 4s, 8s
        print(f"  provider busy ({last_code or 'network'}), "
              f"retry {attempt}/{MAX_ATTEMPTS - 1} in {wait}s")
        time.sleep(wait)

    raise ProviderError(_friendly(last_code or 503, last_detail))


def _strip_unsupported(schema: dict) -> dict:
    """Gemini rejects additionalProperties; drop it recursively."""
    if isinstance(schema, dict):
        return {
            k: _strip_unsupported(v)
            for k, v in schema.items()
            if k != "additionalProperties"
        }
    if isinstance(schema, list):
        return [_strip_unsupported(v) for v in schema]
    return schema


# ---------------------------------------------------------------- Anthropic

def _anthropic(model: str, key: str, emails: list[dict], system: str) -> dict:
    try:
        import anthropic
    except ImportError as exc:
        raise ProviderError("The anthropic package is not installed.") from exc

    client = anthropic.Anthropic(api_key=key)
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=16000,
            thinking={"type": "adaptive"},
            output_config={
                "format": {"type": "json_schema", "schema": TRIAGE_SCHEMA}
            },
            system=system,
            messages=[{"role": "user", "content": _payload(emails)}],
        )
    except anthropic.APIStatusError as exc:
        raise ProviderError(f"Anthropic error {exc.status_code}: {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise ProviderError("Could not reach the Anthropic API.") from exc

    if resp.stop_reason == "refusal":
        raise ProviderError("The model declined to process this batch.")

    text = next((b.text for b in resp.content if b.type == "text"), "")
    if not text:
        raise ProviderError("Anthropic returned no text content.")
    return json.loads(text)


# ------------------------------------------------------------------- Google

def _google(model: str, key: str, emails: list[dict], system: str) -> dict:
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={key}"
    )
    data = _post(
        url,
        {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": _payload(emails)}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": _strip_unsupported(TRIAGE_SCHEMA),
            },
        },
        {},
    )
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as exc:
        raise ProviderError(f"Unexpected Google response: {str(data)[:300]}") from exc
    return json.loads(text)


# --------------------------------------------------------------- OpenRouter

def _openrouter(model: str, key: str, emails: list[dict], system: str) -> dict:
    data = _post(
        "https://openrouter.ai/api/v1/chat/completions",
        {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": _payload(emails)},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "triage",
                    "strict": True,
                    "schema": TRIAGE_SCHEMA,
                },
            },
        },
        {
            "Authorization": f"Bearer {key}",
            "X-Title": "Clerk - Outlook Inbox Agent",
        },
    )
    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as exc:
        raise ProviderError(f"Unexpected OpenRouter response: {str(data)[:300]}") from exc
    return json.loads(text)


# -------------------------------------------------------------------- entry

_DISPATCH = {
    "anthropic": _anthropic,
    "google": _google,
    "openrouter": _openrouter,
}


def build_digest(items: list[dict]) -> str:
    """Compact view of the last triage run - what Ada is allowed to reason from."""
    if not items:
        return "(no emails have been triaged yet)"
    lines = []
    for i, m in enumerate(items, 1):
        lines.append(
            f"{i}. {m.get('from_name')} <{m.get('from_email')}> - "
            f"\"{m.get('subject')}\" ({m.get('received')})\n"
            f"   category={m.get('category')} priority={m.get('priority')} "
            f"needs_reply={m.get('needs_reply')}\n"
            f"   summary: {m.get('summary')}\n"
            f"   action: {m.get('action')}"
            + (f"\n   flagged: {'; '.join(m.get('issues') or [])}" if m.get("issues") else "")
            + (f"\n   drafted reply: {m.get('suggested_reply')}" if m.get("suggested_reply") else "")
        )
    return "\n\n".join(lines)


def split_docs(docs: list[dict]) -> tuple[str, list[dict]]:
    """Text documents fold into the prompt; PDFs and images go natively.

    Attachments and library documents are labelled separately - otherwise the
    model treats a file the user just sent as something already on file, and
    replies "you forgot to attach it" while quoting from it.
    """
    lib, att, native = [], [], []
    for d in docs or []:
        if d.get("kind") != "text":
            native.append(d)
        elif d.get("source") == "attached":
            att.append(f"### {d['name']}\n{d['text']}")
        else:
            lib.append(f"### {d['name']}\n{d['text']}")

    blob = ""
    if lib:
        blob += ("\n\n## Reference library\n"
                 "The user's own templates, samples and policies, always "
                 "available. Follow their formats and conventions exactly when "
                 "producing anything modelled on them.\n\n" + "\n\n".join(lib))
    if att:
        blob += ("\n\n## Attached to the message you are answering right now\n"
                 "The user has just sent these files with their current "
                 "message. They are present and readable below - never tell "
                 "the user they forgot to attach something that appears here. "
                 "Work from this content directly.\n\n" + "\n\n".join(att))
    return blob, native


def _chat_anthropic(model, key, system, turns, native=None):
    try:
        import anthropic
    except ImportError as exc:
        raise ProviderError("The anthropic package is not installed.") from exc
    client = anthropic.Anthropic(api_key=key)
    msgs = [dict(t) for t in turns]
    if native and msgs:
        blocks = []
        for d in native:
            btype = "document" if d["kind"] == "pdf" else "image"
            blocks.append({
                "type": btype,
                "source": {"type": "base64",
                           "media_type": d["media_type"], "data": d["data"]},
            })
        blocks.append({"type": "text", "text": msgs[-1]["content"]})
        msgs[-1] = {"role": "user", "content": blocks}
    try:
        resp = client.messages.create(
            model=model, max_tokens=4000,
            thinking={"type": "adaptive"},
            system=system,
            messages=msgs,
        )
    except anthropic.APIStatusError as exc:
        raise ProviderError(f"Anthropic error {exc.status_code}: {exc.message}") from exc
    if resp.stop_reason == "refusal":
        raise ProviderError("The model declined to answer that.")
    return "".join(b.text for b in resp.content if b.type == "text").strip()


def _chat_google(model, key, system, turns, native=None):
    contents = [
        {"role": "model" if t["role"] == "assistant" else "user",
         "parts": [{"text": t["content"]}]}
        for t in turns
    ]
    if native and contents:
        contents[-1]["parts"] = [
            {"inline_data": {"mime_type": d["media_type"], "data": d["data"]}}
            for d in native
        ] + contents[-1]["parts"]
    data = _post(
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={key}",
        {"systemInstruction": {"parts": [{"text": system}]}, "contents": contents},
        {},
    )
    try:
        parts = data["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts).strip()
    except (KeyError, IndexError) as exc:
        raise ProviderError(f"Unexpected Google response: {str(data)[:300]}") from exc


def _chat_openrouter(model, key, system, turns, native=None):
    turns = [dict(t) for t in turns]
    imgs = [d for d in (native or []) if d["kind"] == "image"]
    if imgs and turns:
        turns[-1] = {"role": "user", "content":
                     [{"type": "image_url",
                       "image_url": {"url": f"data:{d['media_type']};base64,{d['data']}"}}
                      for d in imgs]
                     + [{"type": "text", "text": turns[-1]["content"]}]}
    data = _post(
        "https://openrouter.ai/api/v1/chat/completions",
        {"model": model,
         "messages": [{"role": "system", "content": system}] + turns},
        {"Authorization": f"Bearer {key}", "X-Title": "Clerk - Outlook Inbox Agent"},
    )
    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError) as exc:
        raise ProviderError(f"Unexpected OpenRouter response: {str(data)[:300]}") from exc


_CHAT = {
    "anthropic": _chat_anthropic,
    "google": _chat_google,
    "openrouter": _chat_openrouter,
}


def chat(provider: str, model: str, key: str,
         message: str, digest: str, history: list[dict],
         system: str, briefing: str = "", docs: list[dict] | None = None) -> str:
    """Answer a question grounded in the supplied digest.

    `system` is the caller's full base persona/context - there is no hardcoded
    default, since the right persona differs by caller (an unauthenticated
    landing-page visitor is a very different conversation from someone signed
    into their own company's workspace). `briefing` is for short, optional,
    user-authored instructions folded on top of that via with_briefing - most
    callers can leave it blank.
    """
    fn = _CHAT.get(provider)
    if not fn:
        raise ProviderError(f"Unknown provider: {provider}")
    if not key:
        raise ProviderError(f"No API key configured for {provider}.")

    doc_text, native = split_docs(docs or [])
    system = (with_briefing(system, briefing) + doc_text
              + "\n\n--- CURRENT CONTEXT ---\n" + digest)

    turns = []
    for t in history[-8:]:                       # keep the last few exchanges only
        role = "assistant" if t.get("role") == "assistant" else "user"
        text = str(t.get("content") or "").strip()
        if text:
            turns.append({"role": role, "content": text[:4000]})
    turns.append({"role": "user", "content": message[:4000]})

    reply = fn(model or DEFAULT_MODELS[provider], key, system, turns, native)
    if not reply:
        raise ProviderError("The model returned an empty reply.")
    return reply


def triage(provider: str, model: str, key: str, emails: list[dict],
           briefing: str = "") -> list[dict]:
    """Run one batch through the chosen provider and return the item list."""
    fn = _DISPATCH.get(provider)
    if not fn:
        raise ProviderError(f"Unknown provider: {provider}")
    if not key:
        raise ProviderError(f"No API key configured for {provider}.")
    if not emails:
        return []

    model = model or DEFAULT_MODELS[provider]
    try:
        result = fn(model, key, emails, with_briefing(SYSTEM, briefing))
    except json.JSONDecodeError as exc:
        raise ProviderError("The model returned malformed JSON.") from exc

    items = result.get("items", []) if isinstance(result, dict) else []

    # Re-attach the original headers so the dashboard can render without a lookup.
    by_id = {e["id"]: e for e in emails}
    merged = []
    for it in items:
        src = by_id.get(it.get("id"))
        if not src:
            continue
        merged.append({**it, **{
            "from_name": src["from_name"],
            "from_email": src["from_email"],
            "subject": src["subject"],
            "received": src["received"],
            "unread": src["unread"],
            "attachments": src["attachments"],
        }})
    return merged


def triage_with_docs(provider: str, model: str, key: str, emails: list[dict],
                     docs: list[dict], briefing: str = "") -> list[dict]:
    """Triage email and its actual attachment contents in one model call."""
    raw = chat(
        provider, model, key,
        "Review the email and every attached document. Return only valid JSON in the same {\"items\":[...]} triage shape, with no markdown fence.",
        _payload(emails), [], system=SYSTEM, briefing=briefing, docs=docs,
    ).strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProviderError("The model returned malformed attachment analysis.") from exc
    items = result.get("items", []) if isinstance(result, dict) else []
    by_id = {e["id"]: e for e in emails}
    merged = []
    for item in items:
        source = by_id.get(item.get("id"))
        if source:
            merged.append({**item, "from_name": source["from_name"],
                           "from_email": source["from_email"], "subject": source["subject"],
                           "received": source["received"], "unread": source["unread"],
                           "attachments": source["attachments"]})
    return merged
