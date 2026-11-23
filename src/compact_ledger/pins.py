"""Deterministic user constraint extraction and superseding correction engine.

Zero third-party dependencies, zero LLM calls, strictly linear-time scanning
(ReDoS-safe regexes and inverted-index candidate lookup). Extracts verbatim
user directives, negative constraints, and superseding corrections from ordered
transcript turns while filtering conversational chatter and preserving explicit
manual pins.
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
# Unicode / fenced-code helpers (strictly linear time, zero backtracking)
# ---------------------------------------------------------------------------

_FENCE_OPEN_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})(.*)$")
_SMART_APOSTROPHE_RE = re.compile(r"[\u2018\u2019\u2032]")
_DASH_VARIANTS_RE = re.compile(r"[\u2013\u2014]")
_WORD_TOKEN_RE = re.compile(r"[a-z0-9]+")
_CAMEL_SPLIT_RE = re.compile(r"([a-z0-9])([A-Z])")
_SENTENCE_BREAK_RE = re.compile(r"(?<=[.!?]) ")
_CLAUSE_SPLIT_RE = re.compile(r"[.!?;,]+")

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

_LEADING_FILLER_RE = re.compile(
    r"^(?:(?:ok(?:ay)?|yes|yep|yeah|sure|thanks?|thank you|great|nice|cool|awesome|"
    r"alright|all right|got it|lgtm|sounds good|looks good|wait|hey|hi|hello|"
    r"and|also|plus|btw|by the way|one more thing|one more rule)\b[,:; -]*){1,3}",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Question / non-directive observation guards
# ---------------------------------------------------------------------------

_INFO_QUESTION_START_RE = re.compile(
    r"^(?:what|where|when|why|who|whom|whose|which|how|"
    r"is|are|was|were|am|do|does|did|have|has|had|should|"
    r"would (?:it|that|this|we|there)|"
    r"could (?:it|that|this|we|there)|"
    r"can (?:it|that|this|we|there)|"
    r"will (?:it|that|this|there)|"
    r"might|may i)\b",
    re.IGNORECASE,
)

_POLITE_REQUEST_START_RE = re.compile(
    r"^(?:can|could|would|will) you (?:please )?",
    re.IGNORECASE,
)

_NON_DIRECTIVE_REQUEST_VERB_RE = re.compile(
    r"^(?:explain|tell me|show me|describe|list|summarize|clarify|confirm|"
    r"check (?:if|whether|why)|see (?:if|whether|why)|find out|look up)\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Negative constraint detection (all run on single-space normalized sentences)
# ---------------------------------------------------------------------------

_NEG_STRONG_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bdo not\b", re.IGNORECASE),
    re.compile(r"\bdon't\b", re.IGNORECASE),
    re.compile(r"\bmust not\b|\bmustn't\b|\bshall not\b", re.IGNORECASE),
    re.compile(r"\bshould not\b|\bshouldn't\b", re.IGNORECASE),
    re.compile(r"\bnever\b", re.IGNORECASE),
    re.compile(
        r"\bavoid (?:using|modifying|touching|changing|adding|breaking|introducing|"
        r"calling|mutating|rewriting|deleting|removing|importing|editing|installing|"
        r"external|third[- ]party|global|hardcod\w*|[a-z][a-z0-9_-]*ing\b)",
        re.IGNORECASE,
    ),
    re.compile(r"^(?:please )?avoid\b", re.IGNORECASE),
    re.compile(
        r"\b(?:forbid(?:den)?|prohibit(?:ed)?|disallow(?:ed)?|not allowed to|"
        r"under no circumstances|hands off)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:cannot|can't) (?:break|modify|touch|change|use|depend|delete|remove|"
        r"commit|push|alter|mutate|drop|exceed|import|call|expose|leak)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bno (?:third[- ]party|external|llm|network|breaking|extra|additional|"
        r"changes? to|edits? to|modifications? to|writes? to|dependencies|"
        r"deps|mocking|mocks|skipping|skipped)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bzero (?:(?:third[- ]party|external|runtime) ){1,2}dependenc(?:y|ies)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bwithout (?:modifying|touching|changing|breaking|adding|using|installing|"
        r"mutating|altering|deleting|removing|calling)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bstop (?:using|modifying|touching|changing|editing|adding|rewriting|calling)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bleave \S+(?: \S+){0,4} (?:alone|untouched|as[- ]is|unchanged)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:please|could you|can you|would you|let's|lets) not\b", re.IGNORECASE),
)

_NEG_DONT_FALSE_POSITIVE_RE = re.compile(
    r"\b(?:why don't|"
    r"(?:i|we) (?:just )?don't (?:know|think|see|understand|remember|believe|"
    r"mind|care|have a|get why|get how)|"
    r"(?:if|when|whenever|since|because|although|though) (?:you|we|they|it) don't|"
    r"if (?:it|this|that) does not|"
    r"(?:the [a-z0-9_.-]+(?: [a-z0-9_.-]+)?|they|it|this|that|tests?|builds?|hooks?|"
    r"checks?|loops?|scripts?|files?) (?:still )?(?:don't|do not) "
    r"(?:pass|work|run|compile|build|finish|terminate|match|load|start|connect|"
    r"exist|return|appear|show|support|handle|include|have))\b",
    re.IGNORECASE,
)

_NEG_NEVER_FALSE_POSITIVE_RE = re.compile(
    r"\b(?:never ?mind|"
    r"(?:i|we|it|that|this|he|she|they)(?:'ve|'s|'d| (?:have|has|had))? never "
    r"(?:seen|worked|happened|got|gotten|ran|run|runs|failed|crashed|been|had|heard|tried|"
    r"noticed|encountered|said|asked|mentioned|requested|meant|wanted|changed|touched|"
    r"edited|broke|terminat\w*|finish\w*|pass\w*|complet\w*|return\w*|exits?|stops?|"
    r"resolves?|triggers?|used (?:it|this|that) before)|"
    r"the [a-z0-9_.-]+(?: [a-z0-9_.-]+)? (?:still )?never "
    r"(?:terminat\w*|finish\w*|pass\w*|work\w*|ran|run|runs|complet\w*|return\w*|"
    r"exits?|stops?|resolves?|triggers?|loads?|starts?|matches?|fires?))\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Positive directive / constraint detection (single-space normalized sentences)
# ---------------------------------------------------------------------------

_CONSTRAINT_KEYWORDS_RE = re.compile(
    r"\b(?:must|have to|need(?:s)? to|required to|always|strictly|exclusively|"
    r"ensure|make sure|guarantee|enforce|preserve|maintain|stick (?:to|with)|"
    r"adhere to|comply with|remember (?:to|that)|"
    r"backward[s]?[- ]compatible|compatible with|stdlib only|zero[- ]dependenc(?:y|ies)|"
    r"fail[- ]open|deterministic)\b|"
    r"^(?:requirement|constraint|rule|important|note|policy|invariant):",
    re.IGNORECASE,
)

_RESTRICTIVE_DIRECTIVE_RE = re.compile(
    r"\b(?:only (?:use|modify|edit|touch|change|return|accept|allow|support|import|"
    r"run|update|write|target|keep|emit|read|output|print)|"
    r"use only|"
    r"limit (?:it |this |the \w+ )?to|"
    r"stay (?:under|below|within)|"
    r"keep (?:it|this|that|the|all|our|every|runtime|output|changes?|diffs?|deps?|"
    r"dependencies|compatibility|under|below|within|at|to|in|backward|public|api|tests?))\b",
    re.IGNORECASE,
)

_IMPERATIVE_VERBS = (
    "use|keep|set|make|format|output|return|write|store|save|add|remove|delete|rename|move|"
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
    rf"^(?:(?:please|kindly|let's|lets|we should|you should|we must|you must|"
    rf"be sure to|go ahead and) )?(?:{_IMPERATIVE_VERBS})\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Superseding correction / override detection (all strictly linear-time)
# All patterns run on single-space normalized sentences with trailing [.!?] stripped.
# ---------------------------------------------------------------------------

_EXPLICIT_OVERRIDE_HEADER_RE = re.compile(
    r"^(?:correction|override|change of plan|new rule|revised rule|updated rule|"
    r"retract(?:ion)?)(?::| - )",
    re.IGNORECASE,
)

_UPDATE_HEADER_RE = re.compile(r"^update:\s*(.+)$", re.IGNORECASE)

_SOFT_ACTUALLY_RE = re.compile(
    r"^(?:(?:no|wait|sorry), )?actually\b",
    re.IGNORECASE,
)

_ADDITIVE_AFTER_ACTUALLY_RE = re.compile(
    r"^(?:(?:no|wait|sorry), )?actually,? (?:(?:let's|lets|can you|could you|please|we should) )?"
    r"(?:also|in addition|as well|additionally)\b",
    re.IGNORECASE,
)

_HARD_NO_ACTUALLY_RE = re.compile(
    r"^(?:no, actually|actually,? no\b|wait, actually|on second thought)\b",
    re.IGNORECASE,
)

_IGNORE_FORGET_ABOUT_RE = re.compile(
    r"\b(?:ignore|forget|disregard|cancel|undo|revert|retract) "
    r"(?:what i said (?:earlier|before|previously)|"
    r"(?:my|the) (?:previous|earlier|prior|last|old|original) "
    r"(?:instruction|instructions|constraint|constraints|rule|rules|requirement|"
    r"requirements|message|directive|directives|note|pin)|"
    r"my last (?:message|instruction|rule|constraint)|"
    r"that (?:earlier|previous|last) (?:instruction|rule|constraint|requirement))"
    r"(?: (?:about|regarding|on|for|with) ([^,;!?]{1,80}))?",
    re.IGNORECASE,
)

_FORGET_ABOUT_DIRECT_RE = re.compile(
    r"\b(?:ignore|forget|disregard) (?:about )?the ([^,;!?]{1,60}) "
    r"(?:rule|constraint|requirement|instruction|cutoff|limit|setting)\b",
    re.IGNORECASE,
)

# Must end at clause boundary so "ignore that lint warning" NEVER matches!
_SCRATCH_THAT_RE = re.compile(
    r"^(?:scratch that|strike that|belay that|cancel that|"
    r"disregard that|forget that|ignore that)(?:$|[,;:-])",
    re.IGNORECASE,
)

_NEVER_MIND_RE = re.compile(
    r"^(?:never ?mind|nvm)(?:$|[,;:-] ?(.*)$| (?:about )?([^,;!?]{1,60}))",
    re.IGNORECASE,
)

_INSTEAD_OF_PREFIX_RE = re.compile(
    r"\binstead of ([^,;!?]{1,60}?)(?:, | (?:use|do|keep|set|make|pick|"
    r"choose|go with|try|run|write|return|output|format|switch to)\b) ?([^!?]{0,80})",
    re.IGNORECASE,
)

_INSTEAD_OF_SUFFIX_RE = re.compile(
    r"\binstead of ([^,;!?]{1,60})",
    re.IGNORECASE,
)

_RATHER_THAN_RE = re.compile(
    r"\brather than ([^,;!?]{1,60})",
    re.IGNORECASE,
)

# Strictly linear on single-space normalized text (no unanchored \s+)
_NOT_CONTRAST_RE = re.compile(
    r"(?:, | - )not ([^,;!?]{1,50})",
    re.IGNORECASE,
)

# Directive prefix allowed before a replacement verb (excludes nouns like "a small change"
# and third-person observations like "The tests change from passing to failing").
_DIRECTIVE_VERB_PREFIX = (
    r"(?:^|[,;:] )(?:(?:please|kindly|let's|lets|we should|you should|we must|you must|"
    r"can you|could you|would you|will you|actually|now|and|then|so|instead|correction:|"
    r"override:|update:|go ahead and|be sure to) )*"
)

_FROM_X_TO_Y_RE = re.compile(
    _DIRECTIVE_VERB_PREFIX
    + r"(?:switch|change|update|move|migrate|convert|bump|raise|lower|increase|decrease|"
    r"extend|shorten|alter)"
    r"(?: (?:the )?([a-z0-9_./-]{1,40}(?: [a-z0-9_./-]{1,30}){0,2}))?"
    r" from ([^,;!?]{1,50}?) to ([^,;!?]{1,50})",
    re.IGNORECASE,
)

_CHANGE_X_TO_Y_RE = re.compile(
    _DIRECTIVE_VERB_PREFIX + r"(?:change|switch) ([^,;!?]{1,50}?) to ([^,;!?]{1,50})",
    re.IGNORECASE,
)

_REPLACE_X_WITH_Y_RE = re.compile(
    _DIRECTIVE_VERB_PREFIX + r"replace ([^,;!?]{1,50}?) with ([^,;!?]{1,50})",
    re.IGNORECASE,
)

_UPDATE_TOPIC_TO_Y_RE = re.compile(
    _DIRECTIVE_VERB_PREFIX
    + r"update (?:the )?([a-z0-9_./-]{2,40}(?: [a-z0-9_./-]{2,30}){0,2}) to ([^,;!?]{1,50})",
    re.IGNORECASE,
)

_SWITCH_TO_Y_RE = re.compile(
    _DIRECTIVE_VERB_PREFIX + r"switch to ([^,;!?]{1,50})",
    re.IGNORECASE,
)

# Clause-level negation and conditional/hypothetical guards (prevents B2 false positives)
_CLAUSE_NEGATION_RE = re.compile(
    r"\b(?:do not|don't|never|must not|mustn't|shall not|should not|shouldn't|"
    r"cannot|can't|avoid|let's not|lets not|please not|not to|rather not|"
    r"no need to|don't want to|do not want to|don't need to|do not need to|"
    r"without|stop|refuse to|forbid\w*|prohibit\w*)\b",
    re.IGNORECASE,
)

_CLAUSE_CONDITIONAL_RE = re.compile(
    r"^(?:if|when|whenever|unless|suppose|assuming|what if|in case)\b",
    re.IGNORECASE,
)

_INFINITIVE_PURPOSE_START_RE = re.compile(
    r"^(?:get|see|check|find|verify|ensure|avoid|enable|disable|make|be|have|keep)\b",
    re.IGNORECASE,
)

# Value-domain detectors for short bare-value overrides (e.g., "Use June 30" -> "Actually July 31")
_DATE_VALUE_RE = re.compile(
    r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
    r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?) "
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
    topic_hints: tuple[str, ...]
    is_unscoped_retraction: bool


# ---------------------------------------------------------------------------
# Text normalization & fenced code stripping
# ---------------------------------------------------------------------------


def _trim_non_alnum(text: str) -> str:
    """Strip leading and trailing non-alphanumeric characters in O(N) time."""
    n = len(text)
    start = 0
    while start < n and not text[start].isalnum():
        start += 1
    end = n
    while end > start and not text[end - 1].isalnum():
        end -= 1
    return text[start:end]


def strip_fenced_code_blocks(text: str) -> str:
    """Remove Markdown fenced code blocks (`` ``` `` or ``~~~``) in O(N) time.

    Per CommonMark, a backtick opening fence cannot contain backticks in its info
    string, so inline triple-backtick spans (e.g., `` ```cmd``` do not run ``)
    are preserved as prose rather than treated as an unclosed code fence.
    Unclosed multi-line fences consume the remainder of the text so pasted code
    snippets never falsely trigger constraint or override detectors.
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
                rest = match.group(2)
                # CommonMark rule: backtick fence info strings cannot contain backticks;
                # a same-line closing backtick run is an inline code span, not a block fence.
                if marker[0] == "`" and "`" in rest:
                    out_lines.append(line)
                    continue
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
    """Split prose into single-space-normalized sentences in strictly O(N) time."""
    parts: list[str] = []
    for raw_line in prose.splitlines():
        compact_line = " ".join(raw_line.split())
        if not compact_line:
            continue
        for raw_sent in _SENTENCE_BREAK_RE.split(compact_line):
            s = raw_sent.strip()
            if s:
                parts.append(s)
    return parts


def _strip_terminal_punct(sentence: str) -> str:
    """Strip sentence-ending punctuation while keeping internal dots (e.g., 3.11, auth.py)."""
    end = len(sentence)
    while end > 0 and sentence[end - 1] in ".!?":
        end -= 1
    return sentence[:end].strip()


def _is_pure_info_question(sentence: str) -> bool:
    """Return True when a sentence is a pure information-seeking question."""
    s = sentence.strip()
    if not s.endswith("?"):
        return False
    trimmed = _LEADING_FILLER_RE.sub("", s).strip()
    trimmed = re.sub(r"^actually,? ", "", trimmed, flags=re.IGNORECASE).strip()
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
    """Extract canonical stemmed domain/topic tokens (1 word -> 1 token)."""
    prose = _normalize_prose(text)
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
    return frozenset(tokens)


def _clean_phrase(raw: str) -> str:
    """Clean an extracted old/topic phrase for matching."""
    cleaned = raw.strip().strip("\"'`()[]{}")
    cleaned = re.sub(r"^(?:the|a|an) ", "", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.rstrip(".,;:!?")
    return " ".join(cleaned.split())


def _phrase_in_norm_text(phrase: str, norm_lower_text: str) -> bool:
    """Check whether ``phrase`` appears in pre-normalized lowercase text on token boundaries."""
    clean_phrase = _clean_phrase(phrase).lower()
    if not clean_phrase or not norm_lower_text:
        return False
    escaped = re.escape(clean_phrase)
    pattern = rf"(?<![a-z0-9]){escaped}(?![a-z0-9])"
    return re.search(pattern, norm_lower_text) is not None


def _is_negated_or_conditional_clause(sentence: str, pos: int) -> bool:
    """Return True if the clause preceding ``pos`` is negated or conditional/hypothetical."""
    prefix = sentence[:pos]
    # Trim to the current sub-clause (after last semicolon, colon, or "but"/"instead")
    for sep in (";", ":", " but "):
        idx = prefix.lower().rfind(sep)
        if idx != -1:
            prefix = prefix[idx + len(sep) :]
    prefix = prefix.strip()
    if not prefix:
        return False
    if _CLAUSE_CONDITIONAL_RE.search(prefix):
        return True
    return _CLAUSE_NEGATION_RE.search(prefix) is not None


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
                scrubbed = _NEG_DONT_FALSE_POSITIVE_RE.sub(" ", check)
                if not re.search(r"\b(?:do not|don't)\b", scrubbed, re.IGNORECASE):
                    continue
            if matched == "never" and _NEG_NEVER_FALSE_POSITIVE_RE.search(check):
                scrubbed = _NEG_NEVER_FALSE_POSITIVE_RE.sub(" ", check)
                if not re.search(r"\bnever\b", scrubbed, re.IGNORECASE):
                    continue
            return True
    return False


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
            norm_clause = _trim_non_alnum(cleaned.lower())
            if norm_clause not in _CHATTER_PHRASES:
                return True
        for sep in (":", ","):
            if sep in cleaned:
                tail = _LEADING_FILLER_RE.sub("", cleaned.split(sep, 1)[1].strip()).strip()
                if tail and _IMPERATIVE_START_RE.match(tail):
                    norm_tail = _trim_non_alnum(tail.lower())
                    if norm_tail not in _CHATTER_PHRASES:
                        return True
    return False


def _detect_override_signal(text: str) -> _OverrideSignal | None:
    """Inspect ``text`` for superseding correction / override cues in O(N) time."""
    prose = _normalize_prose(text)
    if not prose:
        return None

    sentences = _split_sentences(prose)
    non_question_sentences = [s for s in sentences if not _is_pure_info_question(s)]
    if not non_question_sentences:
        return None

    hard = False
    soft = False
    unscoped_retraction = False
    old_values: list[str] = []
    topic_hints: list[str] = []

    def _add_unique(target: list[str], value: str) -> None:
        cleaned = _clean_phrase(value)
        if cleaned and cleaned.lower() not in {v.lower() for v in target}:
            target.append(cleaned)

    has_any_directive = _has_directive_sentence(non_question_sentences) or is_negative_constraint(
        " ".join(non_question_sentences)
    )

    for raw_sent in non_question_sentences:
        sent = _strip_terminal_punct(raw_sent)
        if not sent:
            continue

        # 1. Explicit override headers ("Correction:", "Override:", "New rule:")
        if _EXPLICIT_OVERRIDE_HEADER_RE.match(sent):
            hard = True

        # 1b. "Update: <body>" is ONLY an override if <body> is a directive/constraint
        # (guards against status lines like "Update: the tests pass now.")
        upd_match = _UPDATE_HEADER_RE.match(sent)
        if upd_match:
            body = upd_match.group(1).strip()
            body_sents = _split_sentences(body)
            if _has_directive_sentence(body_sents) or is_negative_constraint(body):
                hard = True

        # 2. Explicit ignore/forget previous instruction (unless negated/conditional)
        for m in _IGNORE_FORGET_ABOUT_RE.finditer(sent):
            if _is_negated_or_conditional_clause(sent, m.start()):
                continue
            hard = True
            topic = m.group(1)
            if topic and topic.strip():
                _add_unique(topic_hints, topic)
            else:
                unscoped_retraction = True

        for m in _FORGET_ABOUT_DIRECT_RE.finditer(sent):
            if _is_negated_or_conditional_clause(sent, m.start()):
                continue
            hard = True
            _add_unique(topic_hints, m.group(1))

        # 3. Scratch that / strike that / cancel that (only at clause boundary)
        if _SCRATCH_THAT_RE.match(sent):
            hard = True
            unscoped_retraction = True

        # 4. Never mind ...
        nm_match = _NEVER_MIND_RE.match(sent)
        if nm_match:
            after_punct = nm_match.group(1)
            topic_arg = nm_match.group(2)
            if topic_arg and _extract_content_tokens(topic_arg):
                hard = True
                _add_unique(topic_hints, topic_arg)
                _add_unique(old_values, topic_arg)
            elif after_punct is not None:
                # "Never mind, use 30s" -> retraction; "Never mind, I found it" -> conversational!
                tail = after_punct.strip()
                if not tail or _has_directive_sentence([tail]) or is_negative_constraint(tail):
                    hard = True
                    unscoped_retraction = True
            else:
                # Bare "Never mind."
                hard = True
                unscoped_retraction = True

        # 5. "no, actually ..." / "wait, actually ..." / "on second thought ..."
        if _HARD_NO_ACTUALLY_RE.match(sent):
            after = _HARD_NO_ACTUALLY_RE.sub("", sent, count=1).lstrip(",:;- ")
            if not after or _has_directive_sentence([after]) or is_negative_constraint(after):
                hard = True

        # 6. "instead of <OLD>, use <NEW>" (prefix form has built-in directive verb)
        for m in _INSTEAD_OF_PREFIX_RE.finditer(sent):
            if _is_negated_or_conditional_clause(sent, m.start()):
                continue
            hard = True
            _add_unique(old_values, m.group(1))

        sent_after_actually = (
            re.sub(
                r"^(?:(?:no|wait|sorry), )?actually,? ",
                "",
                sent,
                count=1,
                flags=re.IGNORECASE,
            ).strip()
            if _SOFT_ACTUALLY_RE.match(sent)
            else ""
        )
        sent_has_directive = has_any_directive or (
            bool(sent_after_actually)
            and (
                _has_directive_sentence([sent_after_actually])
                or is_negative_constraint(sent_after_actually)
            )
        )

        # 6b. "<NEW> instead of <OLD>" ONLY in directive/override sentences
        # (guards against bug reports like "The function returns None instead of a list.")
        if sent_has_directive or hard:
            for m in _INSTEAD_OF_SUFFIX_RE.finditer(sent):
                if _is_negated_or_conditional_clause(sent, m.start()):
                    continue
                hard = True
                _add_unique(old_values, m.group(1))

        # 7. "rather than <OLD>" (only in directive sentences)
        if sent_has_directive:
            for m in _RATHER_THAN_RE.finditer(sent):
                if _is_negated_or_conditional_clause(sent, m.start()):
                    continue
                hard = True
                _add_unique(old_values, m.group(1))

        # 8. ", not <OLD>" contrast inside a directive sentence
        if sent_has_directive:
            for m in _NOT_CONTRAST_RE.finditer(sent):
                candidate_old = _clean_phrase(m.group(1))
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

        # 9. "switch/change/update/move <topic?> from <OLD> to <NEW>" (directive position only)
        for m in _FROM_X_TO_Y_RE.finditer(sent):
            if _is_negated_or_conditional_clause(sent, m.start()):
                continue
            rhs = _clean_phrase(m.group(3))
            if not rhs or _INFINITIVE_PURPOSE_START_RE.match(rhs):
                continue
            hard = True
            if m.group(1):
                _add_unique(topic_hints, m.group(1))
            _add_unique(old_values, m.group(2))

        # 10. "replace <OLD> with <NEW>" (directive position only)
        for m in _REPLACE_X_WITH_Y_RE.finditer(sent):
            if _is_negated_or_conditional_clause(sent, m.start()):
                continue
            hard = True
            _add_unique(old_values, m.group(1))

        # 11. "change/switch <OLD> to <NEW>" (directive position, not from..to, not infinitive)
        if not old_values:
            for m in _CHANGE_X_TO_Y_RE.finditer(sent):
                if _is_negated_or_conditional_clause(sent, m.start()):
                    continue
                lhs = _clean_phrase(m.group(1))
                rhs = _clean_phrase(m.group(2))
                if (
                    lhs
                    and rhs
                    and " from " not in lhs.lower()
                    and not _INFINITIVE_PURPOSE_START_RE.match(rhs)
                ):
                    hard = True
                    _add_unique(old_values, lhs)
                    _add_unique(topic_hints, lhs)

        # 12. "update the <TOPIC> to <NEW>" (soft override cue)
        for m in _UPDATE_TOPIC_TO_Y_RE.finditer(sent):
            if _is_negated_or_conditional_clause(sent, m.start()):
                continue
            topic = _clean_phrase(m.group(1))
            rhs = _clean_phrase(m.group(2))
            if (
                topic
                and rhs
                and not _INFINITIVE_PURPOSE_START_RE.match(rhs)
                and _extract_content_tokens(topic)
            ):
                soft = True
                _add_unique(topic_hints, topic)

        # 13. "switch to <NEW>" (soft override cue)
        if not old_values:
            for m in _SWITCH_TO_Y_RE.finditer(sent):
                if _is_negated_or_conditional_clause(sent, m.start()):
                    continue
                rhs = _clean_phrase(m.group(1))
                if rhs and not _INFINITIVE_PURPOSE_START_RE.match(rhs):
                    soft = True

        # 14. Soft "actually" ONLY when accompanied by a directive/constraint and NOT additive
        # (guards against "that actually worked!", "actually, also add an index", questions)
        if (
            not hard
            and _SOFT_ACTUALLY_RE.match(sent)
            and not _ADDITIVE_AFTER_ACTUALLY_RE.match(sent)
        ):
            after_actually = re.sub(
                r"^(?:(?:no|wait|sorry), )?actually,? ",
                "",
                sent,
                count=1,
                flags=re.IGNORECASE,
            ).strip()
            if _has_directive_sentence([after_actually]) or is_negative_constraint(after_actually):
                soft = True

    if not hard and not soft:
        return None

    if old_values or topic_hints:
        unscoped_retraction = False

    return _OverrideSignal(
        kind="hard" if hard else "soft",
        old_values=tuple(old_values),
        topic_hints=tuple(topic_hints),
        is_unscoped_retraction=unscoped_retraction,
    )


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
        return "```" not in clean and "~~~" not in clean

    # Pure punctuation / emoji / symbols (strictly O(N), never uses regex trim)
    if not any(ch.isalnum() for ch in prose):
        return True

    # Short-circuit long inputs BEFORE clause analysis or override checks
    if len(prose) > 140:
        return False

    # Never classify a genuine directive, negative constraint, or override as chatter.
    if is_negative_constraint(clean) or _detect_override_signal(clean) is not None:
        return False

    compact = " ".join(prose.lower().split())
    clauses = [c.strip() for c in _CLAUSE_SPLIT_RE.split(compact) if c.strip()]
    if not clauses:
        return True

    for clause in clauses:
        norm = " ".join(_trim_non_alnum(clause).split())
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
    candidate_norm_lower: str,
    candidate_own_tokens: frozenset[str],
    candidate_chain_tokens: frozenset[str],
    candidate_has_date: bool,
    override_has_date: bool,
) -> int:
    """Score how strongly ``signal`` targets a candidate pin. Return 0 for no match."""
    score = 0

    # 1. Explicit old-value matches (highest signal)
    for old_val in signal.old_values:
        if _phrase_in_norm_text(old_val, candidate_norm_lower):
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
        if _phrase_in_norm_text(hint, candidate_norm_lower):
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
                    for ht in hint_tokens:
                        if len(ht) >= 4 and any(
                            ct.startswith(ht) or ht.startswith(ct)
                            for ct in candidate_chain_tokens
                            if len(ct) >= 4
                        ):
                            score += 30
                            break

    # 3. General content-token overlap (1 word = 1 canonical stemmed token).
    # Soft overrides ("actually, ...") MUST share >= 2 distinct content tokens so one
    # incidental shared noun (e.g., "database" or "test") never falsely supersedes.
    own_overlap = override_tokens & candidate_own_tokens
    chain_overlap = override_tokens & candidate_chain_tokens
    has_explicit_target = bool(signal.old_values or signal.topic_hints)

    if len(own_overlap) >= 2:
        score += 25 * len(own_overlap)
    elif len(own_overlap) == 1 and not has_explicit_target and signal.kind == "hard":
        tok = next(iter(own_overlap))
        if len(tok) >= 4:
            score += 25
    elif len(chain_overlap) >= 2:
        score += 18 * len(chain_overlap)
    elif len(chain_overlap) == 1 and not has_explicit_target and signal.kind == "hard":
        tok = next(iter(chain_overlap))
        if len(tok) >= 4:
            score += 18

    # 4. Short date-only value override (e.g., "Use June 30." -> "Actually, use July 31.")
    if score == 0 and not has_explicit_target and override_has_date and candidate_has_date:
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
# Main extraction pipeline (O(N) via inverted token index)
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
    - Deduplicates repeated active instructions cleanly while still allowing
      repeated retractions (e.g., ``Scratch that.`` used twice on separate rules).
    """
    result: list[PinEntry] = []
    # pin_id -> (order_idx, own_tokens, chain_tokens, norm_lower_prose, has_date)
    pin_meta: dict[str, tuple[int, frozenset[str], frozenset[str], str, bool]] = {}
    # Inverted index: canonical token -> active pins containing that token
    token_index: dict[str, list[PinEntry]] = {}
    # Prefix bucket (first 4 chars) for abbreviation topic hints ("auth" <-> "authentication")
    prefix4_index: dict[str, list[PinEntry]] = {}
    # Active pins that contain a calendar date value
    date_pins: list[PinEntry] = []

    def _index_pin(
        pin: PinEntry,
        own_tokens: frozenset[str],
        chain_tokens: frozenset[str],
        norm_lower: str,
        has_date: bool,
    ) -> None:
        order_idx = len(result)
        result.append(pin)
        pin_meta[pin.pin_id] = (order_idx, own_tokens, chain_tokens, norm_lower, has_date)
        for tok in chain_tokens:
            token_index.setdefault(tok, []).append(pin)
            if len(tok) >= 4:
                prefix4_index.setdefault(tok[:4], []).append(pin)
        if has_date:
            date_pins.append(pin)

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
        toks = _extract_content_tokens(clean_text)
        norm_lower = " ".join(_normalize_prose(clean_text).lower().split())
        _index_pin(copied, toks, toks, norm_lower, bool(_DATE_VALUE_RE.search(norm_lower)))

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
    # Track the pin produced by the immediately preceding non-chatter user turn so
    # unscoped retractions ("Scratch that.", "Never mind.") never reach past unrelated turns.
    prev_non_chatter_pin: PinEntry | None = None

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
            # Non-chatter, non-directive turn (e.g., question or status update) breaks
            # adjacency for unscoped retractions.
            prev_non_chatter_pin = None
            continue

        own_tokens = _extract_content_tokens(clean_text)
        norm_lower = " ".join(_normalize_prose(clean_text).lower().split())
        override_has_date = bool(_DATE_VALUE_RE.search(norm_lower))
        superseded_target: PinEntry | None = None

        if signal is not None:
            # Gather candidate active pins via inverted index in O(matched) time
            query_tokens: set[str] = set(own_tokens)
            for old_val in signal.old_values:
                query_tokens.update(_extract_content_tokens(old_val))
            for hint in signal.topic_hints:
                query_tokens.update(_extract_content_tokens(hint))

            candidate_map: dict[str, PinEntry] = {}

            def _collect_from(bucket: list[PinEntry], out: dict[str, PinEntry]) -> None:
                # Lazily prune superseded pins while collecting active candidates
                alive: list[PinEntry] = []
                for p in bucket:
                    if p.superseded_by is None:
                        alive.append(p)
                        out[p.pin_id] = p
                if len(alive) != len(bucket):
                    bucket[:] = alive

            for tok in query_tokens:
                bucket = token_index.get(tok)
                if bucket:
                    _collect_from(bucket, candidate_map)

            for hint in signal.topic_hints:
                for ht in _extract_content_tokens(hint):
                    if len(ht) >= 4:
                        pbucket = prefix4_index.get(ht[:4])
                        if pbucket:
                            _collect_from(pbucket, candidate_map)

            if override_has_date and not signal.old_values and not signal.topic_hints and date_pins:
                _collect_from(date_pins, candidate_map)

            # Sort matched candidates newest-first for deterministic recency tie-breaking
            eligible: list[PinEntry] = []
            for cand in candidate_map.values():
                if cand.source == "manual" and (
                    cand.turn_index is None or cand.turn_index >= turn.turn_index
                ):
                    continue
                eligible.append(cand)
            eligible.sort(key=lambda p: pin_meta[p.pin_id][0], reverse=True)

            best_score = 0
            best_candidate: PinEntry | None = None
            for cand in eligible:
                _, cand_own, cand_chain, cand_norm_lower, cand_has_date = pin_meta[cand.pin_id]
                score = _score_supersede_candidate(
                    signal=signal,
                    override_text=clean_text,
                    override_tokens=own_tokens,
                    candidate_norm_lower=cand_norm_lower,
                    candidate_own_tokens=cand_own,
                    candidate_chain_tokens=cand_chain,
                    candidate_has_date=cand_has_date,
                    override_has_date=override_has_date,
                )
                if score > best_score:
                    best_score = score
                    best_candidate = cand

            if best_candidate is not None and best_score >= 25:
                superseded_target = best_candidate
            elif (
                signal.is_unscoped_retraction
                and prev_non_chatter_pin is not None
                and prev_non_chatter_pin.superseded_by is None
                and prev_non_chatter_pin.source != "manual"
            ):
                # Unscoped retraction only targets the immediately preceding active
                # transcript pin, never jumping over intervening unrelated turns.
                superseded_target = prev_non_chatter_pin

        # Deduplicate repeated identical/whitespace-equivalent active instructions,
        # UNLESS this turn supersedes a distinct active pin (e.g., repeated "Scratch that.").
        if superseded_target is None and norm in active_norms:
            prev_non_chatter_pin = None
            continue

        source = (
            "override"
            if (superseded_target is not None or (signal is not None and signal.kind == "hard"))
            else "auto"
        )

        pin_id = make_pin_id(source, norm, turn.turn_index)
        if pin_id in seen_pin_ids:
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
            _, _, prev_chain, _, prev_has_date = pin_meta[superseded_target.pin_id]
            chain_tokens = own_tokens | prev_chain
            override_has_date = override_has_date or prev_has_date

        seen_pin_ids.add(new_pin.pin_id)
        active_norms.add(norm)
        _index_pin(new_pin, own_tokens, chain_tokens, norm_lower, override_has_date)
        prev_non_chatter_pin = new_pin

    return result
