"""
Pure helper functions for Phase 8 (incremental partial translation).

No I/O, no provider/websocket dependency -- these are trivially
unit-testable in isolation (see _verify_stability.py). Everything here
operates on already-transcribed text, never audio: the incremental
translation pipeline reacts to two *consecutive* partial transcripts
(produced exactly as before by TranslationProvider.transcribe_partial(),
which re-transcribes the whole phrase-so-far audio each round -- see
base.py) rather than doing any new/incremental speech recognition of its
own.

The "stability" idea (a word-level "local agreement" policy): a word is
trusted enough to translate once it has shown up in the same position in
two consecutive partial transcripts. That's the cheapest signal available
given this app's providers are batch re-transcribers, not token-stream
decoders with a monotonic prefix guarantee -- so a holdback margin
(trimming a few trailing words off the agreed prefix) and a minimum
commit size (not bothering to translate a single new word at a time) are
both needed as safety/cost margins, not just tuning knobs. See
"Using streaming translation" in the README for how to tune both from
real logs, the same way VAD_END_SILENCE_MS was originally tuned.
"""

from __future__ import annotations

import string


def tokenize(text: str) -> list[str]:
    """Whitespace-delimited word tokenization. Fine for the app's current
    language list (en/az/ru are all space-delimited, Cyrillic included) --
    would need revisiting (e.g. a real segmenter) if a non-whitespace-
    delimited language (Thai, Chinese, ...) were ever added."""
    if not text:
        return []
    return text.split()


def normalize_word(word: str) -> str:
    """Casefold + strip surrounding punctuation, for *comparison only* --
    e.g. "Hello" and "hello," should count as the same word when checking
    whether two partial hypotheses agree, even though the literal (cased,
    punctuated) form is what actually gets used once a word is committed
    (see stable_prefix_len's caller in handlers.py: it takes words from the
    *newer* hypothesis, which had more audio context and is more likely to
    have correct casing/punctuation)."""
    return word.strip(string.punctuation + "…").casefold()  # … = "…"


def longest_common_prefix_len(a: list[str], b: list[str]) -> int:
    """Length of the longest run of *normalized*-equal words at the start
    of both lists."""
    n = min(len(a), len(b))
    i = 0
    while i < n and normalize_word(a[i]) == normalize_word(b[i]):
        i += 1
    return i


def stable_prefix_len(committed_len: int, lcp_len: int, holdback_words: int, min_commit_words: int) -> int:
    """Given how much is already committed, how long the two most recent
    partial hypotheses agree for (lcp_len), a trailing holdback margin, and
    the minimum newly-stabilized word count worth bothering to translate,
    return the new stable_len to commit up to (>= committed_len -- this is
    append-only, see the module docstring and handlers.py's caller for why
    a shrinking LCP is not treated as "un-committing" anything).

    Returns committed_len unchanged if nothing new clears the bar."""
    candidate = max(0, lcp_len - holdback_words)
    if candidate <= committed_len:
        return committed_len
    if candidate - committed_len < min_commit_words:
        return committed_len
    return candidate


def reconcile_final_tts_text(committed_translation_text: str, final_translation_text: str, min_coverage: float) -> str:
    """What to actually speak once the *authoritative* final translation is
    available, given some prefix of the translation may already have been
    spoken incrementally.

    Deliberately binary (confident-prefix-or-full-fallback), not a
    fine-grained diff: the final-translation prompt explicitly encourages
    reordering/rephrasing for natural spoken output (see the README's
    "перевести/передвинуть" example), so the authoritative final
    translation frequently has different word order than the
    incrementally-committed text even when both are correct translations
    of the same meaning. A raw word-diff between two differently-phrased
    sentences can find a spuriously short common prefix and speak
    final_words[lcp_len:], producing garbled/duplicated audio -- likely
    common, not rare, given the prompt encourages reordering.

    So: only trust and skip the committed prefix if the two agree on at
    least `min_coverage` of the committed text's own length; otherwise
    speak the *entire* final translation, identical to pre-Phase-8
    behavior. Worst case is a rare full repeat -- exactly what already
    happens today for every phrase -- never a botched partial cut."""
    if not committed_translation_text:
        return final_translation_text
    committed_words = tokenize(committed_translation_text)
    final_words = tokenize(final_translation_text)
    if not committed_words:
        return final_translation_text
    lcp_len = longest_common_prefix_len(committed_words, final_words)
    if (lcp_len / len(committed_words)) >= min_coverage:
        return " ".join(final_words[lcp_len:])
    return final_translation_text
