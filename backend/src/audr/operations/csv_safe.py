"""Spreadsheet-formula neutralization for untrusted labels and metadata (T085 / US4).

Strings that begin with ``=``, ``+``, ``-``, ``@``, ``\\t``, or ``\\r`` can be
interpreted as formulas when a CSV is opened in Excel or Google Sheets.  The
standard mitigation is to prepend a single-quote (``'``), which forces the cell
to be treated as literal text.

Examples::

    >>> sanitize("=SUM(A1)")
    "'=SUM(A1)"
    >>> sanitize("+42")
    "'+42"
    >>> sanitize("-1")
    "'-1"
    >>> sanitize("@user")
    "'@user"
    >>> sanitize("hello")
    'hello'
    >>> sanitize("")
    ''
    >>> sanitize(None)
    ''
    >>> sanitize_row(["=A1", None, "safe"])
    ["'=A1", '', 'safe']
    >>> is_safe("=bad")
    False
    >>> is_safe("good")
    True
"""

from __future__ import annotations

_FORMULA_PREFIXES: frozenset[str] = frozenset({"=", "+", "-", "@", "\t", "\r"})


def sanitize(value: str | None) -> str:
    """Neutralize spreadsheet formula injection in a CSV cell value.

    Returns empty string for None. Prepends a single-quote if the value
    starts with a formula trigger character.
    """
    if value is None:
        return ""
    if value and value[0] in _FORMULA_PREFIXES:
        return "'" + value
    return value


def sanitize_row(row: list[str | None]) -> list[str]:
    """Apply sanitize() to every cell in a row."""
    return [sanitize(cell) for cell in row]


def is_safe(value: str) -> bool:
    """Return True if the value does not start with a formula trigger character."""
    return not (value and value[0] in _FORMULA_PREFIXES)
