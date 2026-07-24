"""Deterministic solar system sizing.

Same reasoning as voucher.py's arithmetic checks: a number a customer will
spend real money against shouldn't depend on an LLM getting multiplication
right by chance. Ada calls out to this rather than estimating herself - see
solar_tools.py for the tool she calls.

Assumptions (SUN_HOURS/PERFORMANCE_RATIO/DOD/SURGE_FACTOR) are Ghana-typical
defaults, not universal constants - a real client may want their own numbers
plugged in once they've registered.
"""

from __future__ import annotations

SUN_HOURS_GHANA = 5.5       # average peak sun hours/day
PERFORMANCE_RATIO = 0.75    # derating for heat, wiring, inverter losses
DEFAULT_DOD = 0.9           # depth of discharge, lithium (LFP)
SURGE_FACTOR = 1.3          # inverter headroom over simultaneous running watts


def compute_load(appliances: list[dict]) -> dict:
    """appliances: [{name, watts, quantity, hours_per_day}, ...]

    watts is running watts per unit, not surge/starting watts - the surge
    margin is applied once, at the inverter-sizing step, rather than asked
    of whoever is listing appliances.
    """
    daily_wh = 0.0
    peak_watts = 0.0
    for item in appliances:
        watts = float(item.get("watts") or 0)
        qty = float(item.get("quantity") or 1)
        hours = float(item.get("hours_per_day") or 0)
        daily_wh += watts * qty * hours
        peak_watts += watts * qty
    return {
        "daily_wh": round(daily_wh, 1),
        "daily_kwh": round(daily_wh / 1000, 2),
        "peak_watts": round(peak_watts, 1),
    }


def size_system(daily_kwh: float, peak_watts: float, backup_days: float = 1,
                 sun_hours: float = SUN_HOURS_GHANA,
                 performance_ratio: float = PERFORMANCE_RATIO,
                 dod: float = DEFAULT_DOD) -> dict:
    """Panel (kWp), inverter (kVA), and battery (kWh) sizing from a daily
    load. backup_days is how many sunless days the battery alone must cover."""
    kwp = daily_kwh / sun_hours / performance_ratio if sun_hours and performance_ratio else 0.0
    kva = (peak_watts * SURGE_FACTOR) / 1000
    battery_kwh = (daily_kwh * backup_days) / dod if dod else 0.0
    return {
        "kwp": round(kwp, 2),
        "kva": round(kva, 2),
        "battery_kwh": round(battery_kwh, 2),
    }


def match_tier(sizing: dict, tiers: list[dict]) -> dict | None:
    """The cheapest configured tier whose panels/inverter/battery all cover
    the computed need, or None if nothing on the price list is big enough."""
    candidates = [
        t for t in tiers
        if t["min_kwp"] >= sizing["kwp"]
        and t["inverter_kva"] >= sizing["kva"]
        and t["battery_kwh"] >= sizing["battery_kwh"]
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda t: t["min_kwp"])
