"""Final negotiated prices are net selling prices, never marked up implicitly."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import html

from .repository import InquiryError


def number(value, *, positive=False):
    if isinstance(value, bool):
        raise InquiryError("quote_number_invalid", 422)
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise InquiryError("quote_number_invalid", 422)
    if not result.is_finite() or result < 0 or (positive and result <= 0) or result > Decimal("1000000000000"):
        raise InquiryError("quote_number_invalid", 422)
    return result


def money(value):
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def calculate_final_quote(terms: dict, products: list[dict]) -> dict:
    required = ("currency", "payment_terms", "trade_term", "delivery", "valid_until", "recipient")
    if any(not isinstance(terms.get(key), str) or not terms[key].strip() for key in required):
        raise InquiryError("quote_terms_incomplete", 422)
    if terms.get("price_basis") != "final_net_unit_price":
        raise InquiryError("quote_final_price_basis_required", 422)
    currency = terms["currency"].upper()
    if len(currency) != 3 or not currency.isascii() or not currency.isalpha():
        raise InquiryError("quote_currency_invalid", 422)
    try:
        valid = datetime.fromisoformat(terms["valid_until"].replace("Z", "+00:00"))
        if valid.tzinfo is None:
            raise ValueError()
        if valid <= datetime.now(timezone.utc):
            raise ValueError()
    except ValueError:
        raise InquiryError("quote_validity_invalid", 422)
    lines = terms.get("items")
    if not isinstance(lines, list) or not 1 <= len(lines) <= 100:
        raise InquiryError("quote_items_required", 422)
    catalog = {str(item["sku"]): item for item in products}
    result, seen = [], set()
    for line in lines:
        if not isinstance(line, dict):
            raise InquiryError("quote_items_invalid", 422)
        sku = str(line.get("sku", ""))
        if sku in seen or sku not in catalog:
            raise InquiryError("quote_product_match_required", 422)
        seen.add(sku)
        product = catalog[sku]
        quantity = number(line.get("quantity"), positive=True)
        unit_price = number(line.get("unit_price"), positive=True)
        if not line.get("unit") or line["unit"] != product.get("unit"):
            raise InquiryError("quote_unit_mismatch", 422)
        if quantity < number(product.get("moq", 1)):
            raise InquiryError("quote_below_moq", 422)
        if quantity > number(product.get("inventory", 0)):
            raise InquiryError("quote_insufficient_inventory", 422)
        result.append({"sku": sku, "description": product.get("name_en") or product.get("name_cn", sku),
                       "quantity": str(quantity), "unit": line["unit"], "unit_price": str(unit_price),
                       "line_total": money(quantity * unit_price)})
    # Explicit zero is required: omitted freight/taxes are not assumed to be zero.
    fees = terms.get("fees")
    if not isinstance(fees, dict) or set(fees) != {"freight", "packaging", "tax", "other"}:
        raise InquiryError("quote_explicit_fees_required", 422)
    normalized_fees = {key: money(number(value)) for key, value in fees.items()}
    subtotal = sum((Decimal(line["line_total"]) for line in result), Decimal(0))
    return {"items": result, "currency": currency, "subtotal": money(subtotal), "fees": normalized_fees,
            "total": money(subtotal + sum((Decimal(x) for x in normalized_fees.values()), Decimal(0))),
            "price_basis": "final_net_unit_price", **{key: terms[key] for key in required if key != "currency"}}


def render_quote(quote: dict, inquiry_id: str, *, language: str = "en") -> tuple[str, str, str]:
    chinese = language == "zh"
    subject = f"报价单 {inquiry_id}" if chinese else f"Quotation {inquiry_id}"
    labels = ({"currency": "币种", "subtotal": "小计", "total": "总计", "payment": "付款条件",
               "trade_term": "贸易条款", "delivery": "交货安排", "valid_until": "报价有效期",
               "freight": "运费", "packaging": "包装费", "tax": "税费", "other": "其他费用"}
              if chinese else {"currency": "Currency", "subtotal": "Subtotal", "total": "Total",
                               "payment": "Payment", "trade_term": "Trade term", "delivery": "Delivery",
                               "valid_until": "Valid until", "freight": "Freight",
                               "packaging": "Packaging", "tax": "Tax", "other": "Other fees"})
    lines = [subject, "", f"{labels['currency']}: {quote['currency']}"]
    for item in quote["items"]:
        lines.append(f"{item['sku']} | {item['description']} | {item['quantity']} {item['unit']} x {item['unit_price']} = {item['line_total']}")
    lines.extend([f"{labels['subtotal']}: {quote['subtotal']}",
                  *[f"{labels.get(k, k)}: {v}" for k, v in quote["fees"].items()],
                  f"{labels['total']}: {quote['total']} {quote['currency']}",
                  f"{labels['payment']}: {quote['payment_terms']}",
                  f"{labels['trade_term']}: {quote['trade_term']}",
                  f"{labels['delivery']}: {quote['delivery']}",
                  f"{labels['valid_until']}: {quote['valid_until']}"])
    body = "\n".join(lines)
    document = (f'<!doctype html><html lang="{language}"><meta charset="utf-8">'
                f'<title>{html.escape(subject)}</title><body><h1>{html.escape(subject)}</h1><pre>'
                + html.escape(body) + '</pre></body></html>')
    return subject, body, document
