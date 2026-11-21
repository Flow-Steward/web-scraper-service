"""The closed transform list and the final type coercion (README 6.1)."""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from app.extraction.htmlclean import clean_html
from app.extraction.pricing import (
    DefaultCurrency,
    PriceRange,
    decimal_hint,
    note,
    split_range,
)

WS_RE = re.compile(r"\s+", re.UNICODE)
NUMBER_STRIP = str.maketrans("", "", "     '’")
NUMBER_RE = re.compile(r"[-+]?\d[\d.,]*")
PRICE_TOKEN = re.compile("[-+]?\\d{1,3}(?:[ \u00a0\u202f\u2009\u2007']\\d{3})+(?:[.,]\\d+)?(?!\\d)|[-+]?\\d[\\d.,]*")

ISO_CODES = set(
    """AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN BHD BIF BMD BND BOB BRL BSD BTN BWP BYN BZD CAD CDF CHF
    CLP CNY COP CRC CUP CVE CZK DJF DKK DOP DZD EGP ERN ETB EUR FJD FKP GBP GEL GHS GIP GMD GNF GTQ GYD HKD HNL HTG HUF
    IDR ILS INR IQD IRR ISK JMD JOD JPY KES KGS KHR KMF KPW KRW KWD KYD KZT LAK LBP LKR LRD LSL LYD MAD MDL MGA MKD MMK
    MNT MOP MRU MUR MVR MWK MXN MYR MZN NAD NGN NIO NOK NPR NZD OMR PAB PEN PGK PHP PKR PLN PYG QAR RON RSD RUB RWF SAR
    SBD SCR SDG SEK SGD SHP SLE SOS SRD SSP STN SYP SZL THB TJS TMT TND TOP TRY TTD TWD TZS UAH UGX USD UYU UZS VES VND
    VUV WST XAF XCD XOF XPF YER ZAR ZMW ZWL""".split()
)
# Fixed symbol table; "$" and "kr" are ambiguous and resolve only through default_currency.
SYMBOLS = [
    ("zł", "PLN"), ("Kč", "CZK"), ("Ft", "HUF"), ("CHF", "CHF"), ("€", "EUR"), ("£", "GBP"), ("¥", "JPY"),
    ("₽", "RUB"), ("₴", "UAH"), ("₹", "INR"), ("₺", "TRY"), ("₩", "KRW"), ("₪", "ILS"), ("₫", "VND"), ("₦", "NGN"),
    ("лв", "BGN"), ("руб", "RUB"), ("lei", "RON"),
]
AMBIGUOUS = ("$", "kr")
TRUE_WORDS = {"true", "yes", "y", "1", "on", "ja"}
FALSE_WORDS = {"false", "no", "n", "0", "off", "nein"}
TRUE_AVAIL = ("instock", "instoreonly", "onlineonly", "limitedavailability", "preorder")
FALSE_AVAIL = ("outofstock", "soldout", "discontinued")


def trim(value: str) -> str:
    return WS_RE.sub(" ", value).strip()


def regex_extract(value: str, pattern: str, group: int = 1) -> str | None:
    try:
        match = re.search(pattern, value)
    except re.error:
        return None
    if not match:
        return None
    try:
        out = match.group(group)
    except IndexError:
        return None
    return out


def _parse_number(token: str, sep: str) -> float | None:
    token = token.rstrip(".,")
    if not token or token in "+-":
        return None
    sign = ""
    if token[0] in "+-":
        sign, token = token[0], token[1:]
    if sep == "dot":
        cleaned = token.replace(",", "")
    elif sep == "comma":
        cleaned = token.replace(".", "").replace(",", ".")
    else:
        has_dot, has_comma = "." in token, "," in token
        if has_dot and has_comma:
            dec = "." if token.rfind(".") > token.rfind(",") else ","
            other = "," if dec == "." else "."
            cleaned = token.replace(other, "").replace(dec, ".")
        elif has_dot or has_comma:
            ch = "." if has_dot else ","
            count = token.count(ch)
            after = token.split(ch)[-1]
            if count == 1 and len(after) == 3 and after.isdigit():
                # "1.234" / "1,234": thousands or decimals? The page's language decides, else thousands
                hint = decimal_hint()
                decimal = hint is not None and ((hint[0] == "dot") == (ch == "."))
                note(hint[1] if hint else "default_thousands", sign + token)
                cleaned = token.replace(ch, ".") if decimal else token.replace(ch, "")
            elif count > 1:
                cleaned = token.replace(ch, "")
            else:
                cleaned = token.replace(ch, ".")
        else:
            cleaned = token
    try:
        return float(sign + cleaned)
    except ValueError:
        return None


def number(value: str, decimal_separator: str = "auto") -> float | None:
    text = str(value).translate(NUMBER_STRIP)
    match = NUMBER_RE.search(text)
    if not match:
        return None
    return _parse_number(match.group(0), decimal_separator or "auto")


def price(value: str, decimal_separator: str = "auto") -> float | None:
    """The price in the text; a range ("24,00 € – 54,00 €", "Price range: 24 € through 54 €")
    gives its lowest value as a ``PriceRange`` that also carries the highest."""
    sides = split_range(str(value))
    if sides is not None:
        low, high = _single_price(sides[0], decimal_separator), _single_price(sides[1], decimal_separator)
        if low is not None and high is not None and high > low:
            return PriceRange(low, high)
    return _single_price(value, decimal_separator)


def _single_price(value: str, decimal_separator: str = "auto") -> float | None:
    text = str(value)
    for sym, _ in SYMBOLS:
        text = text.replace(sym, " ")
    text = re.sub(r"\b[A-Za-z]{3}\b", " ", text)
    text = re.sub(r"[^\d.,\-+\s   ']", " ", text)
    # the first amount only: "24,00 € 35,00 €" (a sale) is 24, not 24003500; digit groups
    # split by a space, NBSP, thin space or ' ("1 299,00") stay one amount
    match = PRICE_TOKEN.search(text)
    return number(match.group(0), decimal_separator) if match else None


def currency(value: str, default_currency: str | None = None) -> str | None:
    text = str(value)
    for code in re.findall(r"(?<![A-Za-z])([A-Z]{3})(?![A-Za-z])", text):
        if code in ISO_CODES:
            return code
    for sym, code in SYMBOLS:
        if sym in text:
            return code
    if default_currency:
        return DefaultCurrency(default_currency)  # marked: the page named none
    return None


def absolute_url(value: str, base_url: str | None) -> str | None:
    from app.core.urls import absolute

    if not base_url:
        return value if re.match(r"^https?://", value.strip(), re.I) else None
    return absolute(base_url, value)


_ENGLISH_MONTH_NAMES = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)
_ENGLISH_MONTHS = {
    spelling: number
    for number, name in enumerate(_ENGLISH_MONTH_NAMES, 1)
    for spelling in (name, name[:3])
}
_ENGLISH_MONTHS["sept"] = 9
_ENGLISH_DMY = re.compile(r"(?<![\w+-])(\d{1,2})\s+([A-Za-z]{3,9})\.?\s+(\d{4})(?!\w)")
_ENGLISH_MDY = re.compile(
    r"(?<!\w)([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:,\s*|\s+)(\d{4})(?!\w)"
)


def _english_date(text: str) -> str | None:
    """Parse a complete named date without process-wide locale state or guessing."""
    if len(text) > 512:
        return None
    match = _ENGLISH_DMY.search(text)
    if match:
        day, name, year = match.groups()
    else:
        match = _ENGLISH_MDY.search(text)
        if not match:
            return None
        name, day, year = match.groups()
    month = _ENGLISH_MONTHS.get(name.lower())
    if month is None:
        return None
    try:
        return date(int(year), month, int(day)).isoformat()
    except ValueError:
        return None


def to_date(value: str, day_first: bool = False) -> str | None:
    text = str(value).strip()
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        m = re.search(r"(\d{1,4})([./-])(\d{1,2})\2(\d{2,4})", text)
        if not m:
            return _english_date(text)
        a, sep, b, c = m.group(1), m.group(2), m.group(3), m.group(4)
        if len(a) == 4:
            y, mo, d = int(a), int(b), int(c)
        else:
            if sep == "." or day_first:
                d, mo = int(a), int(b)
            else:
                mo, d = int(a), int(b)
            y = int(c)
            if len(c) == 2:
                y += 2000
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        return None


def to_boolean(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    text = str(value).strip()
    low = text.lower()
    if low in TRUE_WORDS:
        return True
    if low in FALSE_WORDS:
        return False
    tail = re.split(r"[/:#]", low.rstrip("/"))[-1]
    if tail in TRUE_AVAIL:
        return True
    if tail in FALSE_AVAIL:
        return False
    return None


SRCSET_ITEM = re.compile(r"\s*(\S+?)(?:\s+(\d+(?:\.\d+)?)([wx]))?\s*(?:,|$)")


def srcset_largest(value: str) -> str | None:
    """The candidate with the largest width (``800w``) or density (``2x``) of a ``srcset``; a
    candidate without a descriptor counts as ``1x``. the full-size image."""
    best, best_key = None, (-1, -1.0)
    for m in SRCSET_ITEM.finditer(value or ""):
        url = m.group(1)
        if not url or url == ",":
            continue
        num = float(m.group(2)) if m.group(2) else 1.0
        key = (1, num) if m.group(3) == "w" else (0, num)
        if key > best_key:
            best, best_key = url.rstrip(","), key
    return best


def regex_replace(value: str, pattern: str, replacement: str, count: int = 0) -> str | None:
    """``re.sub`` (``\\1`` back-references allowed): e.g. remove ``-300x300`` from a thumbnail URL."""
    try:
        return re.sub(pattern, replacement, value, count=count)
    except (re.error, IndexError):
        return None


def apply_transform(value: str, spec: dict, base_url: str | None) -> Any:
    op = spec["op"]
    if op == "regex_replace":
        return regex_replace(value, spec["pattern"], spec.get("replacement", ""), int(spec.get("count", 0)))
    if op == "srcset_largest":
        return srcset_largest(value)
    if op == "join":
        return value  # list-level: applied by the field (mapping.extract_field)
    if op == "trim":
        return trim(value)
    if op == "regex_extract":
        return regex_extract(value, spec["pattern"], spec.get("group", 1))
    if op == "number":
        return number(value, spec.get("decimal_separator", "auto"))
    if op == "price":
        return price(value, spec.get("decimal_separator", "auto"))
    if op == "currency":
        return currency(value, spec.get("default_currency"))
    if op == "absolute_url":
        return absolute_url(value, base_url)
    if op == "date":
        return to_date(value, spec.get("day_first", False))
    if op == "html_clean":
        return clean_html(value, base_url)
    return None


def _num_text(value: float | int) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def coerce(value: Any, field_type: str, base_url: str | None) -> Any:
    """Final coercion; ``None`` = no value."""
    if value is None:
        return None
    if isinstance(value, PriceRange) and field_type in ("price", "number"):
        return value  # the lowest of a range; the record says it is a range
    if isinstance(value, DefaultCurrency) and field_type == "currency":
        return value if re.fullmatch(r"[A-Z]{3}", value) else None
    if field_type == "boolean":
        return to_boolean(value)
    if isinstance(value, bool):
        return None if field_type not in ("text",) else ("true" if value else "false")
    if isinstance(value, (int, float)):
        if field_type in ("number", "price"):
            return float(value)
        if field_type == "text":
            return _num_text(value)
        value = _num_text(value)
    if not isinstance(value, str):
        return None
    if field_type == "text":
        out = trim(value)
        return out or None
    if field_type == "html":
        out = clean_html(value, base_url)
        return out if out and out.strip() else None
    if field_type == "number":
        return number(value)
    if field_type == "price":
        return price(value)
    if field_type == "currency":
        stripped = value.strip()
        if re.fullmatch(r"[A-Z]{3}", stripped):
            return stripped
        return currency(value)
    if field_type in ("url", "image_url"):
        return absolute_url(value, base_url)
    if field_type == "date":
        return to_date(value)
    return None
