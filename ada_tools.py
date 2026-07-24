"""LLM-callable tools Ada can invoke via Gemini function calling
(providers.chat's tools/tool_runner params - see providers._chat_google).

Each entry pairs a JSON schema (what the model sees) with a Python handler
bound to the acting user's company/user id. company_id/user_id are always
injected by the caller from the authenticated session - never read from the
model's tool_input - so Ada cannot act outside the scope of whoever is
chatting with her, however she's prompted.

due_in_minutes (relative), not an absolute timestamp: models are unreliable
at producing correct epoch/ISO values without careful grounding. A relative
offset from "now" - which the caller grounds in the digest, see
server._handle_chat's current-time line - is a much safer contract.
"""

from __future__ import annotations

import time

import db

REMINDER_TOOLS = [
    {
        "name": "create_reminder",
        "description": (
            "Create a one-time reminder for the current user. It shows up in "
            "their dashboard and is emailed to them when due."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "What to remind them about"},
                "due_in_minutes": {
                    "type": "integer",
                    "description": "How many minutes from now the reminder is due",
                },
            },
            "required": ["message", "due_in_minutes"],
        },
    },
    {
        "name": "cancel_reminder",
        "description": "Cancel a pending reminder by its id.",
        "parameters": {
            "type": "object",
            "properties": {"reminder_id": {"type": "integer"}},
            "required": ["reminder_id"],
        },
    },
    {
        "name": "list_reminders",
        "description": "List the current user's reminders, pending first.",
        "parameters": {"type": "object", "properties": {}},
    },
]


def run_reminder_tool(name: str, tool_input: dict, *, company_id: int, user_id: int) -> dict:
    if name == "create_reminder":
        minutes = tool_input.get("due_in_minutes")
        try:
            minutes = max(1, int(minutes))
        except (TypeError, ValueError):
            return {"error": "due_in_minutes must be a whole number of minutes."}
        due_at = time.time() + minutes * 60
        try:
            r = db.create_reminder(
                company_id, user_id,
                message=str(tool_input.get("message") or ""),
                due_at=due_at,
            )
        except db.AuthError as exc:
            return {"error": str(exc)}
        return {"ok": True, "reminder": r}

    if name == "cancel_reminder":
        try:
            reminder_id = int(tool_input["reminder_id"])
        except (KeyError, TypeError, ValueError):
            return {"error": "reminder_id is required and must be a number."}
        try:
            r = db.cancel_reminder(company_id, user_id, reminder_id)
        except db.AuthError as exc:
            return {"error": str(exc)}
        return {"ok": True, "reminder": r}

    if name == "list_reminders":
        return {"reminders": db.list_reminders(company_id, user_id, "")}

    return {"error": f"Unknown tool: {name}"}
