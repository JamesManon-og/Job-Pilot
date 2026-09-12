"""Small helpers shared by browser adapters.

Site markup changes; every extractor takes a list of selectors and returns the
first hit, so an adapter keeps working when one class name is renamed and
fails loudly (not silently) when the whole layout changes.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

_RELATIVE = re.compile(
    r"(?P<n>\d+)\s*(?P<unit>m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days|w|wk|"
    r"week|weeks|mo|month|months)\b",
    re.IGNORECASE,
)
_UNIT_HOURS = {"m": 1 / 60, "h": 1, "d": 24, "w": 168, "mo": 720}

# JS run in the page: first selector that yields a non-empty value wins.
TEXT_OF_JS = """
(args) => {
  const root = args.root ? document.querySelector(args.root) : document;
  if (!root) return null;
  for (const selector of args.selectors) {
    const el = root.querySelector(selector);
    const text = el ? (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim() : '';
    if (text) return text;
  }
  return null;
}
"""

ATTR_OF_JS = """
(args) => {
  const root = args.root ? document.querySelector(args.root) : document;
  if (!root) return null;
  for (const selector of args.selectors) {
    const el = root.querySelector(selector);
    const value = el ? el.getAttribute(args.attribute) : null;
    if (value) return value;
  }
  return null;
}
"""


def clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def first_number(text: str) -> int | None:
    match = re.search(r"\d[\d,]*", text.replace(" ", ""))
    return int(match.group().replace(",", "")) if match else None


def parse_salary(text: str) -> tuple[int | None, int | None]:
    """ "₱40,000 – ₱60,000 per month" -> (40000, 60000). Returns (None, None) if unclear."""
    numbers = [int(n.replace(",", "")) for n in re.findall(r"\d[\d,]{2,}", text or "")]
    numbers = [n for n in numbers if n >= 1000]
    if not numbers:
        return None, None
    if len(numbers) == 1:
        return numbers[0], None
    return min(numbers), max(numbers)


def parse_relative_date(text: str, *, now: datetime | None = None) -> datetime | None:
    """ "3d ago", "Posted 2 weeks ago", "30+ days ago" -> an approximate timestamp."""
    match = _RELATIVE.search(text or "")
    if not match:
        return None
    unit = match.group("unit").lower()
    key = "mo" if unit.startswith("mo") else unit[0]
    hours = _UNIT_HOURS.get(key)
    if hours is None:
        return None
    return (now or datetime.now(UTC)) - timedelta(hours=hours * int(match.group("n")))


def parse_timestamp(value: str) -> datetime | None:
    """ISO-8601 or "2026-09-10 08:49:24" / "Sep 10, 2026"."""
    value = clean(value)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def as_dicts(value: Any) -> list[dict[str, Any]]:
    """page.evaluate() results are untyped; keep only well-formed rows."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]
