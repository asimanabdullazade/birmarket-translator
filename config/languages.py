"""
Central list of languages supported by the translator UI and backend.

Keeping this in one place means the frontend language dropdowns and the
backend provider adapters (translation/*.py) always agree on the set of
valid language codes.
"""

from typing import TypedDict


class Language(TypedDict):
    code: str  # BCP-47 / ISO 639-1 style code, used as the wire value
    name: str  # Human readable label shown in the UI


SUPPORTED_LANGUAGES: list[Language] = [
    {"code": "en", "name": "English"},
    {"code": "az", "name": "Azerbaijani"},
    {"code": "ru", "name": "Russian"},
]

LANGUAGE_CODES = {lang["code"] for lang in SUPPORTED_LANGUAGES}


def is_supported(code: str) -> bool:
    return code in LANGUAGE_CODES
