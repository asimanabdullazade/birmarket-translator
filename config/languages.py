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
    {"code": "tr", "name": "Turkish"},
    {"code": "ru", "name": "Russian"},
    {"code": "es", "name": "Spanish"},
    {"code": "fr", "name": "French"},
    {"code": "de", "name": "German"},
    {"code": "it", "name": "Italian"},
    {"code": "pt", "name": "Portuguese"},
    {"code": "ar", "name": "Arabic"},
    {"code": "zh", "name": "Chinese (Mandarin)"},
    {"code": "ja", "name": "Japanese"},
    {"code": "ko", "name": "Korean"},
    {"code": "hi", "name": "Hindi"},
]

LANGUAGE_CODES = {lang["code"] for lang in SUPPORTED_LANGUAGES}


def is_supported(code: str) -> bool:
    return code in LANGUAGE_CODES
