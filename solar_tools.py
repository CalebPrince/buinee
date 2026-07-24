"""LLM-callable tool for sizing a solar system and matching it against the
acting company's own pricing tiers - the same tools/tool_runner wiring
ada_tools.py uses for reminders (see providers._chat_google).

The arithmetic runs in solar_calc.py, never in the model: Ada extracts the
appliance list from conversation and calls out for the sizing, matching
voucher.py's "computed, never a matter of opinion" rule for anything a
customer will spend money against.
"""

from __future__ import annotations

import db
import solar_calc

SOLAR_TOOLS = [
    {
        "name": "calculate_solar_quote",
        "description": (
            "Size a solar system from a customer's appliance list and match it against "
            "this company's own pricing tiers. Call this whenever a customer describes "
            "what they want to run on solar - never estimate panel, inverter, or battery "
            "sizes yourself, and never invent a price that isn't in the matched tier."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "appliances": {
                    "type": "array",
                    "description": "Every appliance the customer wants powered.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "watts": {"type": "number", "description": "Running watts, per unit"},
                            "quantity": {"type": "integer"},
                            "hours_per_day": {"type": "number"},
                        },
                        "required": ["name", "watts", "quantity", "hours_per_day"],
                    },
                },
                "backup_days": {
                    "type": "number",
                    "description": "Days of battery autonomy wanted with no sun at all. Defaults to 1.",
                },
            },
            "required": ["appliances"],
        },
    },
]


def run_solar_tool(name: str, tool_input: dict, *, company_id: int) -> dict:
    if name != "calculate_solar_quote":
        return {"error": f"Unknown tool: {name}"}

    appliances = tool_input.get("appliances") or []
    if not isinstance(appliances, list) or not appliances:
        return {"error": "At least one appliance is required."}

    try:
        backup_days = max(0.5, float(tool_input.get("backup_days") or 1))
    except (TypeError, ValueError):
        backup_days = 1.0

    load = solar_calc.compute_load(appliances)
    sizing = solar_calc.size_system(load["daily_kwh"], load["peak_watts"], backup_days=backup_days)

    tiers = db.list_solar_pricing_tiers(company_id)
    if not tiers:
        return {"load": load, "sizing": sizing, "tier": None,
                "note": "This company hasn't configured solar pricing tiers yet."}

    tier = solar_calc.match_tier(sizing, tiers)
    if not tier:
        return {"load": load, "sizing": sizing, "tier": None,
                "note": "This load exceeds every configured tier - needs a custom quote."}
    return {"load": load, "sizing": sizing, "tier": tier}
