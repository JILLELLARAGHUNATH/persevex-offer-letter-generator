"""
email_utils.py — Shared email header helpers for Persevex.

Policy: the outgoing ``To`` header must contain *only* the recipient's
complete, validated email address (including domain).  Display names are
never included so that email clients always show the full address, e.g.

    To: 9922005112@klu.ac.in          ✓ correct
    To: DEMO <9922005112@klu.ac.in>   ✗ rejected – hides domain in some clients
"""

import re
from typing import Optional


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def is_valid_email(email: str) -> bool:
    """Return True when *email* looks like a syntactically valid address."""
    return bool(_EMAIL_RE.match(str(email or "").strip()))


def format_to_header(name: Optional[str], email: str) -> str:
    """
    Return the validated recipient email address for use as a ``To`` header.

    The *name* / display-name argument is accepted for call-site compatibility
    but is intentionally ignored — the ``To`` header will always be the plain
    email address so no email client can hide the domain behind a display name.

    Parameters
    ----------
    name : str or None
        Candidate display name.  **Ignored.**
    email : str
        Recipient email address.  Must be non-empty and syntactically valid.

    Returns
    -------
    str
        The bare, lower-cased email address, e.g. ``"student@gmail.com"``.

    Raises
    ------
    ValueError
        If *email* is missing or not a valid address.

    Examples
    --------
    >>> format_to_header("Rahul Kumar", "Rahul.Kumar@Example.COM")
    'rahul.kumar@example.com'
    >>> format_to_header("", "student@gmail.com")
    'student@gmail.com'
    >>> format_to_header(None, "9922005112@klu.ac.in")
    '9922005112@klu.ac.in'
    """
    email = str(email or "").strip().lower()
    if not email or not is_valid_email(email):
        raise ValueError(
            f"A valid recipient email address is required; got: {email!r}"
        )
    return email
