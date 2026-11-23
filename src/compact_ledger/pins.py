"""Deterministic user constraint extraction and superseding correction engine.

Zero third-party dependencies, zero LLM calls, strictly linear-time scanning
(ReDoS-safe regexes only). Extracts verbatim user directives, negative
constraints, and superseding corrections from ordered transcript turns while
filtering conversational chatter and preserving explicit manual pins.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from compact_ledger.models import (
    PinEntry,
    UserTurn,
    make_pin_id,
    normalize_pin_text,
    sanitize_unicode,
)

__all__ = [
    "extract_pins",
    "is_chatter",
    "is_constraint_or_directive",
    "is_negative_constraint",
    "is_override_turn",
    "strip_fenced_code_blocks",
]

# ---------------------------------------------------------------------------
# Unicode / fenced-code helpers (linear time, zero catastrophic backtracking)
# ---------------------------------------------------------------------------

_FENCE_OPEN_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
_SMART_APOSTROPHE_RE = re.compile(r"[\u2018\u2019\u2032]")
_DASH_VARIANTS_RE = re.compile(r"[\u2013\u2014]")
_WORD_TOKEN_RE = re.compile(r"[a-z0-9]+")
_CAMEL_SPLIT_RE = re.compile(r"([a-z0-9])([A-Z])")
_SENTENCE_SPLIT_RE = re.compile(r"(?:\n+|(?<=[.!?])\s+)")
_CLAUSE_SPLIT_RE = re.compile(r"[.!?;,\n]+")

# ---------------------------------------------------------------------------
# Chatter / acknowledgement filtering
# ---------------------------------------------------------------------------

_CHATTER_PHRASES: frozenset[str] = frozenset(
    {
        "ok",
        "okay",
        "k",
        "kk",
        "ok thanks",
        "okay thanks",
        "ok thank you",
        "okay thank you",
        "ok great",
        "okay great",
        "ok cool",
        "okay cool",
        "ok nice",
        "ok good",
        "ok perfect",
        "ok got it",
        "okay got it",
        "ok sounds good",
        "okay sounds good",
        "ok looks good",
        "okay looks good",
        "ok go ahead",
        "okay go ahead",
        "ok proceed",
        "okay proceed",
        "ok continue",
        "okay continue",
        "thanks",
        "thank you",
        "thx",
        "ty",
        "tysm",
        "thanks a lot",
        "thank you so much",
        "thanks very much",
        "many thanks",
        "appreciate it",
        "much appreciated",
        "cheers",
        "yes",
        "yep",
        "yup",
        "yeah",
        "ya",
        "y",
        "yes please",
        "yep please",
        "no",
        "nope",
        "nah",
        "n",
        "sure",
        "sure thing",
        "of course",
        "sounds good",
        "sounds great",
        "looks good",
        "looks great",
        "looks right",
        "looks good to me",
        "looks great to me",
        "that looks good",
        "that looks great",
        "that actually looks good",
        "that actually looks great",
        "lgtm",
        "sgtm",
        "gtm",
        "ship it",
        "continue",
        "please continue",
        "keep going",
        "carry on",
        "go ahead",
        "please go ahead",
        "go for it",
        "go ahead and ship it",
        "go ahead and proceed",
        "proceed",
        "please proceed",
        "next",
        "do it",
        "run it",
        "try it",
        "try again",
        "go",
        "nice",
        "very nice",
        "great",
        "awesome",
        "perfect",
        "cool",
        "sweet",
        "neat",
        "brilliant",
        "excellent",
        "wonderful",
        "fantastic",
        "amazing",
        "good",
        "good job",
        "nice job",
        "well done",
        "great job",
        "got it",
        "makes sense",
        "that makes sense",
        "understood",
        "roger",
        "roger that",
        "ack",
        "acknowledged",
        "copy",
        "copy that",
        "all good",
        "fine",
        "alright",
        "all right",
        "right",
        "agreed",
        "i agree",
        "works for me",
        "that works",
        "that worked",
        "it worked",
        "that actually worked",
        "it actually worked",
        "actually worked",
        "actually that worked",
        "nice work",
        "good stuff",
        "done",
        "all set",
        "awesome thanks",
        "great thanks",
        "cool thanks",
        "perfect thanks",
        "nice thanks",
        "thanks again",
    }
)

_CHATTER_TRIM_RE = re.compile(r"^[\W_]+|[\W_]+$")
_LEADING_FILLER_RE = re.compile(
    r"^(?:(?:ok(?:ay)?|yes|yep|yeah|sure|thanks?|thank\s+you|great|nice|cool|awesome|"
    r"alright|all\s+right|got\s+it|lgtm|sounds\s+good|looks\s+good|wait|hey|hi|hello|"
    r"and|also|plus|btw|by\s+the\s+way|one\s+more\s+thing|one\s+more\s+rule)\b[\s,:;-]*){1,3}",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Question / non-directive observation guards
# ---------------------------------------------------------------------------

_INFO_QUESTION_START_RE = re.compile(
    r"^(?:what|where|when|why|who|whom|whose|which|how|"
    r"is|are|was|were|am|do|does|did|have|has|had|should|"
    r"would\s+(?:it|that|this|we|there)|"
    r"could\s+(?:it|that|this|we|there)|"
    r"can\s+(?:it|that|this|we|there)|"
    r"will\s+(?:it|that|this|there)|"
    r"might|may\s+i)\b",
    re.IGNORECASE,
)

_POLITE_REQUEST_START_RE = re.compile(
    r"^(?:can|could|would|will)\s+you\s+(?:please\s+)?",
    re.IGNORECASE,
)

_NON_DIRECTIVE_REQUEST_VERB_RE = re.compile(
    r"^(?:explain|tell\s+me|show\s+me|describe|list|summarize|clarify|confirm|"
    r"check\s+(?:if|whether|why)|see\s+(?:if|whether|why)|find\s+out|look\s+up)\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Negative constraint detection
# ---------------------------------------------------------------------------

_NEG_STRONG_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bdo\s+not\b", re.IGNORECASE),
    re.compile(r"\bdon't\b", re.IGNORECASE),
    re.compile(r"\bmust\s+not\b|\bmustn't\b|\bshall\s+not\b", re.IGNORECASE),
    re.compile(r"\bshould\s+not\b|\bshouldn't\b", re.IGNORECASE),
    re.compile(r"\bnever\b", re.IGNORECASE),
    re.compile(
        r"\bavoid\s+(?:using|modifying|touching|changing|adding|breaking|introducing|"
        r"calling|mutating|rewriting|deleting|removing|importing|editing|installing|"
        r"external|third[- ]party|global|hardcod\w*|[a-z][a-z0-9_-]*ing\b)",
        re.IGNORECASE,
    ),
    re.compile(r"(?:^|[.!?;:\n]\s*)(?:please\s+)?avoid\b", re.IGNORECASE),
    re.compile(
        r"\b(?:forbid(?:den)?|prohibit(?:ed)?|disallow(?:ed)?|not\s+allowed\s+to|"
        r"under\s+no\s+circumstances|hands\s+off)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:cannot|can't)\s+(?:break|modify|touch|change|use|depend|delete|remove|"
        r"commit|push|alter|mutate|drop|exceed|import|call|expose|leak)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bno\s+(?:third[- ]party|external|llm|network|breaking|extra|additional|"
        r"changes?\s+to|edits?\s+to|modifications?\s+to|writes?\s+to|dependencies|"
        r"deps|mocking|mocks|skipping|skipped)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bzero\s+(?:(?:third[- ]party|external|runtime)\s+){1,2}dependenc(?:y|ies)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bwithout\s+(?:modifying|touching|changing|breaking|adding|using|installing|"
        r"mutating|altering|deleting|removing|calling)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bstop\s+(?:using|modifying|touching|changing|editing|adding|rewriting|calling)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bleave\s+\S+(?:\s+\S+){0,4}\s+(?:alone|untouched|as[- ]is|unchanged)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:please|could\s+you|can\s+you|would\s+you)\s+not\b", re.IGNORECASE),
)

_NEG_DONT_FALSE_POSITIVE_RE = re.compile(
    r"\b(?:why\s+don't|"
    r"(?:i|we)\s+(?:just\s+)?don't\s+(?:know|think|see|understand|remember|believe|"
    r"mind|care|have\s+a|get\s+why|get\s+how)|"
    r"(?:if|when|whenever|since|because|although|though)\s+(?:you|we|they|it)\s+don't|"
    r"if\s+(?:it|this|that)\s+does\s+not)\b",
    re.IGNORECASE,
)

_NEG_NEVER_FALSE_POSITIVE_RE = re.compile(
    r"\b(?:never\s*mind|"
    r"(?:i|we|it|that|this|he|she|they)(?:'ve|'s|'d|\s+(?:have|has|had))?\s+never\s+"
    r"(?:seen|worked|happened|got|gotten|ran|run|failed|crashed|been|had|heard|tried|"
    r"noticed|encountered|used\s+(?:it|this|that)\s+before))\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Positive directive / constraint detection
# ---------------------------------------------------------------------------

_CONSTRAINT_KEYWORDS_RE = re.compile(
    r"\b(?:must|have\s+to|need(?:s)?\s+to|required\s+to|always|strictly|exclusively|"
    r"ensure|make\s+sure|guarantee|enforce|preserve|maintain|stick\s+(?:to|with)|"
    r"adhere\s+to|comply\s+with|remember\s+(?:to|that)|"
    r"backward[s]?[- ]compatible|compatible\s+with|stdlib\s+only|zero[- ]dependenc(?:y|ies)|"
    r"fail[- ]open|deterministic)\b|"
    r"(?:^|[.!?\n]\s*)(?:requirement|constraint|rule|important|note|policy|invariant)\s*:",
    re.IGNORECASE,
)

_RESTRICTIVE_DIRECTIVE_RE = re.compile(
    r"\b(?:only\s+(?:use|modify|edit|touch|change|return|accept|allow|support|import|"
    r"run|update|write|target|keep|emit|read|output|print)|"
    r"use\s+only|"
    r"limit\s+(?:it\s+|this\s+|the\s+\w+\s+)?to|"
    r"stay\s+(?:under|below|within)|"
    r"keep\s+(?:it|this|that|the|all|our|every|runtime|output|changes?|diffs?|deps?|"
    r"dependencies|compatibility|under|below|within|at|to|in|backward|public|api|tests?))\b",
    re.IGNORECASE,
)

_IMPERATIVE_VERBS = (
    "use|keep|set|format|output|return|write|store|save|add|remove|delete|rename|move|"
    "pin|limit|cap|enforce|preserve|maintain|ensure|avoid|prevent|block|reject|validate|"
    "sanitize|escape|sort|deduplicate|default|switch|change|replace|update|implement|"
    "refactor|migrate|convert|support|include|exclude|skip|ignore|restrict|require|"
    "prefer|treat|handle|emit|parse|render|configure|enable|disable|lock|freeze|bump|"
    "downgrade|upgrade|assume|target|apply|stick|follow|match|respect|honor|retain|"
    "drop|omit|strip|wrap|prefix|suffix|encode|decode|serialize|deserialize|pass|bind|"
    "route|redirect|map|filter|group|split|merge|join|cache|flush|close|open|load|read|"
    "run|execute"
)

_IMPERATIVE_START_RE = re.compile(
    rf"^(?:(?:please|kindly|let's|lets|we\s+should|you\s+should|we\s+must|you\s+must|"
    rf"be\s+sure\s+to|go\s+ahead\s+and)\s+)?(?:{_IMPERATIVE_VERBS})\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Superseding correction / override detection (linear-time patterns)
# ---------------------------------------------------------------------------

_EXPLICIT_OVERRIDE_HEADER_RE = re.compile(
    r"(?:^|[.!?;:\n]\s*)(?:correction|override|change\s+of\s+plan|new\s+rule|"
    r"revised\s+rule|updated\s+rule|retract(?:ion)?)\s*[:\-]",
    re.IGNORECASE,
)

_UPDATE_HEADER_RE = re.compile(
    r"(?:^|[.!?;:\n]\s*)update\s*:\s*\S",
    re.IGNORECASE,
)

_SOFT_ACTUALLY_RE = re.compile(
    r"(?:^|[.!?;,\n]\s*)(?:no\s*,\s*|wait\s*,\s*|sorry\s*,\s*)?actually\b",
    re.IGNORECASE,
)

_HARD_NO_ACTUALLY_RE = re.compile(
    r"(?:^|[.!?;:\n]\s*)(?:no\s*,\s*actually|actually\s*,?\s*no\b|"
    r"wait\s*,\s*actually|on\s+second\s+thought)\b",
    re.IGNORECASE,
)

_IGNORE_FORGET_ABOUT_RE = re.compile(
    r"\b(?:ignore|forget|disregard|drop|cancel|undo|revert|retract)\s+"
    r"(?:what\s+i\s+said\s+(?:earlier|before|previously)|"
    r"(?:my|the)\s+(?:previous|earlier|prior|last|old|original)\s+"
    r"(?:instruction|instructions|constraint|constraints|rule|rules|requirement|"
    r"requirements|message|directive|directives|note|pin|cutoff|date|setting|config)|"
    r"my\s+last\s+(?:message|instruction|rule|constraint)|"
    r"that\s+(?:earlier|previous|last)\s+(?:instruction|rule|constraint|requirement))"
    r"(?:\s+(?:about|regarding|on|for|with)\s+([^.!?;,\n]{1,80}))?",
    re.IGNORECASE,
)

_FORGET_ABOUT_DIRECT_RE = re.compile(
    r"\b(?:ignore|forget|disregard)\s+(?:about\s+)?the\s+([^.!?;,\n]{1,60})\s+"
    r"(?:rule|constraint|requirement|instruction|cutoff|limit|setting)\b",
    re.IGNORECASE,
)

_SCRATCH_THAT_RE = re.compile(
    r"(?:^|[.!?;,\n]\s*)(?:scratch\s+that|strike\s+that|belay\s+that|cancel\s+that|"
    r"disregard\s+that|forget\s+that|ignore\s+that)\b",
    re.IGNORECASE,
)

_NEVER_MIND_RE = re.compile(
    r"(?:^|[.!?;,\n]\s*)(?:never\s*mind|nvm)(?:\s+(?:about\s+)?([^.!?;,\n]{1,60}))?",
    re.IGNORECASE,
)

_INSTEAD_OF_PREFIX_RE = re.compile(
    r"\binstead\s+of\s+([^,.;!?\n]{1,60}?)(?:\s*,\s*|\s+(?:use|do|keep|set|make|pick|"
    r"choose|go\s+with|try|run|write|return|output|format|switch\s+to)\b)\s*([^.!?\n]{0,80})",
    re.IGNORECASE,
)

_INSTEAD_OF_SUFFIX_RE = re.compile(
    r"\binstead\s+of\s+([^,.;!?\n]{1,60})",
    re.IGNORECASE,
)

_RATHER_THAN_RE = re.compile(
    r"\brather\s+than\s+([^,.;!?\n]{1,60})",
    re.IGNORECASE,
)

_NOT_CONTRAST_RE = re.compile(
    r"(?:,\s*|\s+-\s+|\s+—\s+)not\s+([^,.;!?\n]{1,50})",
    re.IGNORECASE,
)

_FROM_X_TO_Y_RE = re.compile(
    r"\b(?:switch|change|update|move|migrate|convert|bump|raise|lower|increase|decrease|"
    r"extend|shorten|push|pull|shift|alter)\b"
    r"(?:\s+(?:the\s+)?([a-z0-9_./-]{1,40}(?:\s+[a-z0-9_./-]{1,30}){0,2}))?"
    r"\s+from\s+([^,.;!?\n]{1,50}?)\s+to\s+([^,.;!?\n]{1,50})",
    re.IGNORECASE,
)

_CHANGE_X_TO_Y_RE = re.compile(
    r"\b(?:change|switch)\s+([^,.;!?\n]{1,50}?)\s+to\s+([^,.;!?\n]{1,50})",
    re.IGNORECASE,
)

_REPLACE_X_WITH_Y_RE = re.compile(
    r"\breplace\s+([^,.;!?\n]{1,50}?)\s+with\s+([^,.;!?\n]{1,50})",
    re.IGNORECASE,
)

_UPDATE_TOPIC_TO_Y_RE = re.compile(
    r"\bupdate\s+(?:the\s+)?([a-z0-9_./-]{2,40}(?:\s+[a-z0-9_./-]{2,30}){0,2})"
    r"\s+to\s+([^,.;!?\n]{1,50})",
    re.IGNORECASE,
)

_SWITCH_TO_Y_RE = re.compile(
    r"\bswitch\s+to\s+([^,.;!?\n]{1,50})",
    re.IGNORECASE,
)

_NEGATION_BEFORE_VERB_RE = re.compile(
    r"(?:^|[\s,.;:!?\n])(?:do\s+not|don't|never|must\s+not|mustn't|shall\s+not|"
    r"should\s+not|shouldn't|cannot|can't|avoid|not\s+to|please\s+not)\s+$",
    re.IGNORECASE,
)

# Value-domain detectors for short bare-value overrides (e.g., "Use June 30" -> "Actually July 31")
_DATE_VALUE_RE = re.compile(
    r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
    r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+"
    r"\d{1,2}(?:st|nd|rd|th)?\b|"
    r"\b\d{4}-\d{2}-\d{2}\b",
    re.IGNORECASE,
)
_ORDINAL_DAY_RE = re.compile(r"^\d{1,2}(?:st|nd|rd|th)$", re.IGNORECASE)

# Stopwords and generic directive/meta words excluded from topic overlap scoring
_STOPWORDS: frozenset[str] = frozenset(
    {
        "a",
        "about",
        "actually",
        "add",
        "after",
        "again",
        "all",
        "also",
        "always",
        "am",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "avoid",
        "be",
        "because",
        "been",
        "before",
        "being",
        "below",
        "between",
        "both",
        "but",
        "by",
        "can",
        "cannot",
        "cant",
        "change",
        "code",
        "correction",
        "could",
        "did",
        "do",
        "does",
        "don",
        "dont",
        "earlier",
        "edit",
        "else",
        "ensure",
        "file",
        "files",
        "for",
        "forget",
        "from",
        "get",
        "go",
        "had",
        "has",
        "have",
        "here",
        "how",
        "i",
        "if",
        "ignore",
        "in",
        "instead",
        "instruction",
        "instructions",
        "into",
        "is",
        "it",
        "its",
        "just",
        "keep",
        "last",
        "let",
        "lets",
        "like",
        "make",
        "may",
        "me",
        "message",
        "mind",
        "modify",
        "more",
        "must",
        "my",
        "need",
        "never",
        "nevermind",
        "new",
        "no",
        "not",
        "now",
        "nvm",
        "of",
        "ok",
        "okay",
        "old",
        "on",
        "one",
        "only",
        "or",
        "other",
        "our",
        "out",
        "override",
        "please",
        "previous",
        "previously",
        "prior",
        "put",
        "rather",
        "really",
        "regarding",
        "replace",
        "rule",
        "rules",
        "run",
        "said",
        "same",
        "scratch",
        "second",
        "see",
        "set",
        "shall",
        "should",
        "so",
        "some",
        "still",
        "sure",
        "switch",
        "than",
        "thanks",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "thing",
        "this",
        "those",
        "thought",
        "to",
        "too",
        "touch",
        "try",
        "under",
        "up",
        "update",
        "us",
        "use",
        "using",
        "very",
        "wait",
        "want",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "while",
        "who",
        "why",
        "will",
        "with",
        "without",
        "would",
        "yes",
        "yet",
        "you",
        "your",
    }
)


@dataclass(frozen=True, slots=True)
class _OverrideSignal:
    kind: str  # "hard" or "soft"
    old_values: tuple[str, ...]
    new_values: tuple[str, ...]
    topic_hints: tuple[str, ...]
    is_unscoped_retraction: bool


# ---------------------------------------------------------------------------
# Text normalization & fenced code stripping
# ---------------------------------------------------------------------------


def strip_fenced_code_blocks(text: str) -> str:
    """Remove Markdown fenced code blocks (`` ``` `` or ``~~~``) in O(N) time.

    Unclosed fences consume the remainder of the text so pasted code snippets
    never falsely trigger constraint or override detectors.
    """
    clean = sanitize_unicode(text)
    if "```" not in clean and "~~~" not in clean:
        return clean

    out_lines: list[str] = []
    in_fence = False
    fence_char = ""
    fence_len = 0

    for line in clean.splitlines():
        if not in_fence:
            match = _FENCE_OPEN_RE.match(line)
            if match:
                marker = match.group(1)
                fence_char = marker[0]
                fence_len = len(marker)
                in_fence = True
                continue
            out_lines.append(line)
        else:
            stripped = line.lstrip(" \t")
            if (
                len(line) - len(stripped) <= 3
                and stripped.startswith(fence_char * fence_len)
                and stripped.strip(fence_char).strip() == ""
            ):
                in_fence = False
                fence_char = ""
                fence_len = 0

    return "\n".join(out_lines)


def _normalize_prose(text: str) -> str:
    """Strip fenced code and normalize punctuation variants for matching."""
    prose = strip_fenced_code_blocks(text)
    prose = _SMART_APOSTROPHE_RE.sub("'", prose)
    prose = _DASH_VARIANTS_RE.sub("-", prose)
    return prose.strip()


def _split_sentences(prose: str) -> list[str]:
    parts: list[str] = []
    for raw in _SENTENCE_SPLIT_RE.split(prose):
        s = raw.strip()
        if s:
            parts.append(s)
    return parts


def _is_pure_info_question(sentence: str) -> bool:
    """Return True when a sentence is a pure information-seeking question."""
    s = sentence.strip()
    if not s.endswith("?"):
        return False
    # Strip conversational filler like "Wait, " or "Actually, " before checking interrogative
    trimmed = _LEADING_FILLER_RE.sub("", s).strip()
    trimmed = re.sub(r"^actually\s*,?\s*", "", trimmed, flags=re.IGNORECASE).strip()
    if _POLITE_REQUEST_START_RE.match(trimmed):
        remainder = _POLITE_REQUEST_START_RE.sub("", trimmed, count=1).strip()
        return bool(_NON_DIRECTIVE_REQUEST_VERB_RE.match(remainder))
    return bool(_INFO_QUESTION_START_RE.match(trimmed))


def _stem_token(tok: str) -> str:
    """Lightweight deterministic stemmer for English plurals/verb endings."""
    if len(tok) <= 3:
        return tok
    if tok.endswith("ies") and len(tok) > 4:
        return tok[:-3] + "y"
    if tok.endswith("ing") and len(tok) > 5:
        return tok[:-3]
    if tok.endswith("es") and len(tok) > 4 and tok[-3] in "sxz":
        return tok[:-2]
    if tok.endswith("s") and not tok.endswith("ss") and len(tok) > 3:
        return tok[:-1]
    return tok


def _extract_content_tokens(text: str) -> frozenset[str]:
    """Extract normalized domain/topic tokens (splitting camelCase and paths)."""
    prose = _normalize_prose(text)
    # Split camelCase before lowercasing so authHandler -> auth handler
    split_camel = _CAMEL_SPLIT_RE.sub(r"\1 \2", prose).lower()
    tokens: set[str] = set()
    for raw in _WORD_TOKEN_RE.findall(split_camel):
        if raw in _STOPWORDS:
            continue
        if len(raw) == 1 and not raw.isdigit():
            continue
        stemmed = _stem_token(raw)
        if stemmed in _STOPWORDS:
            continue
        tokens.add(stemmed)
        if stemmed != raw:
            tokens.add(raw)
    return frozenset(tokens)


def _clean_phrase(raw: str) -> str:
    """Clean an extracted old/new/topic phrase for matching."""
    cleaned = raw.strip().strip("\"'`()[]{}")
    cleaned = re.sub(r"^(?:the|a|an)\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.rstrip(".,;:!?")
    return " ".join(cleaned.split())


def _phrase_in_text(phrase: str, text: str) -> bool:
    """Check whether ``phrase`` appears in ``text`` on token boundaries."""
    clean_phrase = _clean_phrase(phrase).lower()
    if not clean_phrase:
        return False
    norm_text = " ".join(_normalize_prose(text).lower().split())
    if not norm_text:
        return False
    escaped = re.escape(clean_phrase)
    # Require non-alphanumeric boundaries so "10" doesn't match "100" and "go" != "good".
    pattern = rf"(?<![a-z0-9]){escaped}(?![a-z0-9])"
    return re.search(pattern, norm_text) is not None


# ---------------------------------------------------------------------------
# Public classifiers
# ---------------------------------------------------------------------------


def is_negative_constraint(text: str) -> bool:
    """Return True if ``text`` expresses a negative / do-not / never constraint."""
    prose = _normalize_prose(text)
    if not prose:
        return False

    for sentence in _split_sentences(prose):
        if _is_pure_info_question(sentence):
            continue
        # Remove polite question prefix ("Can you make sure...") for inner check
        check = sentence
        if check.endswith("?") and _POLITE_REQUEST_START_RE.match(check):
            check = _POLITE_REQUEST_START_RE.sub("", check, count=1).strip()

        for pat in _NEG_STRONG_PATTERNS:
            match = pat.search(check)
            if not match:
                continue
            matched = match.group(0).lower()
            if ("don't" in matched or "do not" in matched) and _NEG_DONT_FALSE_POSITIVE_RE.search(
                check
            ):
                # Verify whether there's another non-excluded do-not / don't in this sentence
                scrubbed = _NEG_DONT_FALSE_POSITIVE_RE.sub(" ", check)
                if not re.search(r"\b(?:do\s+not|don't)\b", scrubbed, re.IGNORECASE):
                    continue
            if matched == "never" and _NEG_NEVER_FALSE_POSITIVE_RE.search(check):
                scrubbed = _NEG_NEVER_FALSE_POSITIVE_RE.sub(" ", check)
                if not re.search(r"\bnever\b", scrubbed, re.IGNORECASE):
                    continue
            return True
    return False


def _is_negated_before(text: str, pos: int) -> bool:
    """Return True if a negation cue immediately precedes ``pos`` in ``text``."""
    window = text[max(0, pos - 32) : pos]
    return _NEGATION_BEFORE_VERB_RE.search(window) is not None


def _detect_override_signal(text: str) -> _OverrideSignal | None:
    """Inspect ``text`` for superseding correction / override cues."""
    prose = _normalize_prose(text)
    if not prose:
        return None

    sentences = _split_sentences(prose)
    # Filter out pure information-seeking questions
    non_question_sentences = [s for s in sentences if not _is_pure_info_question(s)]
    if not non_question_sentences:
        return None

    active_prose = " ".join(non_question_sentences)

    hard = False
    soft = False
    unscoped_retraction = False
    old_values: list[str] = []
    new_values: list[str] = []
    topic_hints: list[str] = []

    def _add_unique(target: list[str], value: str) -> None:
        cleaned = _clean_phrase(value)
        if cleaned and cleaned.lower() not in {v.lower() for v in target}:
            target.append(cleaned)

    # 1. Explicit headers ("Correction:", "Override:", "Update:")
    if _EXPLICIT_OVERRIDE_HEADER_RE.search(active_prose) or _UPDATE_HEADER_RE.search(active_prose):
        hard = True

    # 2. Explicit ignore/forget previous instruction (unless negated: "don't ignore ...")
    for m in _IGNORE_FORGET_ABOUT_RE.finditer(active_prose):
        if _is_negated_before(active_prose, m.start()):
            continue
        hard = True
        topic = m.group(1)
        if topic and topic.strip():
            _add_unique(topic_hints, topic)
        else:
            unscoped_retraction = True

    for m in _FORGET_ABOUT_DIRECT_RE.finditer(active_prose):
        if _is_negated_before(active_prose, m.start()):
            continue
        hard = True
        _add_unique(topic_hints, m.group(1))

    # 3. Scratch that / strike that / cancel that
    if _SCRATCH_THAT_RE.search(active_prose):
        hard = True
        unscoped_retraction = True

    # 4. Never mind ...
    for m in _NEVER_MIND_RE.finditer(active_prose):
        hard = True
        topic = m.group(1)
        if topic and _extract_content_tokens(topic):
            _add_unique(topic_hints, topic)
            _add_unique(old_values, topic)
        else:
            unscoped_retraction = True

    # 5. "no, actually ..." / "wait, actually ..." / "on second thought ..."
    if _HARD_NO_ACTUALLY_RE.search(active_prose):
        hard = True

    # 6. "instead of <OLD>, use <NEW>" and "<NEW> instead of <OLD>"
    for m in _INSTEAD_OF_PREFIX_RE.finditer(active_prose):
        hard = True
        _add_unique(old_values, m.group(1))
        if m.group(2):
            _add_unique(new_values, m.group(2))

    for m in _INSTEAD_OF_SUFFIX_RE.finditer(active_prose):
        hard = True
        _add_unique(old_values, m.group(1))

    # 7. "rather than <OLD>"
    for m in _RATHER_THAN_RE.finditer(active_prose):
        # Require directive context so casual prose doesn't trigger
        if _has_directive_sentence(non_question_sentences):
            hard = True
            _add_unique(old_values, m.group(1))

    # 8. ", not <OLD>" contrast inside a directive sentence
    for m in _NOT_CONTRAST_RE.finditer(active_prose):
        if _has_directive_sentence(non_question_sentences):
            candidate_old = _clean_phrase(m.group(1))
            # Avoid matching ", not sure" / ", not really" / ", not yet"
            if candidate_old and candidate_old.lower() not in {
                "sure",
                "really",
                "yet",
                "now",
                "anymore",
                "at all",
                "bad",
                "good",
                "working",
                "needed",
                "necessary",
            }:
                hard = True
                _add_unique(old_values, candidate_old)

    # 9. "switch/change/update/move <topic?> from <OLD> to <NEW>" (unless negated)
    for m in _FROM_X_TO_Y_RE.finditer(active_prose):
        if _is_negated_before(active_prose, m.start()):
            continue
        hard = True
        if m.group(1):
            _add_unique(topic_hints, m.group(1))
        _add_unique(old_values, m.group(2))
        _add_unique(new_values, m.group(3))

    # 10. "replace <OLD> with <NEW>" (unless negated)
    for m in _REPLACE_X_WITH_Y_RE.finditer(active_prose):
        if _is_negated_before(active_prose, m.start()):
            continue
        hard = True
        _add_unique(old_values, m.group(1))
        _add_unique(new_values, m.group(2))

    # 11. "change/switch <OLD> to <NEW>" (when not already matched by from..to and not negated)
    if not old_values:
        for m in _CHANGE_X_TO_Y_RE.finditer(active_prose):
            if _is_negated_before(active_prose, m.start()):
                continue
            lhs = _clean_phrase(m.group(1))
            rhs = _clean_phrase(m.group(2))
            if lhs and rhs and " from " not in lhs.lower():
                hard = True
                _add_unique(old_values, lhs)
                _add_unique(topic_hints, lhs)
                _add_unique(new_values, rhs)

    # 12. "update the <TOPIC> to <NEW>" (soft unless paired with hard cue; skip if negated)
    for m in _UPDATE_TOPIC_TO_Y_RE.finditer(active_prose):
        if _is_negated_before(active_prose, m.start()):
            continue
        topic = _clean_phrase(m.group(1))
        rhs = _clean_phrase(m.group(2))
        if topic and rhs and _extract_content_tokens(topic):
            soft = True
            _add_unique(topic_hints, topic)
            _add_unique(new_values, rhs)

    # 13. "switch to <NEW>" (soft override cue; skip if negated)
    if not old_values:
        for m in _SWITCH_TO_Y_RE.finditer(active_prose):
            if _is_negated_before(active_prose, m.start()):
                continue
            rhs = _clean_phrase(m.group(1))
            if rhs:
                soft = True
                _add_unique(new_values, rhs)

    # 14. Soft "actually" ONLY when accompanied by a genuine directive/constraint!
    # Strictly guards against "that actually worked!", "thanks, that actually looks great",
    # "is it actually broken?", "I actually like this".
    if not hard and _SOFT_ACTUALLY_RE.search(active_prose):
        # Strip leading "actually," to check if the remainder is a real directive
        after_actually = re.sub(
            r"^(?:(?:no|wait|sorry)\s*,\s*)?actually\s*,?\s*",
            "",
            active_prose,
            count=1,
            flags=re.IGNORECASE,
        ).strip()
        if _has_directive_sentence(_split_sentences(after_actually)) or is_negative_constraint(
            after_actually
        ):
            soft = True

    if not hard and not soft:
        return None

    # If we had specific old_values or topic_hints, it's scoped (not an unscoped retraction)
    if old_values or topic_hints:
        unscoped_retraction = False

    return _OverrideSignal(
        kind="hard" if hard else "soft",
        old_values=tuple(old_values),
        new_values=tuple(new_values),
        topic_hints=tuple(topic_hints),
        is_unscoped_retraction=unscoped_retraction,
    )


def _has_directive_sentence(sentences: Sequence[str]) -> bool:
    """Return True if any non-question sentence contains a constraint or imperative directive."""
    for sentence in sentences:
        if _is_pure_info_question(sentence):
            continue
        cleaned = _LEADING_FILLER_RE.sub("", sentence).strip()
        if not cleaned:
            continue
        if cleaned.endswith("?") and _POLITE_REQUEST_START_RE.match(cleaned):
            cleaned = _POLITE_REQUEST_START_RE.sub("", cleaned, count=1).strip()
            if _NON_DIRECTIVE_REQUEST_VERB_RE.match(cleaned):
                continue

        if _CONSTRAINT_KEYWORDS_RE.search(cleaned):
            return True
        if _RESTRICTIVE_DIRECTIVE_RE.search(cleaned):
            return True
        if _IMPERATIVE_START_RE.match(cleaned):
            # Guard against 1-word chatter imperatives like "continue", "go", "run it", "try again"
            norm_clause = _CHATTER_TRIM_RE.sub("", cleaned.lower())
            if norm_clause not in _CHATTER_PHRASES:
                return True
        # Check sub-clauses after ":" or "," (e.g., "One rule: use UTC", "From now on, keep X")
        for sep in (":", ","):
            if sep in cleaned:
                tail = cleaned.split(sep, 1)[1].strip()
                if tail and _IMPERATIVE_START_RE.match(tail):
                    norm_tail = _CHATTER_TRIM_RE.sub("", tail.lower())
                    if norm_tail not in _CHATTER_PHRASES:
                        return True
    return False


def is_override_turn(text: str) -> bool:
    """Return True if ``text`` expresses a superseding correction or override."""
    signal = _detect_override_signal(text)
    return signal is not None and signal.kind == "hard"


def is_constraint_or_directive(text: str) -> bool:
    """Return True if ``text`` contains a user constraint, directive, or override."""
    prose = _normalize_prose(text)
    if not prose:
        return False

    if is_negative_constraint(text):
        return True

    signal = _detect_override_signal(text)
    if signal is not None:
        return True

    sentences = _split_sentences(prose)
    return _has_directive_sentence(sentences)


def is_chatter(text: str) -> bool:
    """Return True if ``text`` is low-signal chatter, acknowledgement, or empty."""
    clean = sanitize_unicode(text)
    if not clean.strip():
        return True

    prose = _normalize_prose(clean)
    if not prose:
        # If there was a fenced code block (and no prose), it's code rather than chatter.
        return "```" not in clean and "~~~" not in clean

    # Never classify a genuine directive, negative constraint, or override as chatter.
    if is_negative_constraint(clean) or _detect_override_signal(clean) is not None:
        return False

    # Pure punctuation / emoji / symbols
    stripped = _CHATTER_TRIM_RE.sub("", prose.lower())
    if not stripped:
        return True

    if len(prose) > 140:
        return False

    # Every non-empty clause must be a known low-signal acknowledgement phrase
    clauses = [c.strip() for c in _CLAUSE_SPLIT_RE.split(prose.lower()) if c.strip()]
    if not clauses:
        return True

    for clause in clauses:
        norm = " ".join(_CHATTER_TRIM_RE.sub("", clause).split())
        if not norm:
            continue
        if norm not in _CHATTER_PHRASES:
            return False
    return True


# ---------------------------------------------------------------------------
# Target pin matching for superseding chains
# ---------------------------------------------------------------------------


def _score_supersede_candidate(
    signal: _OverrideSignal,
    override_text: str,
    override_tokens: frozenset[str],
    candidate: PinEntry,
    candidate_own_tokens: frozenset[str],
    candidate_chain_tokens: frozenset[str],
) -> int:
    """Score how strongly ``signal`` targets ``candidate``. Return 0 for no match."""
    score = 0

    # 1. Explicit old-value matches (highest signal)
    for old_val in signal.old_values:
        if _phrase_in_text(old_val, candidate.text):
            score += 120
            continue
        old_tokens = _extract_content_tokens(old_val)
        if old_tokens:
            own_hit = old_tokens & candidate_own_tokens
            if own_hit == old_tokens:
                score += 100
            elif own_hit:
                score += 45 * len(own_hit)
            else:
                chain_hit = old_tokens & candidate_chain_tokens
                if chain_hit == old_tokens:
                    score += 70

    # 2. Explicit topic-hint matches ("ignore what I said earlier about auth")
    for hint in signal.topic_hints:
        if _phrase_in_text(hint, candidate.text):
            score += 90
            continue
        hint_tokens = _extract_content_tokens(hint)
        if hint_tokens:
            own_hit = hint_tokens & candidate_own_tokens
            if own_hit:
                score += 50 * len(own_hit)
            else:
                chain_hit = hint_tokens & candidate_chain_tokens
                if chain_hit:
                    score += 35 * len(chain_hit)
                else:
                    # Prefix match for domain abbreviations (e.g., "auth" <-> "authentication")
                    for ht in hint_tokens:
                        if len(ht) >= 4 and any(
                            ct.startswith(ht) or ht.startswith(ct)
                            for ct in candidate_chain_tokens
                            if len(ct) >= 4
                        ):
                            score += 30
                            break

    # 3. General content-token overlap (excluding stopwords & generic directive verbs)
    own_overlap = override_tokens & candidate_own_tokens
    chain_overlap = override_tokens & candidate_chain_tokens

    # If the override explicitly named old_values or topic_hints that had ZERO match
    # against this candidate, be stricter about incidental single-word overlap.
    has_explicit_target = bool(signal.old_values or signal.topic_hints)

    if len(own_overlap) >= 2:
        score += 25 * len(own_overlap)
    elif len(own_overlap) == 1 and not has_explicit_target:
        tok = next(iter(own_overlap))
        if len(tok) >= 4:
            score += 25
    elif len(chain_overlap) >= 2:
        score += 18 * len(chain_overlap)
    elif len(chain_overlap) == 1 and not has_explicit_target:
        tok = next(iter(chain_overlap))
        if len(tok) >= 4 and signal.kind == "hard":
            score += 18

    # 4. Short date-only value override (e.g., "Use June 30." -> "Actually, use July 31.")
    if (
        score == 0
        and not has_explicit_target
        and _DATE_VALUE_RE.search(override_text)
        and _DATE_VALUE_RE.search(candidate.text)
    ):
        # Only match bare date rules when they don't introduce conflicting topic nouns
        # (filter out month names and day numbers).
        override_non_date = {
            t
            for t in override_tokens
            if not t.isdigit()
            and not _ORDINAL_DAY_RE.fullmatch(t)
            and not _DATE_VALUE_RE.fullmatch(t + " 1")
        }
        cand_non_date = {
            t
            for t in candidate_chain_tokens
            if not t.isdigit()
            and not _ORDINAL_DAY_RE.fullmatch(t)
            and not _DATE_VALUE_RE.fullmatch(t + " 1")
        }
        if not override_non_date or not cand_non_date or (override_non_date & cand_non_date):
            score += 35

    return score


# ---------------------------------------------------------------------------
# Main extraction pipeline
# ---------------------------------------------------------------------------


def extract_pins(
    user_turns: Sequence[UserTurn],
    manual_pins: Sequence[PinEntry] = (),
) -> list[PinEntry]:
    """Extract active & superseded pins from ordered user turns plus manual pins.

    - Preserves all manual pins (copied without mutating inputs, deduplicated).
    - Filters chatter/acknowledgements and non-directive turns.
    - Detects negative constraints and superseding corrections, linking multi-step
      override chains (``A -> B -> C``) via ``supersedes`` / ``superseded_by``.
    - Deduplicates repeated active instructions cleanly.
    """
    result: list[PinEntry] = []
    # Maps pin_id -> (own_tokens, accumulated_chain_tokens)
    pin_tokens: dict[str, tuple[frozenset[str], frozenset[str]]] = {}

    # 1. Copy & deduplicate manual pins (never mutate caller's PinEntry objects)
    seen_manual_norms: set[str] = set()
    seen_pin_ids: set[str] = set()
    manual_by_norm: dict[str, PinEntry] = {}

    for mp in manual_pins:
        clean_text = sanitize_unicode(mp.text).strip()
        norm = normalize_pin_text(clean_text)
        if not norm:
            continue
        if norm in manual_by_norm:
            existing = manual_by_norm[norm]
            for tag in mp.tags:
                clean_tag = sanitize_unicode(tag).strip()
                if clean_tag and clean_tag not in existing.tags:
                    existing.tags.append(clean_tag)
            if mp.is_negative and not existing.is_negative:
                existing.is_negative = True
            continue
        if mp.pin_id in seen_pin_ids:
            continue

        copied = PinEntry(
            pin_id=sanitize_unicode(mp.pin_id),
            text=clean_text,
            source=mp.source,
            turn_index=mp.turn_index,
            timestamp=mp.timestamp,
            supersedes=mp.supersedes,
            superseded_by=mp.superseded_by,
            is_negative=mp.is_negative,
            tags=[sanitize_unicode(t).strip() for t in mp.tags if sanitize_unicode(t).strip()],
        )
        seen_manual_norms.add(norm)
        seen_pin_ids.add(copied.pin_id)
        manual_by_norm[norm] = copied
        result.append(copied)
        toks = _extract_content_tokens(clean_text)
        pin_tokens[copied.pin_id] = (toks, toks)

    # 2. Sort user turns deterministically by (turn_index, seq, original_order)
    ordered_turns = [
        turn
        for _, turn in sorted(
            enumerate(user_turns),
            key=lambda pair: (pair[1].turn_index, pair[1].seq, pair[0]),
        )
    ]

    # Track normalized text of currently ACTIVE pins for deduplication
    active_norms: set[str] = set(seen_manual_norms)

    for turn in ordered_turns:
        clean_text = sanitize_unicode(turn.text).strip()
        norm = normalize_pin_text(clean_text)
        if not norm:
            continue
        if is_chatter(clean_text):
            continue

        signal = _detect_override_signal(clean_text)
        neg = is_negative_constraint(clean_text)
        directive = neg or signal is not None or is_constraint_or_directive(clean_text)
        if not directive:
            continue

        # Deduplicate repeated identical/whitespace-equivalent active instructions
        if norm in active_norms:
            continue

        own_tokens = _extract_content_tokens(clean_text)
        superseded_target: PinEntry | None = None

        if signal is not None:
            # Find eligible earlier active pins (newest first)
            candidates: list[PinEntry] = []
            for prev in reversed(result):
                if prev.superseded_by is not None:
                    continue
                # Only supersede a manual pin if it has an explicit earlier turn_index
                if prev.source == "manual" and (
                    prev.turn_index is None or prev.turn_index >= turn.turn_index
                ):
                    continue
                candidates.append(prev)

            best_score = 0
            best_candidate: PinEntry | None = None
            for cand in candidates:
                cand_own, cand_chain = pin_tokens.get(cand.pin_id, (frozenset(), frozenset()))
                score = _score_supersede_candidate(
                    signal=signal,
                    override_text=clean_text,
                    override_tokens=own_tokens,
                    candidate=cand,
                    candidate_own_tokens=cand_own,
                    candidate_chain_tokens=cand_chain,
                )
                # Strict > preserves recency preference on ties (candidates are newest first)
                if score > best_score:
                    best_score = score
                    best_candidate = cand

            if best_candidate is not None and best_score >= 25:
                superseded_target = best_candidate
            elif signal.is_unscoped_retraction and candidates:
                # Unscoped retraction ("scratch that", "ignore what I said earlier") targets
                # the most recent active transcript pin (never an un-indexed manual pin).
                for cand in candidates:
                    if cand.source != "manual":
                        superseded_target = cand
                        break

        # Determine final pin source:
        # - "override" if we superseded a target OR it's a hard override signal
        # - "auto" if it was only a soft cue (e.g. "actually, also add X") with no target match
        source = (
            "override"
            if (superseded_target is not None or (signal is not None and signal.kind == "hard"))
            else "auto"
        )

        pin_id = make_pin_id(source, norm, turn.turn_index)
        if pin_id in seen_pin_ids:
            # Disambiguate deterministic ID if same turn_index has distinct seq
            pin_id = make_pin_id(source, f"{norm}#seq={turn.seq}", turn.turn_index)
        if pin_id in seen_pin_ids:
            continue

        new_pin = PinEntry(
            pin_id=pin_id,
            text=clean_text,
            source=source,
            turn_index=turn.turn_index,
            timestamp=turn.timestamp,
            supersedes=superseded_target.pin_id if superseded_target is not None else None,
            superseded_by=None,
            is_negative=neg,
            tags=[],
        )

        chain_tokens = own_tokens
        if superseded_target is not None:
            superseded_target.superseded_by = new_pin.pin_id
            old_norm = normalize_pin_text(superseded_target.text)
            active_norms.discard(old_norm)
            _, prev_chain = pin_tokens.get(superseded_target.pin_id, (frozenset(), frozenset()))
            chain_tokens = own_tokens | prev_chain

        seen_pin_ids.add(new_pin.pin_id)
        active_norms.add(norm)
        pin_tokens[new_pin.pin_id] = (own_tokens, chain_tokens)
        result.append(new_pin)

    return result
