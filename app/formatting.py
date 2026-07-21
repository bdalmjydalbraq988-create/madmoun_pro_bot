from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

DISPLAY_CENT = Decimal("0.01")


def money_number(value: Decimal | str | int | float) -> str:
    """Format stored 8-decimal money as a customer-friendly two-decimal value."""

    amount = Decimal(str(value)).quantize(DISPLAY_CENT, rounding=ROUND_HALF_UP)
    return format(amount, ".2f")


def money_label(value: Decimal | str | int | float, currency: str | None = "USDT") -> str:
    """Use a stable LTR dollar label for USD/USDT and avoid Arabic bidi reordering."""

    number = money_number(value)
    normalized_currency = (currency or "USDT").strip().upper()
    if normalized_currency in {"USD", "USDT"}:
        return f"${number}"
    return f"{number} {normalized_currency}"


def decimal_number(value: Decimal | str | int | float) -> str:
    """Format a generic decimal without database padding zeros."""

    text = format(Decimal(str(value)), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text
