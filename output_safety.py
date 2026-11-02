"""
Spreadsheet output safety — prevent formula / DDE injection (CWE-1236).

Values written to results.xlsx come from scraped web pages and LLM output. openpyxl stores any
string starting with "=" as a live formula, so a page could plant e.g. =HYPERLINK(...) or a DDE
payload in the output. Prefixing such strings with an apostrophe forces them to plain text.
"""

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n")


def excel_safe(value):
    """Return value unchanged unless it is a string a spreadsheet could interpret as a formula."""
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value
