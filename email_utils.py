"""
email_utils.py — Shared email header helpers for Persevex.

All outbound email 'To' headers must contain the full, validated
recipient email address (including domain). This module provides a
single utility that every service must use when constructing
message["To"] so that no future change can accidentally obscure the
domain (e.g. by setting the display name to the email username prefix).
"""

import re
from typing import Optional


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def is_valid_email(email: str) -> bool:
    """Return True when *email* looks like a syntactically valid address."""
    return bool(_EMAIL_RE.match(str(email or "").strip()))


def format_to_header(name: Optional[str], email: str) -> str:
    """
    Build a safe RFC-5322 ``To`` header value.

    Rules
    -----
    * *email* must be a non-empty, fully-qualified address (``user@domain.tld``).
      A ``ValueError`` is raised if it is missing or invalid.
    * If *name* is absent, empty, identical to *email*, or identical to the
      username prefix (the part before ``@``), the header is set to the bare
      address so email clients never display a truncated ``username`` label
      instead of the full address.
    * If *name* is a genuine human name that differs from the email/prefix,
      the header is formatted as ``"Name <email@domain.tld>"`` (RFC 5322).
    * The returned string always includes the full ``@domain.tld`` portion.

    Examples
    --------
    >>> format_to_header("Rahul Kumar", "rahul.kumar@example.com")
    'Rahul Kumar <rahul.kumar@example.com>'
    >>> format_to_header("rahul.kumar", "rahul.kumar@example.com")
    'rahul.kumar@example.com'
    >>> format_to_header("", "rahul.kumar@example.com")
    'rahul.kumar@example.com'
    >>> format_to_header(None, "student@gmail.com")
    'student@gmail.com'
    """
    email = str(email or "").strip().lower()
    if not email or not is_valid_email(email):
        raise ValueError(
            f"A valid recipient email address is required; got: {email!r}"
        )

    name = str(name or "").strip()
    prefix = email.split("@")[0]

    # Treat the name as absent when it is empty, equal to the full email,
    # or equal to the username prefix — any of these would hide the domain.
    if not name or name.lower() in (email, prefix):
        return email

    return f"{name} <{email}>"
