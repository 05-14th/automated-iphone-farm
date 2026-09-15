"""Python port of the database's `public.normalize_address(text)`.

The database remains the authority: `contacts` and `suppressions` both run the
SQL version in a BEFORE INSERT/UPDATE trigger. This copy exists so the API can
*look up* by normalized address and reject obvious garbage before a round trip.

It must stay byte-identical in behaviour to
`supabase/migrations/20260914120100_phase11_normalize_and_guards.sql`:

    '  MeIdY@ClarioCapital.COM '  ->  'meidy@clariocapital.com'
    '(778) 323-1234'              ->  '+17783231234'
    '+1 778-323-1234'             ->  '+17783231234'
    '17783231234'                 ->  '+17783231234'
"""

from __future__ import annotations

import re

_NON_DIGITS = re.compile(r"[^0-9]")


def normalize_address(address: str | None) -> str | None:
    """Canonicalize a recipient identifier. Returns None for empty input."""
    if address is None:
        return None

    trimmed = address.strip()
    if not trimmed:
        return None

    # Email identity: lowercase + trim. This is the primary address shape in this
    # architecture - the sender identity is an Apple Account email.
    if "@" in trimmed:
        return trimmed.lower()

    digits = _NON_DIGITS.sub("", trimmed)
    if not digits:
        # Neither an email nor anything phone-shaped. Deterministic fallback,
        # matching the SQL function exactly.
        return trimmed.lower()

    if trimmed.startswith("+"):
        return "+" + digits
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return "+" + digits


def looks_like_address(address: str | None) -> bool:
    """True if the value normalizes to something that could plausibly be routed.

    Deliberately loose - the database is the real gate. This only catches
    input that is obviously not an address at all.
    """
    normalized = normalize_address(address)
    if not normalized:
        return False
    if "@" in normalized:
        local, _, domain = normalized.partition("@")
        return bool(local) and "." in domain and not domain.startswith(".")
    return bool(re.fullmatch(r"\+\d{7,15}", normalized))
