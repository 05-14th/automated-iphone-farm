"""Address normalization must match the SQL function exactly.

If these two ever diverge, suppression silently leaks: a STOP recorded one way
stops matching a contact stored another way.
"""

from __future__ import annotations

import pytest

from app.normalize import looks_like_address, normalize_address


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # The four worked examples from docs/BRIDGE-SCHEMA.md §6, verbatim.
        ("  MeIdY@ClarioCapital.COM ", "meidy@clariocapital.com"),
        ("(778) 323-1234", "+17783231234"),
        ("+1 778-323-1234", "+17783231234"),
        ("17783231234", "+17783231234"),
        # Plain forms.
        ("meidy765@gmail.com", "meidy765@gmail.com"),
        ("MEIDY765@Gmail.com", "meidy765@gmail.com"),
        ("+17783231234", "+17783231234"),
        ("7783231234", "+17783231234"),
        # International: a leading + means the digits are already complete.
        ("+44 20 7946 0958", "+442079460958"),
        # Empty-ish input.
        ("", None),
        ("   ", None),
        (None, None),
        # Not phone-shaped and not an email: deterministic lowercase fallback.
        ("not-an-address", "not-an-address"),
    ],
)
def test_normalize(raw, expected):
    assert normalize_address(raw) == expected


def test_case_difference_collapses_to_one_address():
    """This is the property suppression depends on."""
    assert normalize_address("MEIDY765@Gmail.com") == normalize_address("  meidy765@gmail.com ")


def test_phone_formats_collapse_to_one_address():
    forms = ["(778) 323-1234", "778-323-1234", "+1 778 323 1234", "17783231234", "+17783231234"]
    assert len({normalize_address(f) for f in forms}) == 1


@pytest.mark.parametrize(
    ("raw", "ok"),
    [
        ("meidy765@gmail.com", True),
        ("+17783231234", True),
        ("(778) 323-1234", True),
        ("not-an-address", False),
        ("@example.com", False),
        ("someone@localhost", False),  # no dot in the domain
        ("123", False),  # too short to be a phone number
        ("", False),
    ],
)
def test_looks_like_address(raw, ok):
    assert looks_like_address(raw) is ok
