"""
Payment voucher computation.

Every figure on Rufus's payment voucher is arithmetic plus one date-keyed rate
lookup. None of it is a judgement call, so none of it belongs to a language
model: an accountant will not accept an LLM doing arithmetic on tax
deductions, and they are right not to.

The split is therefore:
    AI      - reads the invoice and extracts the inputs below
    this    - computes the voucher, deterministically and testably

Rules were reverse-engineered from a real BDDG voucher (invoice EWS/BEF/07/26)
and every figure reconciles to the cent. Run this module directly to re-check
that against the sample:  python bridge/voucher.py
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path


# --------------------------------------------------------------- tax regime

@dataclass(frozen=True)
class TaxRegime:
    """Rates for one jurisdiction. Ghana's are the defaults."""
    nhil_getfl: float = 0.05     # NHIL / GETFL / TL / CST
    vat: float = 0.15
    wht: float = 0.075           # withholding tax
    currency: str = "GHS"


GHANA = TaxRegime()


# ------------------------------------------------------------- FX rate table

class FxRates:
    """Bank of Ghana daily interbank averages, keyed by date."""

    def __init__(self, by_date: dict[date, float]):
        self._rates = by_date

    @classmethod
    def from_workbook(cls, path: str | Path, pair_column: int = 3) -> "FxRates":
        """Read the 'Summary' sheet of a BOG monthly rates workbook.

        Column 3 is GHS/USD, 5 is GHS/EUR, 7 is GHS/GBP.
        """
        import openpyxl

        wb = openpyxl.load_workbook(str(path), data_only=True, read_only=True)
        ws = wb["Summary"]
        out: dict[date, float] = {}
        for row in ws.iter_rows(values_only=True):
            when, rate = row[0], row[pair_column] if len(row) > pair_column else None
            if hasattr(when, "date") and isinstance(rate, (int, float)) and rate > 0:
                out[when.date()] = float(rate)
        wb.close()
        return cls(out)

    def rate_for(self, when: date) -> tuple[float, date]:
        """Rate for a date, falling back to the most recent prior publication.

        The BOG publishes nothing on weekends and holidays - those rows exist
        but read 0. Using the last published rate is the standard treatment,
        and the caller is told which date was actually used so the voucher can
        show it rather than quietly substituting.
        """
        if not self._rates:
            raise ValueError("No FX rates loaded.")
        if when in self._rates:
            return self._rates[when], when
        earlier = [d for d in self._rates if d <= when]
        if not earlier:
            raise ValueError(f"No BOG rate published on or before {when}.")
        used = max(earlier)
        return self._rates[used], used


# ------------------------------------------------------------------- inputs

@dataclass
class LineItem:
    description: str
    amount: float
    supplier_type: str = ""      # e.g. SUNDRY
    cost_centre: str = ""        # e.g. ADMIN EXP


@dataclass
class VoucherInput:
    """What the AI extracts from an invoice."""
    supplier_name: str
    invoice_number: str
    invoice_date: date
    received_date: date
    credit_terms_days: int
    lines: list[LineItem] = field(default_factory=list)

    #: The portion of the invoice subject to tax. Not always the whole invoice,
    #: so it is an input rather than something we can infer.
    vatable_amount: float = 0.0

    apply_nhil: bool = True
    apply_vat: bool = True
    vrpo: bool = False           # VAT Relief Purchase Order
    vrpo_deduction: float = 0.0
    non_taxable: float = 0.0     # non-taxable bill / allowance
    overpayment: float = 0.0     # overpayment / deposit already held

    supplier_address: str = ""
    supplier_tel: str = ""
    supplier_email: str = ""


# ------------------------------------------------------------------ compute

def compute(
    inp: VoucherInput,
    rates: FxRates | None = None,
    regime: TaxRegime = GHANA,
) -> dict:
    """Produce every figure on the voucher.

    Verified against the BDDG sample: NHIL, VAT, WHT, net payable and both
    foreign-currency columns all reconcile exactly.
    """
    total = round(sum(l.amount for l in inp.lines), 2)
    vatable = round(inp.vatable_amount, 2)

    nhil = round(vatable * regime.nhil_getfl, 2) if inp.apply_nhil else 0.0
    vat = round(vatable * regime.vat, 2) if inp.apply_vat else 0.0
    wht = round(vatable * regime.wht, 2)

    # Amount booked is the invoice less any VAT-relief purchase order.
    amount_to_book = round(total - inp.vrpo_deduction, 2)

    # Verified against the sample only for the WHT term - the sample's other
    # deductions are all zero, so those paths are implemented from the voucher's
    # own labels and should be checked against a voucher that exercises them.
    net_payable = round(
        amount_to_book - wht - inp.non_taxable - inp.overpayment, 2
    )

    due_date = inp.received_date + timedelta(days=inp.credit_terms_days)

    out = {
        "supplier_name": inp.supplier_name,
        "supplier_address": inp.supplier_address,
        "invoice_number": inp.invoice_number,
        "invoice_date": inp.invoice_date,
        "received_date": inp.received_date,
        "credit_terms_days": inp.credit_terms_days,
        "due_date": due_date,
        "lines": inp.lines,
        "currency": regime.currency,

        "total_invoice": total,
        "amount_to_book": amount_to_book,
        "vatable_amount": vatable,
        "nhil_getfl": nhil,
        "vat": vat,
        "wht": wht,
        "vrpo_deduction": round(inp.vrpo_deduction, 2),
        "non_taxable": round(inp.non_taxable, 2),
        "overpayment": round(inp.overpayment, 2),
        "net_payable": net_payable,

        "rate_nhil": regime.nhil_getfl,
        "rate_vat": regime.vat,
        "rate_wht": regime.wht,
        "vrpo": "YES" if inp.vrpo else "NO",
    }

    # Foreign-currency column, at the BOG rate for the invoice date.
    if rates is not None:
        rate, rate_date = rates.rate_for(inp.invoice_date)
        out["exchange_rate"] = rate
        out["exchange_rate_date"] = rate_date
        out["exchange_rate_is_fallback"] = rate_date != inp.invoice_date
        for key in ("total_invoice", "amount_to_book", "vatable_amount",
                    "nhil_getfl", "vat", "wht", "net_payable"):
            out[f"fcy_{key}"] = round(out[key] / rate, 2)

    return out


def review(v: dict) -> list[str]:
    """Things a preparer should look at before the reviewer sees it.

    Deliberately conservative: only flags what is arithmetically or
    procedurally checkable, never a matter of opinion.
    """
    notes: list[str] = []

    if v["vatable_amount"] > v["total_invoice"]:
        notes.append(
            f"Vatable amount ({v['vatable_amount']:,.2f}) exceeds the invoice "
            f"total ({v['total_invoice']:,.2f})."
        )
    if v["vatable_amount"] == 0:
        notes.append("No vatable amount set, so no VAT or withholding tax was computed.")
    if v["net_payable"] > v["total_invoice"]:
        notes.append("Net payable is higher than the invoice total - check the deductions.")
    if v["net_payable"] <= 0:
        notes.append("Net payable is zero or negative - check the deductions.")
    if v["due_date"] < v["invoice_date"]:
        notes.append("Due date falls before the invoice date.")
    if v["received_date"] < v["invoice_date"]:
        notes.append("Invoice was recorded as received before it was issued.")
    if v.get("exchange_rate_is_fallback"):
        notes.append(
            f"No BOG rate was published on {v['invoice_date']}; used the "
            f"{v['exchange_rate_date']} rate of {v['exchange_rate']:.4f}."
        )
    return notes


# ------------------------------------------------------------------ self-test

def _self_test() -> int:
    """Reproduce the real BDDG voucher and check every figure."""
    root = Path(__file__).parent.parent
    fx_file = root / "07-July-26 BOG FX RATES.xlsx"

    rates = FxRates.from_workbook(fx_file) if fx_file.exists() else None
    if rates is None:
        print("  ! BOG rates workbook not found - skipping FX checks\n")

    sample = VoucherInput(
        supplier_name="EMPOWER WORKFORCE SOLUTIONS",
        supplier_address="P O BOX, ACCRA",
        invoice_number="EWS/BEF/07/26",
        invoice_date=date(2026, 7, 2),
        received_date=date(2026, 7, 13),
        credit_terms_days=3,
        vatable_amount=1784.58,
        lines=[LineItem("LABOUR BROKERAGE FOR THE MONTH OF JULY 2026",
                        12610.95, "SUNDRY", "ADMIN EXP")],
    )

    v = compute(sample, rates)

    expected = {
        "total_invoice": 12610.95,
        "nhil_getfl": 89.23,
        "vat": 267.69,
        "wht": 133.84,
        "net_payable": 12477.11,
        "due_date": date(2026, 7, 16),
    }
    if rates is not None:
        expected |= {
            "exchange_rate": 11.3895,
            "fcy_total_invoice": 1107.24,
            "fcy_net_payable": 1095.49,
        }

    print(f"  {'figure':<22}{'computed':>14}{'expected':>14}   ok")
    print(f"  {'-' * 54}")
    failures = 0
    for key, want in expected.items():
        got = v.get(key)
        if isinstance(want, float):
            ok = got is not None and abs(got - want) < 0.02
            g, w = f"{got:,.2f}" if got is not None else "-", f"{want:,.2f}"
        else:
            ok = got == want
            g, w = str(got), str(want)
        failures += 0 if ok else 1
        print(f"  {key:<22}{g:>14}{w:>14}   {'YES' if ok else 'NO'}")

    print(f"\n  review notes: {review(v) or 'none - voucher looks clean'}")
    print(f"\n  {'ALL FIGURES RECONCILE' if not failures else str(failures) + ' MISMATCH(ES)'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
