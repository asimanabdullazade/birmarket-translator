"""
Splits translated text into small, independently-speakable chunks for
text-to-speech (Step 6).

Why chunk at all: sending one TTS request for an entire translated phrase
and waiting for the whole thing before playing anything means the person
listening waits out the *entire* synthesis latency before hearing a single
word. Splitting on natural sentence/clause boundaries first lets the first
chunk start playing while later chunks are still being synthesized -- see
"Start playback before the entire sentence is generated" in Step 6, and
TranslationProvider.synthesize_speech in base.py for how each chunk is
streamed to the client as soon as it's ready.

This is deliberately simple regex-based splitting, not real NLP sentence
segmentation -- the phrases this app translates are already short (single
spoken utterances from the VAD segmenter, see backend/audio/segmenter.py),
so a full sentence-boundary-detection library would be overkill.
"""

from __future__ import annotations

import re

# Split after sentence-ending punctuation (keeping it attached to the
# preceding chunk) followed by whitespace or end-of-string.
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?…])\s+")

# If a sentence-level chunk is still long, split it further on commas/
# semicolons/colons (still keeping the punctuation attached) so a single
# long, clause-heavy run-on sentence doesn't become one giant TTS call.
_CLAUSE_BOUNDARY = re.compile(r"(?<=[,;:])\s+")

# A sentence at or under this length isn't worth splitting into clauses --
# not enough to gain from a head start, and TTS quality/prosody tends to
# suffer on very short fragments.
_CLAUSE_SPLIT_THRESHOLD_CHARS = 60

# Chunks shorter than this (in characters) are merged into the previous
# chunk instead of being sent to TTS on their own -- a lone word or two
# (e.g. a stray "Yes." from an aggressive split) makes for a jarring,
# clipped-sounding TTS call and isn't worth the extra request/latency.
_MIN_CHUNK_CHARS = 12

# Each chunk is a separate TTS round trip, so the number of chunks is a
# direct latency cost. Splitting on every sentence turned one paragraph
# into ~12 calls and ~21s of synthesis in a real meeting.
#
# The reason to split at all is time-to-first-audio: playback can start
# once chunk 1 is back. That argument applies to the FIRST chunk and
# essentially not at all to the rest, which are only needed by the time
# the earlier ones have finished playing. So chunk 1 stays small and the
# remainder are merged into far larger groups.
_FIRST_CHUNK_TARGET_CHARS = 90
_LATER_CHUNK_TARGET_CHARS = 320


def split_for_speech(text: str) -> list[str]:
    """
    Split already-translated text into chunks suitable for separate,
    independently-playable TTS calls. Returns [] for empty/whitespace-only
    input; otherwise always returns at least one chunk (the whole text, if
    it can't usefully be split further).
    """
    text = text.strip()
    if not text:
        return []

    sentences = [s for s in _SENTENCE_BOUNDARY.split(text) if s]
    chunks: list[str] = []
    for sentence in sentences:
        if len(sentence) <= _CLAUSE_SPLIT_THRESHOLD_CHARS:
            chunks.append(sentence)
        else:
            chunks.extend(c for c in _CLAUSE_BOUNDARY.split(sentence) if c)

    # Merge short trailing fragments into the previous chunk rather than
    # sending them to TTS as their own tiny, choppy-sounding request.
    merged: list[str] = []
    for chunk in chunks:
        if merged and len(chunk) < _MIN_CHUNK_CHARS:
            merged[-1] = f"{merged[-1]} {chunk}"
        else:
            merged.append(chunk)

    if not merged:
        return [text]

    # Group the merged pieces up to a target size, keeping the first group
    # short so audio starts quickly.
    grouped: list[str] = []
    for piece in merged:
        # `len(grouped) <= 1` not `not grouped`: while the FIRST group is
        # being filled it already exists in the list, so testing for
        # emptiness switched to the large target immediately and the
        # first chunk grew to ~300 chars -- defeating the whole point of
        # keeping it short.
        target = _FIRST_CHUNK_TARGET_CHARS if len(grouped) <= 1 else _LATER_CHUNK_TARGET_CHARS
        if grouped and len(grouped[-1]) + 1 + len(piece) <= target:
            grouped[-1] = f"{grouped[-1]} {piece}"
        else:
            grouped.append(piece)
    return grouped
