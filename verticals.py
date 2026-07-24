"""Registry of vertical-specific quote settings a company can opt into -
drives both storage validation (db.KNOWN_VERTICALS) and the generic
Instructions-page settings UI (see server's /api/verticals route).

Pure data, no imports of the calculators that actually do each vertical's
math (solar_calc.py etc.) - this only describes what the settings UI needs
to render (a toggle, its copy, and the tier-editor's field list), never how
a vertical computes anything. Adding an entry here makes it appear as a
toggle + tier editor in Instructions automatically; it still needs its own
calculator module and Ada tool to do anything once enabled - see
solar_calc.py/solar_tools.py for the one that already exists.
"""

from __future__ import annotations

VERTICALS = {
    "solar": {
        "label": "Solar quoting",
        "description": (
            "Turn this on only if your company installs solar systems and wants "
            "Ada to size and quote them for customers."
        ),
        "tier_label": "Solar quote pricing tiers",
        "tier_description": (
            "Package tiers Ada matches a customer's computed system size against "
            "when quoting a solar job."
        ),
        "fields": [
            {"key": "kwp", "label": "Panel size (kWp)", "step": 0.1},
            {"key": "kva", "label": "Inverter (kVA)", "step": 0.1},
            {"key": "battery_kwh", "label": "Battery (kWh)", "step": 0.1},
        ],
    },
}


def known_ids() -> tuple[str, ...]:
    return tuple(VERTICALS.keys())
