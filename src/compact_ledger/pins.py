"""Deterministic user constraint extraction and superseding correction engine.

Zero third-party dependencies, zero LLM calls, strictly linear-time O(N)
scanning (ReDoS-safe regexes, bounded lookback, and capped inverted-index
candidate lookup). Extracts verbatim user directives, negative constraints,
and superseding corrections from ordered transcript turns while filtering
conversational chatter and preserving explicit manual pins.
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

# Bounded linear-time performance limits (guarantee O(N) worst-case complexity)
_MAX_MATCHES_PER_SENTENCE = 8
_NEGATION_LOOKBACK_CHARS = 120
_MAX_BUCKET_SCAN = 64
_MAX_CHAIN_TOKENS = 32

# ---------------------------------------------------------------------------
# Unicode / fenced-code helpers (strictly linear time, zero backtracking)
# ---------------------------------------------------------------------------

_FENCE_OPEN_RE = re.compile(r"^[ \t>]*(`{3,}|~{3,})(.*)$")
_SMART_APOSTROPHE_RE = re.compile(r"[\u2018\u2019\u2032]")
_DASH_VARIANTS_RE = re.compile(r"[\u2013\u2014]")
_WORD_TOKEN_RE = re.compile(r"[a-z0-9]+")
_CAMEL_SPLIT_RE = re.compile(r"([a-z0-9])([A-Z])")
_SENTENCE_BREAK_RE = re.compile(r"(?<=[.!?]) ")

# Long-form turn segmentation (multi-block briefs, rule lists, long paragraphs)
_LONGFORM_PARAGRAPH_CHARS = 400
_MAX_SEGMENTS_PER_TURN = 200
_LIST_MARKER_RE = re.compile(r"^[ \t>]*(?:[-*+\u2022]|\d{1,3}[.)])[ \t]+(?:\[[ xX]\][ \t]+)?")
_HEADING_MARKER_RE = re.compile(r"^[ \t>]*#{1,6}[ \t]+")
_RULES_LEADIN_RE = re.compile(
    r"\b(?:rules?|constraints?|requirements?|guardrails?|invariants?|restrictions?|"
    r"non[- ]negotiables?|hard limits?|boundar(?:y|ies)|polic(?:y|ies)|"
    r"do(?:'s)? and don'?ts|must[- ]haves?|out of scope|off[- ]limits)\b",
    re.IGNORECASE,
)
_NEGATIVE_LEADIN_RE = re.compile(
    r"^(?:(?:please|you|we|and) )?(?:do not|don't|never|avoid|must not|should not|"
    r"shouldn't|out of scope|off[- ]limits|not allowed|forbidden)\b[^.!?]{0,60}$",
    re.IGNORECASE,
)
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
        r"deps|mocking|mocks|skipping|skipped|new (?:dependenc(?:y|ies)|deps|files?|"
        r"packages?|libraries|libs|abstractions?|features?)|emojis?|force[- ]push\w*|"
        r"floats?|global (?:state|variables?)|hardcod\w*|placeholders?|stubs?|todos?|"
        r"secrets?|wildcard imports?|em[- ]dashes?|console\.logs?|print statements?|"
        r"destructive \w+)\b",
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
    r"mind|care|like|have a|get why|get how)|"
    r"(?:if|when|whenever|since|because|although|though) (?:you|we|they|it) don't|"
    r"if (?:it|this|that) does not|"
    r"(?:(?:the|your|my|our|these|those) [a-z0-9_.-]+(?: [a-z0-9_.-]+)?|"
    r"they|these|those|it|this|that|tests?|builds?|hooks?|changes?|"
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
    r"(?:the|your|my|these|those) [a-z0-9_.-]+(?: [a-z0-9_.-]+)? (?:still )?never "
    r"(?:terminat\w*|finish\w*|pass\w*|work\w*|ran|run|runs|complet\w*|return\w*|"
    r"exits?|stops?|resolves?|triggers?|loads?|starts?|matches?|fires?))\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Positive directive / constraint detection (single-space normalized sentences)
# ---------------------------------------------------------------------------

_CONSTRAINT_KEYWORDS_RE = re.compile(
    r"\b(?:must(?! have\b)|have to|need(?:s)? to|required to|always|strictly|exclusively|"
    r"ensure|make sure|guarantee|enforce|preserve|maintain|stick (?:to|with)|"
    r"adhere to|comply with|remember (?:to|that)|"
    r"backward[s]?[- ]compatible|compatible with|stdlib only|zero[- ]dependenc(?:y|ies)|"
    r"fail[- ]open|deterministic)\b|"
    r"^(?:requirement|constraint|rule|important|note|policy|invariant):",
    re.IGNORECASE,
)

_ALWAYS_FALSE_POSITIVE_RE = re.compile(
    r"\b(?:i|we) always (?:forget|get|see|hit|run into|have trouble|wonder|mix up)\b",
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
    "prefer|treat|handle|emit|parse|render|configure|enable|disable|allow|lock|freeze|bump|"
    "downgrade|upgrade|assume|target|apply|stick|follow|match|respect|honor|retain|"
    "drop|omit|strip|wrap|prefix|suffix|encode|decode|serialize|deserialize|pass|bind|"
    "route|redirect|map|filter|group|split|merge|join|cache|flush|close|open|load|read|"
    "run|execute|go with|pick|choose"
)

# "For the frontend bits use pnpm", "In tests prefer fixtures" (scoped imperative)
_SCOPED_IMPERATIVE_RE = re.compile(
    r"^(?:for|in|on|inside|within|under|with) (?:[a-z0-9_./`'-]+ ){1,5}"
    r"(?:(?:please|always|only) )?(?:use|keep|prefer|stick (?:with|to)|avoid|go with) "
    r"[a-z0-9_./`'-]",
    re.IGNORECASE,
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

# Explicit permission-lifting phrases that CAN supersede a prior [DO NOT] negative constraint
_LIFT_NEGATION_RE = re.compile(
    r"\b(?:it's ok(?:ay)? to|it is ok(?:ay)? to|you can|feel free to|go ahead and|"
    r"allowed to|now ok(?:ay)? to|revert|retract|ignore|forget|disregard|scratch that)\b",
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

# Only match meta-instruction nouns at phrase end (so "ignore the rate limit errors" never matches)
_FORGET_ABOUT_DIRECT_RE = re.compile(
    r"\b(?:ignore|forget|disregard) (?:about )?the ([^,;!?]{1,60}) "
    r"(?:rule|constraint|requirement|instruction)(?:$|[,;:]| (?:for|about|now|please)\b)",
    re.IGNORECASE,
)

# Hesitation fillers allowed before a retraction ("hmm scratch that", "oh wait, never mind")
_RETRACTION_FILLER_RE = re.compile(
    r"^(?:(?:h+m+|u+m+|u+h+|oh|ah|well|so|ok(?:ay)?|sorry|oops|wait|yeah|ugh|argh)\b"
    r"[,;:. -]*){1,3}",
    re.IGNORECASE,
)

# Must end at clause boundary so "ignore that lint warning" NEVER matches!
_SCRATCH_THAT_RE = re.compile(
    r"^(?:scratch that|strike that|belay that|cancel that|"
    r"disregard that|forget that|ignore that)(?:$|[,;:-])",
    re.IGNORECASE,
)

# Only capture a topic after "never mind" when it's an explicit noun phrase ("the X", "about X",
# "my X", "that X"), NOT a finite clause like "nvm it works" or "never mind I found it"!
_NEVER_MIND_RE = re.compile(
    r"^(?:never ?mind|nvm)(?:$|[,;:-] ?(.*)$| (?:about |the |my |that )([^,;!?]{1,60}))",
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

_NOT_CONTRAST_NON_VALUE_START_RE = re.compile(
    r"^(?:sure|really|yet|now|anymore|at all|bad|good|working|needed|necessary|"
    r"ready|going|trying|able|just|only|even|quite|too|very|because|that|if|"
    r"whether|when|while|unless|until|for|to|a good|a bad|worth|possible|clear)\b",
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

# Positive adoption phrases used to retire earlier active negative constraints that
# directly forbid the newly adopted target (e.g., "Never use spaces." retired by
# "Actually use 2 spaces instead of tabs.").
# Note: sentences already have terminal [.!?] stripped, so keeping internal '.' preserves
# dotted identifiers/versions like os.system, console.log, Python 3.12, and TLS 1.0.
_ADOPT_DIRECTIVE_RE = re.compile(
    r"\b(?P<verb>use|stick (?:with|to)|go with|switch to|prefer|pick|choose|adopt|"
    r"enable|allow|add|install|import) (?P<rest>[^!?;,]{1,80})",
    re.IGNORECASE,
)
_ADOPT_DIRECTIVE_PREFIX_RE = re.compile(
    r"^(?:(?:no|wait|sorry|actually|correction:|override:|update:|change of plan:|"
    r"new rule:|revised rule:|updated rule:|on second thought|instead|please|kindly|"
    r"let's|lets|we should|you should|we must|you must|we will|we'll|you can|"
    r"go ahead and|be sure to|just|now|so|then|and|always|only|"
    r"instead of [^!?;,]{1,60})[,: -]*)*$",
    re.IGNORECASE,
)
_ADOPT_ADD_VERBS: frozenset[str] = frozenset({"add", "install", "import"})
_ADOPT_BOUNDARY_RE = re.compile(
    r"\b(?P<kw>instead of|rather than|without|except|but|unless|if|like|as|from|"
    r"over|for|in|on|under|inside|within|when|while|during|at|because|so|to|not|and|or)\b",
    re.IGNORECASE,
)
_ADOPT_SCOPE_KEYWORDS: frozenset[str] = frozenset(
    {"in", "for", "on", "under", "inside", "within", "when", "during", "at"}
)
_PROHIBIT_VERB_RE = re.compile(
    r"\b(?:never|do not|don't|must not|mustn't|should not|shouldn't|cannot|can't|"
    r"avoid|stop|disallow|forbid|prohibit) (?:ever )?"
    r"(?P<verb>use|using|add|adding|import|importing|install|installing|allow|allowing|"
    r"enable|enabling|prefer|pick|picking|choose|choosing) (?P<rest>[^!?;,]{1,80})",
    re.IGNORECASE,
)
_PROHIBIT_ADD_VERBS: frozenset[str] = frozenset(
    {"add", "adding", "import", "importing", "install", "installing"}
)
_PROHIBIT_BARE_NO_RE = re.compile(
    r"(?:^|[,;:] )(?:no|avoid|never|forbid|prohibit|disallow) (?P<rest>[^!?;,]{1,80})",
    re.IGNORECASE,
)
_PROHIBIT_IS_FORBIDDEN_RE = re.compile(
    r"(?:^|[,;:] )(?P<rest>[a-z0-9_./-]{2,40}(?: [a-z0-9_./-]{1,30}){0,2}) "
    r"(?:is|are) (?:forbidden|prohibited|disallowed|not allowed)\b",
    re.IGNORECASE,
)
_PROHIBIT_SCOPE_PREP_RE = re.compile(
    r" (?:in|for|on|under|inside|within|when|during|at) (.+)$",
    re.IGNORECASE,
)
_TARGET_ATOM_RE = re.compile(r"[a-z0-9]+(?:[._/-][a-z0-9]+)*")
_PURE_NUMERIC_ATOM_RE = re.compile(r"^\d+(?:\.\d+)*(?:st|nd|rd|th)?$", re.IGNORECASE)
_NON_NOUN_PROHIBIT_STARTS: frozenset[str] = frozenset(
    {
        "use",
        "using",
        "add",
        "adding",
        "import",
        "importing",
        "install",
        "installing",
        "touch",
        "touching",
        "modify",
        "modifying",
        "edit",
        "editing",
        "change",
        "changing",
        "changes",
        "edits",
        "modifications",
        "break",
        "breaking",
        "delete",
        "deleting",
        "remove",
        "removing",
        "push",
        "pushing",
        "commit",
        "committing",
        "drop",
        "dropping",
        "skip",
        "skipping",
        "run",
        "running",
        "call",
        "calling",
        "leak",
        "leaking",
        "expose",
        "exposing",
        "mutate",
        "mutating",
        "write",
        "writing",
        "writes",
        "rewrite",
        "rewriting",
        "leave",
        "leaving",
        "mention",
        "mentioning",
        "new",
        "more",
        "extra",
        "additional",
    }
)
_GENERIC_CONTRADICTION_TOKENS: frozenset[str] = frozenset(
    {
        "default",
        "value",
        "option",
        "setting",
        "config",
        "mode",
        "type",
        "data",
        "item",
        "list",
        "string",
        "number",
        "line",
        "text",
        "output",
        "input",
        "format",
        "style",
        "test",
        "name",
        "path",
        "method",
        "function",
        "class",
        "version",
        "tool",
        "command",
        "script",
        "package",
        "library",
        "module",
        "service",
        "server",
        "client",
        "system",
        "state",
        "error",
        "result",
    }
)

# Navigation targets ("switch to the main branch", "switch to the tests directory")
_WORKSPACE_NAV_TARGET_RE = re.compile(
    r"\b(?:branch|directory|dir|folder|repo|repository|worktree|tab|window|terminal)\b",
    re.IGNORECASE,
)

# Clause-level negation and conditional/hypothetical guards (O(1) bounded lookback)
_CLAUSE_NEGATION_RE = re.compile(
    r"\b(?:do not|don't|never|no longer|must not|mustn't|shall not|should not|shouldn't|"
    r"cannot|can't|avoid|let's not|lets not|please not|not to|rather not|"
    r"no need to|don't want to|do not want to|don't need to|do not need to|"
    r"without|stop|refuse to|forbid\w*|prohibit\w*)\b",
    re.IGNORECASE,
)

_CLAUSE_CONDITIONAL_RE = re.compile(
    r"^(?:if|when|whenever|unless|suppose|assuming|what if|in case)\b",
    re.IGNORECASE,
)

# Infinitive purpose verbs after "to" (distinguishes "change tests to use fixtures" or
# "update README to mention X" from value replacements like "change timeout to 60s")
_INFINITIVE_PURPOSE_START_RE = re.compile(
    r"^(?:get|see|check|find|verify|ensure|avoid|enable|disable|make|be|have|keep|"
    r"use|mention|include|add|remove|delete|support|handle|accept|return|show|display|"
    r"log|print|emit|run|test|pass|fail|fix|allow|prevent|block|validate|parse|render|"
    r"format|sort|cache|load|read|write|save|store|send|fetch|call|import|export|"
    r"reflect|match|document|explain|describe|cover|require|enforce|preserve|maintain|"
    r"work|do|update|improve|simplify|clean|refactor|split|merge|ignore|skip)\b",
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
class _AdoptedTarget:
    phrase: str
    kind: str  # "use" or "add"
    scope_tokens: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class _ProhibitedTarget:
    phrase: str
    kind: str  # "use" or "add"
    ident_tokens: frozenset[str]
    numeric_tokens: frozenset[str]
    scope_tokens: frozenset[str]


@dataclass(frozen=True, slots=True)
class _OverrideSignal:
    kind: str  # "hard" or "soft"
    old_values: tuple[str, ...]
    topic_hints: tuple[str, ...]
    is_unscoped_retraction: bool
    is_explicit_retraction: bool
    lifts_negation: bool
    adopted_targets: tuple[_AdoptedTarget, ...] = ()


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

    Handles indented fences and blockquote-prefixed fences (``> ``` ``).
    Per CommonMark, a backtick opening fence cannot contain backticks in its info
    string, so inline triple-backtick spans (e.g., `` ```cmd``` do not run ``)
    are preserved as prose rather than treated as an unclosed code fence.
    Unclosed multi-line fences consume the remainder of the text.
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
                if marker[0] == "`" and "`" in rest:
                    out_lines.append(line)
                    continue
                fence_char = marker[0]
                fence_len = len(marker)
                in_fence = True
                continue
            out_lines.append(line)
        else:
            stripped = line.lstrip(" \t>")
            if (
                stripped.startswith(fence_char * fence_len)
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


def _phrase_in_norm_text(clean_lower_phrase: str, norm_lower_text: str) -> bool:
    """Check whether ``clean_lower_phrase`` appears in normalized text on token boundaries."""
    if not clean_lower_phrase or not norm_lower_text or clean_lower_phrase not in norm_lower_text:
        return False
    escaped = re.escape(clean_lower_phrase)
    pattern = rf"(?<![a-z0-9]){escaped}(?![a-z0-9])"
    return re.search(pattern, norm_lower_text) is not None


def _match_clause_start(sentence: str, match: re.Match[str]) -> int:
    """Return index of the first word character inside ``match`` (skipping leading [,;:] )."""
    pos = match.start()
    end = match.end()
    while pos < end and sentence[pos] in ",;: ":
        pos += 1
    return pos


def _is_negated_or_conditional_clause(sentence: str, pos: int) -> bool:
    """Return True if the sub-clause immediately preceding ``pos`` is negated or conditional.

    Strictly O(1): only inspects a bounded ``_NEGATION_LOOKBACK_CHARS`` window before ``pos``.
    """
    window_start = max(0, pos - _NEGATION_LOOKBACK_CHARS)
    prefix = sentence[window_start:pos]
    for sep in (";", ":", ",", " but "):
        idx = prefix.lower().rfind(sep)
        if idx != -1:
            prefix = prefix[idx + len(sep) :]
    prefix = prefix.strip()
    if not prefix:
        return False
    if _CLAUSE_CONDITIONAL_RE.search(prefix):
        return True
    return _CLAUSE_NEGATION_RE.search(prefix) is not None


def _bounded_finditer(pattern: re.Pattern[str], text: str) -> list[re.Match[str]]:
    """Return up to ``_MAX_MATCHES_PER_SENTENCE`` matches to guarantee O(N) runtime."""
    matches: list[re.Match[str]] = []
    for m in pattern.finditer(text):
        matches.append(m)
        if len(matches) >= _MAX_MATCHES_PER_SENTENCE:
            break
    return matches


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

        kw_match = _CONSTRAINT_KEYWORDS_RE.search(cleaned)
        if kw_match:
            if kw_match.group(0).lower() == "always" and _ALWAYS_FALSE_POSITIVE_RE.search(cleaned):
                scrubbed = _ALWAYS_FALSE_POSITIVE_RE.sub(" ", cleaned)
                if _CONSTRAINT_KEYWORDS_RE.search(scrubbed):
                    return True
            else:
                return True
        if _RESTRICTIVE_DIRECTIVE_RE.search(cleaned):
            return True
        if _IMPERATIVE_START_RE.match(cleaned):
            norm_clause = _trim_non_alnum(cleaned.lower())
            if norm_clause not in _CHATTER_PHRASES:
                return True
        if _SCOPED_IMPERATIVE_RE.match(cleaned):
            return True
        for sep in (":", ","):
            if sep in cleaned:
                tail = _LEADING_FILLER_RE.sub("", cleaned.split(sep, 1)[1].strip()).strip()
                if tail and _IMPERATIVE_START_RE.match(tail):
                    norm_tail = _trim_non_alnum(tail.lower())
                    if norm_tail not in _CHATTER_PHRASES:
                        return True
    return False


def _parse_adopted_span(raw: str) -> tuple[str, frozenset[str]]:
    """Split an adopted phrase from any trailing boundary/scope clause."""
    match = _ADOPT_BOUNDARY_RE.search(raw)
    scope_tokens: frozenset[str] = frozenset()
    if match:
        trimmed = raw[: match.start()]
        kw = match.group("kw").lower()
        if kw in _ADOPT_SCOPE_KEYWORDS:
            tail = raw[match.end() :].strip()
            # Drop contrast tail like "instead of ..." from the scope clause.
            tail_contrast = re.search(r"\b(?:instead of|rather than)\b", tail, re.IGNORECASE)
            if tail_contrast:
                tail = tail[: tail_contrast.start()].strip()
            scope_tokens = _extract_content_tokens(tail)
    else:
        trimmed = raw

    cleaned = _clean_phrase(trimmed)
    if not cleaned or _INFINITIVE_PURPOSE_START_RE.match(cleaned):
        return "", frozenset()
    if _WORKSPACE_NAV_TARGET_RE.search(cleaned):
        return "", frozenset()
    return cleaned, scope_tokens


def _extract_target_tokens(text: str) -> tuple[frozenset[str], frozenset[str]]:
    """Extract ``(ident_tokens, numeric_tokens)`` preserving dotted identifiers & versions."""
    prose = _normalize_prose(text).lower()
    idents: set[str] = set()
    nums: set[str] = set()
    for atom in _TARGET_ATOM_RE.findall(prose):
        if _PURE_NUMERIC_ATOM_RE.fullmatch(atom):
            nums.add(atom.lower())
            continue
        if "." in atom or "/" in atom:
            # Dotted identifiers (os.system, console.log) and paths stay atomic.
            idents.add(atom)
            continue
        if atom in _STOPWORDS:
            continue
        if len(atom) == 1:
            continue
        stemmed = _stem_token(atom)
        if stemmed in _STOPWORDS:
            continue
        idents.add(stemmed)
    return frozenset(idents), frozenset(nums)


_ADOPT_HEADER_COLON_RE = re.compile(
    r"(?:correction|override|update|change of plan|new rule|revised rule|updated rule)$",
    re.IGNORECASE,
)
_PARENTHETICAL_NEG_RE = re.compile(
    r"\b(?:never|do not|don't|must not|mustn't|should not|shouldn't|cannot|can't|"
    r"avoid|stop|no longer)\s*,\s*[^,]{1,50},\s*$",
    re.IGNORECASE,
)


def _is_directive_adopt_prefix(sentence: str, pos: int) -> bool:
    """Return True if the clause prefix before ``pos`` is in directive position."""
    window_start = max(0, pos - _NEGATION_LOOKBACK_CHARS)
    raw_prefix = sentence[window_start:pos]
    if _PARENTHETICAL_NEG_RE.search(raw_prefix):
        return False
    prefix = raw_prefix
    for sep in (";", " but "):
        idx = prefix.lower().rfind(sep)
        if idx != -1:
            prefix = prefix[idx + len(sep) :]
    colon_idx = prefix.rfind(":")
    if colon_idx != -1:
        before_colon = prefix[:colon_idx].strip()
        if _ADOPT_HEADER_COLON_RE.search(before_colon):
            prefix = prefix[colon_idx + 1 :]
        elif _CLAUSE_NEGATION_RE.search(before_colon):
            return False
        else:
            prefix = prefix[colon_idx + 1 :]
    comma_idx = prefix.rfind(",")
    if comma_idx != -1:
        prefix = prefix[comma_idx + 1 :]
    prefix = prefix.strip()
    if not prefix:
        return True
    if _CLAUSE_CONDITIONAL_RE.search(prefix) or _CLAUSE_NEGATION_RE.search(prefix):
        return False
    return bool(_ADOPT_DIRECTIVE_PREFIX_RE.match(prefix))


def _extract_prohibited_targets(text: str) -> list[_ProhibitedTarget]:
    """Extract structured prohibited targets from a negative constraint."""
    prose = _normalize_prose(text)
    if not prose:
        return []

    found: list[_ProhibitedTarget] = []
    seen: set[tuple[str, frozenset[str], frozenset[str], frozenset[str]]] = set()

    def _record(raw_span: str, *, kind: str, allow_verb_start: bool) -> None:
        span = raw_span.strip()
        if not span:
            return
        scope_match = _PROHIBIT_SCOPE_PREP_RE.search(span)
        if scope_match:
            target_part = span[: scope_match.start()]
            scope_part = scope_match.group(1)
        else:
            target_part = span
            scope_part = ""
        clean_target = _clean_phrase(target_part)
        if not clean_target:
            return
        if not allow_verb_start:
            first_word = clean_target.split(" ", 1)[0].lower()
            if first_word in _NON_NOUN_PROHIBIT_STARTS:
                return
        ident_toks, num_toks = _extract_target_tokens(clean_target)
        if not ident_toks:
            return
        scope_toks = _extract_content_tokens(scope_part) if scope_part else frozenset()
        key = (kind, ident_toks, num_toks, scope_toks)
        if key not in seen:
            seen.add(key)
            found.append(
                _ProhibitedTarget(
                    phrase=clean_target.lower(),
                    kind=kind,
                    ident_tokens=ident_toks,
                    numeric_tokens=num_toks,
                    scope_tokens=scope_toks,
                )
            )

    for raw_sent in _split_sentences(prose):
        if _is_pure_info_question(raw_sent):
            continue
        sent = _strip_terminal_punct(raw_sent)
        if not sent:
            continue
        for m in _bounded_finditer(_PROHIBIT_VERB_RE, sent):
            verb = m.group("verb").lower()
            kind = "add" if verb in _PROHIBIT_ADD_VERBS else "use"
            _record(m.group("rest"), kind=kind, allow_verb_start=True)
        for m in _bounded_finditer(_PROHIBIT_BARE_NO_RE, sent):
            _record(m.group("rest"), kind="use", allow_verb_start=False)
        for m in _bounded_finditer(_PROHIBIT_IS_FORBIDDEN_RE, sent):
            _record(m.group("rest"), kind="use", allow_verb_start=False)

    return found


def _contradicts_negative_pin(
    adopted_specs: Sequence[tuple[str, frozenset[str], frozenset[str], frozenset[str]]],
    override_tokens: frozenset[str],
    old_val_tokens: frozenset[str],
    has_old_values: bool,
    candidate: PinEntry,
    candidate_own_tokens: frozenset[str],
    candidate_prohibited: Sequence[_ProhibitedTarget],
) -> bool:
    """Return True iff an override's adopted target directly contradicts an active negative pin.

    False-positive guards:
    - Prohibited verb must be compatible (an ``add`` ban is not lifted by a ``use`` adoption).
    - Dotted identifiers/paths stay atomic (``os.system`` != ``os.path.join``).
    - Numeric/version qualifiers on the ban must match (``Python 2`` != ``Python 3.12``).
    - Every core identifier token of the ban must appear in the adopted target
      (``trailing spaces`` is NOT retired by ``2 spaces``).
    - Single overly generic tokens (``default``, ``value``, ``config``, ...) never match.
    - Scoped bans require ALL scope tokens to appear in the override (subset, not intersection),
      so ``mocks in integration tests`` is NOT retired by ``mocks in unit tests``.
    - Scoped exceptions without explicit ``old_values`` (``use console.log in the debug script``)
      do NOT retire an unscoped ban (``Never use console.log.``).
    """
    if not candidate.is_negative or not adopted_specs or not candidate_prohibited:
        return False

    for adopt_kind, adopt_idents, adopt_nums, adopt_scope in adopted_specs:
        if not adopt_idents:
            continue
        for prohib in candidate_prohibited:
            if prohib.kind == "add" and adopt_kind != "add":
                continue
            if not prohib.ident_tokens:
                continue
            if (
                len(prohib.ident_tokens) == 1
                and next(iter(prohib.ident_tokens)) in _GENERIC_CONTRADICTION_TOKENS
            ):
                continue
            if not prohib.ident_tokens.issubset(adopt_idents):
                continue
            # If the prohibition pins a specific version/number (e.g. Python 2, port 22, TLS 1.0),
            # require the adopted target to include those exact numeric tokens.
            if prohib.numeric_tokens and not prohib.numeric_tokens.issubset(adopt_nums):
                continue
            # Scoped exception without an explicit replacement ("use X in debug script")
            # must NOT retire an unscoped ban ("Never use X.").
            if not prohib.scope_tokens and adopt_scope and not has_old_values:
                continue
            # Scoped ban ("mocks in integration tests") requires ALL scope tokens in override.
            if (
                prohib.scope_tokens
                and not prohib.scope_tokens.issubset(override_tokens)
                and not (old_val_tokens and (old_val_tokens & candidate_own_tokens))
            ):
                continue
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
    explicit_retraction = False
    lifts_negation = False
    old_values: list[str] = []
    topic_hints: list[str] = []
    adopted_targets: list[_AdoptedTarget] = []
    seen_adopted: set[tuple[str, str, frozenset[str]]] = set()
    override_sentences: list[str] = []

    def _add_unique(target: list[str], value: str) -> None:
        cleaned = _clean_phrase(value)
        if cleaned and cleaned.lower() not in {v.lower() for v in target}:
            target.append(cleaned)

    def _add_adopted(raw: str, *, kind: str = "use") -> None:
        phrase, scope_toks = _parse_adopted_span(raw)
        if not phrase:
            return
        key = (phrase.lower(), kind, scope_toks)
        if key not in seen_adopted:
            seen_adopted.add(key)
            adopted_targets.append(
                _AdoptedTarget(phrase=phrase, kind=kind, scope_tokens=scope_toks)
            )

    prev_sent_was_retraction = False
    for raw_sent in non_question_sentences:
        sent = _strip_terminal_punct(raw_sent)
        if not sent:
            continue
        hard_before = hard
        soft_before = soft
        retraction_before = explicit_retraction

        if _LIFT_NEGATION_RE.search(sent):
            lifts_negation = True

        # Per-sentence directive check (never leaks across unrelated sentences in one turn)
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
        sent_has_directive = (
            _has_directive_sentence([sent])
            or is_negative_constraint(sent)
            or (
                bool(sent_after_actually)
                and (
                    _has_directive_sentence([sent_after_actually])
                    or is_negative_constraint(sent_after_actually)
                )
            )
        )

        # 1. Explicit override headers ("Correction:", "Override:", "New rule:")
        if _EXPLICIT_OVERRIDE_HEADER_RE.match(sent):
            hard = True

        # 1b. "Update: <body>" is ONLY an override if <body> is a directive/constraint
        upd_match = _UPDATE_HEADER_RE.match(sent)
        if upd_match:
            body = upd_match.group(1).strip()
            body_sents = _split_sentences(body)
            if _has_directive_sentence(body_sents) or is_negative_constraint(body):
                hard = True

        # 2. Explicit ignore/forget previous instruction (unless negated/conditional)
        for m in _bounded_finditer(_IGNORE_FORGET_ABOUT_RE, sent):
            if _is_negated_or_conditional_clause(sent, _match_clause_start(sent, m)):
                continue
            hard = True
            explicit_retraction = True
            topic = m.group(1)
            if topic and topic.strip():
                _add_unique(topic_hints, topic)
            else:
                unscoped_retraction = True

        for m in _bounded_finditer(_FORGET_ABOUT_DIRECT_RE, sent):
            if _is_negated_or_conditional_clause(sent, _match_clause_start(sent, m)):
                continue
            hard = True
            explicit_retraction = True
            _add_unique(topic_hints, m.group(1))

        # 3. Scratch that / strike that / cancel that (only at clause boundary)
        retraction_sent = _RETRACTION_FILLER_RE.sub("", sent, count=1)
        if _SCRATCH_THAT_RE.match(retraction_sent):
            hard = True
            explicit_retraction = True
            unscoped_retraction = True

        # 4. Never mind ... (noun-phrase topic OR bare/directive retraction only)
        nm_match = _NEVER_MIND_RE.match(retraction_sent)
        if nm_match:
            after_punct = nm_match.group(1)
            topic_arg = nm_match.group(2)
            if topic_arg and _extract_content_tokens(topic_arg):
                hard = True
                explicit_retraction = True
                _add_unique(topic_hints, topic_arg)
                _add_unique(old_values, topic_arg)
            elif after_punct is not None:
                tail = after_punct.strip()
                if not tail or _has_directive_sentence([tail]) or is_negative_constraint(tail):
                    hard = True
                    explicit_retraction = True
                    unscoped_retraction = True
            else:
                hard = True
                explicit_retraction = True
                unscoped_retraction = True

        # 5. "no, actually ..." / "wait, actually ..." / "on second thought ..."
        if _HARD_NO_ACTUALLY_RE.match(sent):
            after = _HARD_NO_ACTUALLY_RE.sub("", sent, count=1).lstrip(",:;- ")
            if not after or _has_directive_sentence([after]) or is_negative_constraint(after):
                hard = True

        # 6. "instead of <OLD>, <NEW>" requires directive context in <NEW>
        for m in _bounded_finditer(_INSTEAD_OF_PREFIX_RE, sent):
            if _is_negated_or_conditional_clause(sent, _match_clause_start(sent, m)):
                continue
            tail = (m.group(2) or "").strip()
            matched_full = m.group(0)
            has_comma_only = ", " in matched_full and not re.search(
                r"\b(?:use|do|keep|set|make|pick|choose|go with|try|run|write|return|"
                r"output|format|switch to)\b",
                matched_full,
                re.IGNORECASE,
            )
            if has_comma_only and not (
                _has_directive_sentence([tail]) or is_negative_constraint(tail)
            ):
                continue
            hard = True
            _add_unique(old_values, m.group(1))

        # 6b. "<NEW> instead of <OLD>" ONLY when THIS sentence is a directive/override
        if sent_has_directive or hard:
            for m in _bounded_finditer(_INSTEAD_OF_SUFFIX_RE, sent):
                if _is_negated_or_conditional_clause(sent, _match_clause_start(sent, m)):
                    continue
                # Avoid matching prefix form that failed the directive check above
                if m.start() == 0 and not sent_has_directive:
                    continue
                hard = True
                _add_unique(old_values, m.group(1))

        # 7. "rather than <OLD>" (only when THIS sentence is a directive)
        if sent_has_directive:
            for m in _bounded_finditer(_RATHER_THAN_RE, sent):
                if _is_negated_or_conditional_clause(sent, _match_clause_start(sent, m)):
                    continue
                hard = True
                _add_unique(old_values, m.group(1))

        # 8. ", not <OLD>" contrast inside THIS directive sentence
        if sent_has_directive:
            for m in _bounded_finditer(_NOT_CONTRAST_RE, sent):
                candidate_old = _clean_phrase(m.group(1))
                if candidate_old and not _NOT_CONTRAST_NON_VALUE_START_RE.match(candidate_old):
                    hard = True
                    _add_unique(old_values, candidate_old)

        # 9. "switch/change/update/move <topic?> from <OLD> to <NEW>" (directive position only)
        for m in _bounded_finditer(_FROM_X_TO_Y_RE, sent):
            if _is_negated_or_conditional_clause(sent, _match_clause_start(sent, m)):
                continue
            rhs = _clean_phrase(m.group(3))
            if not rhs or _INFINITIVE_PURPOSE_START_RE.match(rhs):
                continue
            hard = True
            if m.group(1):
                _add_unique(topic_hints, m.group(1))
            _add_unique(old_values, m.group(2))
            _add_adopted(m.group(3))

        # 10. "replace <OLD> with <NEW>" (directive position only)
        for m in _bounded_finditer(_REPLACE_X_WITH_Y_RE, sent):
            if _is_negated_or_conditional_clause(sent, _match_clause_start(sent, m)):
                continue
            hard = True
            _add_unique(old_values, m.group(1))
            _add_adopted(m.group(2))

        # 11. "change/switch <OLD> to <NEW>" (directive position, not from..to, not infinitive)
        if not old_values:
            for m in _bounded_finditer(_CHANGE_X_TO_Y_RE, sent):
                if _is_negated_or_conditional_clause(sent, _match_clause_start(sent, m)):
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
                    _add_adopted(m.group(2))

        # 12. "update the <TOPIC> to <NEW>" (soft override cue; skip infinitives)
        for m in _bounded_finditer(_UPDATE_TOPIC_TO_Y_RE, sent):
            if _is_negated_or_conditional_clause(sent, _match_clause_start(sent, m)):
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
                _add_adopted(m.group(2))

        # 13. "switch to <NEW>" (soft override cue; skip infinitives & workspace navigation)
        if not old_values:
            for m in _bounded_finditer(_SWITCH_TO_Y_RE, sent):
                if _is_negated_or_conditional_clause(sent, _match_clause_start(sent, m)):
                    continue
                rhs = _clean_phrase(m.group(1))
                if (
                    rhs
                    and not _INFINITIVE_PURPOSE_START_RE.match(rhs)
                    and not _WORKSPACE_NAV_TARGET_RE.search(rhs)
                ):
                    soft = True
                    _add_adopted(m.group(1))

        # 14. Soft "actually" ONLY when accompanied by a directive/constraint and NOT additive
        if (
            not hard
            and _SOFT_ACTUALLY_RE.match(sent)
            and not _ADDITIVE_AFTER_ACTUALLY_RE.match(sent)
            and sent_after_actually
            and (
                _has_directive_sentence([sent_after_actually])
                or is_negative_constraint(sent_after_actually)
            )
        ):
            soft = True

        sent_is_override = (
            hard != hard_before
            or soft != soft_before
            or prev_sent_was_retraction
            or _SOFT_ACTUALLY_RE.match(sent) is not None
            or _EXPLICIT_OVERRIDE_HEADER_RE.match(sent) is not None
        )
        if sent_is_override:
            override_sentences.append(sent)
        prev_sent_was_retraction = explicit_retraction != retraction_before

    if not hard and not soft:
        return None

    # Extract explicit positive adoption targets ONLY from override-carrying sentences
    # and ONLY when the adoption verb is in directive position.
    for sent in override_sentences:
        for m in _bounded_finditer(_ADOPT_DIRECTIVE_RE, sent):
            if not _is_directive_adopt_prefix(sent, m.start()):
                continue
            verb = m.group("verb").lower()
            kind = "add" if verb in _ADOPT_ADD_VERBS else "use"
            _add_adopted(m.group("rest"), kind=kind)

    if old_values or topic_hints:
        unscoped_retraction = False

    return _OverrideSignal(
        kind="hard" if hard else "soft",
        old_values=tuple(old_values),
        topic_hints=tuple(topic_hints),
        is_unscoped_retraction=unscoped_retraction,
        is_explicit_retraction=explicit_retraction,
        lifts_negation=lifts_negation,
        adopted_targets=tuple(adopted_targets),
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
    override_tokens: frozenset[str],
    old_val_specs: Sequence[tuple[str, frozenset[str]]],
    hint_specs: Sequence[tuple[str, frozenset[str]]],
    candidate: PinEntry,
    candidate_norm_lower: str,
    candidate_own_tokens: frozenset[str],
    candidate_chain_tokens: frozenset[str],
    candidate_has_date: bool,
    override_has_date: bool,
) -> int:
    """Score how strongly ``signal`` targets a candidate pin. Return 0 for no match."""
    score = 0
    direct_value_or_topic_match = False

    # 1. Explicit old-value matches (highest signal).
    # For multi-word old_values (e.g. "old logging config"), require ALL tokens to match
    # (or >= 2 tokens covering >= 50%) so a single incidental word ("config") never misfires.
    for clean_old_lower, old_tokens in old_val_specs:
        if _phrase_in_norm_text(clean_old_lower, candidate_norm_lower):
            score += 120
            direct_value_or_topic_match = True
            continue
        if not old_tokens:
            continue
        own_hit = old_tokens & candidate_own_tokens
        if own_hit == old_tokens:
            score += 100
            direct_value_or_topic_match = True
        elif len(old_tokens) >= 2 and len(own_hit) >= 2 and len(own_hit) * 2 >= len(old_tokens):
            score += 45 * len(own_hit)
            direct_value_or_topic_match = True
        else:
            chain_hit = old_tokens & candidate_chain_tokens
            if chain_hit == old_tokens:
                score += 70
                direct_value_or_topic_match = True

    # 2. Explicit topic-hint matches ("ignore what I said earlier about auth")
    for clean_hint_lower, hint_tokens in hint_specs:
        if _phrase_in_norm_text(clean_hint_lower, candidate_norm_lower):
            score += 90
            direct_value_or_topic_match = True
            continue
        if not hint_tokens:
            continue
        own_hit = hint_tokens & candidate_own_tokens
        if own_hit == hint_tokens or (len(hint_tokens) == 1 and own_hit):
            score += 50 * len(own_hit)
            direct_value_or_topic_match = True
        else:
            chain_hit = hint_tokens & candidate_chain_tokens
            if chain_hit == hint_tokens or (len(hint_tokens) == 1 and chain_hit):
                score += 35 * len(chain_hit)
                direct_value_or_topic_match = True
            else:
                for ht in hint_tokens:
                    if len(ht) >= 4 and any(
                        ct.startswith(ht) or ht.startswith(ct)
                        for ct in candidate_chain_tokens
                        if len(ct) >= 4
                    ):
                        score += 30
                        direct_value_or_topic_match = True
                        break

    # Safety invariant for [DO NOT] negative constraints: never supersede an active
    # negative constraint via weak/soft overlap alone — require an explicit retraction,
    # explicit permission-lifting phrase, or direct old-value/topic match.
    if (
        candidate.is_negative
        and not signal.is_explicit_retraction
        and not signal.lifts_negation
        and not direct_value_or_topic_match
    ):
        return 0

    # 3. General content-token overlap (1 word = 1 canonical stemmed token).
    # Require >= 2 distinct shared content tokens (or 1 highly specific >= 9-char domain
    # term on a hard override without explicit targets, e.g., "indentation").
    own_overlap = override_tokens & candidate_own_tokens
    chain_overlap = override_tokens & candidate_chain_tokens
    has_explicit_target = bool(signal.old_values or signal.topic_hints)

    if len(own_overlap) >= 2:
        score += 25 * len(own_overlap)
    elif len(own_overlap) == 1 and not has_explicit_target and signal.kind == "hard":
        tok = next(iter(own_overlap))
        if len(tok) >= 9:
            score += 25
    elif len(chain_overlap) >= 2:
        score += 18 * len(chain_overlap)
    elif len(chain_overlap) == 1 and not has_explicit_target and signal.kind == "hard":
        tok = next(iter(chain_overlap))
        if len(tok) >= 9:
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
# Long-form turn segmentation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _PinUnit:
    """One independently pinnable piece of a user turn."""

    text: str
    # True for blocks cut from a multi-block / long message (briefs, rule lists). Such units
    # never supersede a sibling from the same message and only count as overrides when they
    # retire a concrete earlier pin.
    longform: bool = False
    force_directive: bool = False
    force_negative: bool = False


_SIMPLE_NEGATION_RE = re.compile(r"\b(?:do not|don't|never)\b", re.IGNORECASE)
# Words that may sit directly before an imperative "never" / "don't"
_IMPERATIVE_NEGATION_LEADS: frozenset[str] = frozenset(
    {
        "please", "and", "but", "so", "then", "also", "or", "you", "we", "i", "must",
        "should", "shall", "will", "can", "could", "would", "may", "might", "to", "always",
        "just", "that", "lets", "let's", "kindly", "absolutely", "definitely", "really",
        "simply", "strictly", "still", "now", "again", "otherwise", "instead",
    }
)  # fmt: skip


def _scrub_descriptive_negations(text: str) -> str:
    """Blank out "never" / "don't" used to describe behaviour rather than to forbid it.

    In a long brief, "count-only runs never falsely resolve" or "probe commands don't emit
    failures" report what code does. Only a negation in imperative position (sentence or
    clause start, or after a modal / pronoun) is treated as a rule.
    """
    matches = _bounded_finditer(_SIMPLE_NEGATION_RE, text)
    if not matches:
        return text
    chars = list(text)
    for match in matches:
        before = text[max(0, match.start() - 40) : match.start()].rstrip()
        if not before or before[-1] in ",;:(.!?-\u2013\u2014*_`":
            continue
        prev_word = before.rsplit(" ", 1)[-1].lower().strip("*_`\"'([")
        if prev_word in _IMPERATIVE_NEGATION_LEADS:
            continue
        for idx in range(match.start(), match.end()):
            chars[idx] = " "
    return "".join(chars)


def _sentence_is_directive(sentence: str) -> bool:
    return is_negative_constraint(sentence) or _has_directive_sentence([sentence])


def _is_pure_retraction_sentence(sentence: str) -> bool:
    sig = _detect_override_signal(sentence)
    return bool(
        sig is not None
        and sig.is_explicit_retraction
        and not sig.adopted_targets
        and not is_negative_constraint(sentence)
    )


def _group_directive_sentences(sentences: Sequence[str]) -> list[str]:
    """Group sentences so each group holds one directive (or retraction+replacement) + context."""
    n = len(sentences)
    if n == 0:
        return []

    is_retraction = [_is_pure_retraction_sentence(s) for s in sentences]
    is_directive = [
        False if is_retraction[i] else _sentence_is_directive(sentences[i]) for i in range(n)
    ]

    # O(N) suffix flag: True if a non-retraction directive appears strictly after index i.
    has_following_directive = [False] * n
    seen_directive = False
    for i in range(n - 1, -1, -1):
        has_following_directive[i] = seen_directive
        if is_directive[i]:
            seen_directive = True

    groups: list[list[str]] = []
    lead: list[str] = []
    for i, sent in enumerate(sentences):
        if is_retraction[i] and has_following_directive[i]:
            lead.append(sent)
        elif is_directive[i] or is_retraction[i]:
            groups.append([*lead, sent])
            lead = []
        elif lead:
            lead.append(sent)
        elif groups:
            groups[-1].append(sent)
        else:
            lead.append(sent)
    if lead:
        if groups:
            groups[-1].extend(lead)
        else:
            groups.append(lead)
    return [" ".join(group) for group in groups]


def _markdown_blocks(prose: str) -> list[tuple[str, str]]:
    """Split fence-free prose into ``(kind, text)`` blocks in O(N).

    ``kind`` is ``"heading"``, ``"item"`` (list entry), or ``"para"``. Hard-wrapped lines of
    one paragraph or list item are joined with single spaces; list and heading markers are
    stripped so pin text stays the user's own words.
    """
    blocks: list[tuple[str, str]] = []
    current_kind = ""
    current: list[str] = []

    def _flush() -> None:
        nonlocal current_kind, current
        if current:
            blocks.append((current_kind, " ".join(current)))
        current_kind = ""
        current = []

    for raw_line in prose.splitlines():
        line = " ".join(raw_line.split())
        if not line:
            _flush()
            continue
        heading = _HEADING_MARKER_RE.match(raw_line)
        if heading:
            _flush()
            text = " ".join(raw_line[heading.end() :].split()).strip("# ")
            if text:
                blocks.append(("heading", text))
            continue
        marker = _LIST_MARKER_RE.match(raw_line)
        if marker:
            _flush()
            text = " ".join(raw_line[marker.end() :].split())
            if text:
                current_kind = "item"
                current = [text]
            continue
        if not current:
            current_kind = "para"
        current.append(line)
    _flush()
    return blocks


def _split_turn_units(text: str) -> list[_PinUnit]:
    """Split one user turn into independently pinnable units.

    A short single-block message stays whole unless it states two or more separate
    directives. Multi-block messages (task briefs, rule lists) and long paragraphs are cut
    into one unit per block or directive sentence, so a later correction retires only the
    rule it targets instead of every rule in the message.
    """
    prose = strip_fenced_code_blocks(text)
    blocks = _markdown_blocks(prose)
    if not blocks:
        return [_PinUnit(text=text)]

    if len(blocks) == 1 and len(blocks[0][1]) <= _LONGFORM_PARAGRAPH_CHARS:
        if "```" in text or "~~~" in text:
            return [_PinUnit(text=text)]
        groups = _group_directive_sentences(_split_sentences(prose))
        if len(groups) < 2:
            return [_PinUnit(text=text)]
        return [_PinUnit(text=group) for group in groups]

    units: list[_PinUnit] = []
    leadin = ""
    leadin_rules = False
    leadin_negative = False
    for block_idx, (kind, block) in enumerate(blocks):
        if len(units) >= _MAX_SEGMENTS_PER_TURN:
            break
        next_is_item = block_idx + 1 < len(blocks) and blocks[block_idx + 1][0] == "item"
        if kind == "item" and leadin:
            item_text = f"{leadin}: {block}" if leadin_negative else block
            units.append(
                _PinUnit(
                    text=item_text,
                    longform=True,
                    force_directive=leadin_rules or leadin_negative,
                    force_negative=leadin_negative,
                )
            )
            continue

        stripped = block.rstrip()
        is_leadin = kind == "heading" or stripped.endswith(":")
        if is_leadin:
            leadin = stripped.rstrip(":").strip().strip("*_ ").rstrip(":").strip()
            leadin_negative = bool(_NEGATIVE_LEADIN_RE.match(leadin))
            leadin_rules = bool(_RULES_LEADIN_RE.search(leadin)) and len(leadin) <= 80
            # A heading or "…:" line that introduces a list is context for its items,
            # not a rule of its own.
            # A heading that is itself a rule ("# Never push to main") is kept.
            if (
                next_is_item
                or (leadin_negative and stripped.endswith(":"))
                or (kind == "heading" and not _sentence_is_directive(block))
            ):
                continue
        elif kind == "para":
            leadin = ""
            leadin_rules = leadin_negative = False

        if len(block) > _LONGFORM_PARAGRAPH_CHARS:
            for sent in _split_sentences(block):
                if len(units) >= _MAX_SEGMENTS_PER_TURN:
                    break
                if _sentence_is_directive(sent) or _detect_override_signal(sent) is not None:
                    units.append(_PinUnit(text=sent, longform=True))
        else:
            units.append(_PinUnit(text=block, longform=True))
    return units


# ---------------------------------------------------------------------------
# Main extraction pipeline (strictly O(N) via capped inverted token index)
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
    # pin_id -> (order_idx, own_tokens, chain_tokens, norm_lower, has_date, prohibited_targets)
    pin_meta: dict[
        str,
        tuple[
            int,
            frozenset[str],
            frozenset[str],
            str,
            bool,
            tuple[_ProhibitedTarget, ...],
        ],
    ] = {}
    # Inverted index: canonical token -> active pins containing that token (newest at tail)
    token_index: dict[str, list[PinEntry]] = {}
    # Prefix bucket (first 4 chars) for abbreviation topic hints ("auth" <-> "authentication")
    prefix4_index: dict[str, list[PinEntry]] = {}
    # Active pins that contain a calendar date value
    date_pins: list[PinEntry] = []
    # Track pins that were bare unscoped retractions so consecutive "Scratch that." never chain
    bare_retraction_ids: set[str] = set()
    # Track extra negative pins retired by an override so retracting the override reactivates them
    extra_retired_by: dict[str, list[PinEntry]] = {}

    def _index_pin(
        pin: PinEntry,
        own_tokens: frozenset[str],
        chain_tokens: frozenset[str],
        norm_lower: str,
        has_date: bool,
    ) -> None:
        order_idx = len(result)
        result.append(pin)
        prohibited = tuple(_extract_prohibited_targets(pin.text)) if pin.is_negative else ()
        pin_meta[pin.pin_id] = (
            order_idx,
            own_tokens,
            chain_tokens,
            norm_lower,
            has_date,
            prohibited,
        )
        for tok in chain_tokens:
            token_index.setdefault(tok, []).append(pin)
            if len(tok) >= 4:
                prefix4_index.setdefault(tok[:4], []).append(pin)
        if has_date:
            date_pins.append(pin)

    # 1. Copy & deduplicate manual pins (never mutate caller's PinEntry objects)
    seen_pin_ids: set[str] = set()
    manual_by_norm: dict[str, PinEntry] = {}
    active_by_norm: dict[str, PinEntry] = {}

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
        seen_pin_ids.add(copied.pin_id)
        manual_by_norm[norm] = copied
        active_by_norm[norm] = copied
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

    # Track the pin produced by the immediately preceding non-chatter user turn so
    # unscoped retractions ("Scratch that.", "Never mind.") never reach past unrelated turns.
    prev_non_chatter_pin: PinEntry | None = None

    work: list[tuple[UserTurn, _PinUnit]] = []
    for turn in ordered_turns:
        turn_text = sanitize_unicode(turn.text).strip()
        if not normalize_pin_text(turn_text) or is_chatter(turn_text):
            continue
        work.extend((turn, unit) for unit in _split_turn_units(turn_text))

    for turn, unit in work:
        clean_text = unit.text.strip()
        norm = normalize_pin_text(clean_text)
        if not norm:
            continue
        if is_chatter(clean_text):
            continue

        signal = _detect_override_signal(clean_text)
        if unit.longform:
            neg = unit.force_negative or is_negative_constraint(
                _scrub_descriptive_negations(clean_text)
            )
        else:
            neg = is_negative_constraint(clean_text)
        if unit.longform:
            # Blocks of a long brief must stand on their own as a rule; an incidental
            # "actually" / "switch to" inside descriptive prose is not enough.
            base_directive = (
                neg
                or unit.force_directive
                or _has_directive_sentence(_split_sentences(_normalize_prose(clean_text)))
            )
            if not base_directive and signal is None:
                continue
        else:
            base_directive = True
            directive = neg or signal is not None or is_constraint_or_directive(clean_text)
            if not directive:
                prev_non_chatter_pin = None
                continue

        own_tokens = _extract_content_tokens(clean_text)
        norm_lower = " ".join(_normalize_prose(clean_text).lower().split())
        override_has_date = bool(_DATE_VALUE_RE.search(norm_lower))
        superseded_target: PinEntry | None = None
        primary_was_contradicted_ban = False
        contradicted_negatives: list[PinEntry] = []
        adopted_specs: list[tuple[str, frozenset[str], frozenset[str], frozenset[str]]] = []
        all_old_val_tokens: frozenset[str] = frozenset()

        if signal is not None:
            # Precompute normalized phrases and tokens ONCE per override turn (not per candidate)
            old_val_specs = [
                (_clean_phrase(v).lower(), _extract_content_tokens(v)) for v in signal.old_values
            ]
            hint_specs = [
                (_clean_phrase(h).lower(), _extract_content_tokens(h)) for h in signal.topic_hints
            ]
            for target in signal.adopted_targets:
                idents, nums = _extract_target_tokens(target.phrase)
                if idents:
                    adopted_specs.append((target.kind, idents, nums, target.scope_tokens))
            all_old_val_tokens = frozenset(tok for _, toks in old_val_specs for tok in toks)

            # Gather up to _MAX_BUCKET_SCAN newest active candidates per token (strictly O(1))
            query_tokens: set[str] = set(own_tokens)
            for _, old_toks in old_val_specs:
                query_tokens.update(old_toks)
            for _, hint_toks in hint_specs:
                query_tokens.update(hint_toks)
            for target in signal.adopted_targets:
                query_tokens.update(_extract_content_tokens(target.phrase))

            candidate_map: dict[str, PinEntry] = {}

            def _collect_recent(bucket: list[PinEntry], out: dict[str, PinEntry]) -> None:
                found = 0
                for p in reversed(bucket):
                    if p.superseded_by is None:
                        out[p.pin_id] = p
                        found += 1
                        if found >= _MAX_BUCKET_SCAN:
                            break
                if len(bucket) > _MAX_BUCKET_SCAN * 4 and found < _MAX_BUCKET_SCAN:
                    bucket[:] = [p for p in bucket if p.superseded_by is None]

            for tok in query_tokens:
                bucket = token_index.get(tok)
                if bucket:
                    _collect_recent(bucket, candidate_map)

            for _, hint_toks in hint_specs:
                for ht in hint_toks:
                    if len(ht) >= 4 and ht not in token_index:
                        pbucket = prefix4_index.get(ht[:4])
                        if pbucket:
                            _collect_recent(pbucket, candidate_map)

            if override_has_date and not signal.old_values and not signal.topic_hints and date_pins:
                _collect_recent(date_pins, candidate_map)

            eligible: list[PinEntry] = []
            for cand in candidate_map.values():
                if cand.source == "manual" and (
                    cand.turn_index is None or cand.turn_index >= turn.turn_index
                ):
                    continue
                if cand.pin_id in bare_retraction_ids:
                    continue
                if unit.longform and cand.source != "manual" and cand.turn_index == turn.turn_index:
                    continue
                eligible.append(cand)
            eligible.sort(key=lambda p: pin_meta[p.pin_id][0], reverse=True)

            best_score = 0
            best_candidate: PinEntry | None = None
            for cand in eligible:
                _, cand_own, cand_chain, cand_norm_lower, cand_has_date, _ = pin_meta[cand.pin_id]
                score = _score_supersede_candidate(
                    signal=signal,
                    override_tokens=own_tokens,
                    old_val_specs=old_val_specs,
                    hint_specs=hint_specs,
                    candidate=cand,
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
                and prev_non_chatter_pin.pin_id not in bare_retraction_ids
                and not (unit.longform and prev_non_chatter_pin.turn_index == turn.turn_index)
            ):
                superseded_target = prev_non_chatter_pin

            # Also find earlier active negative constraints directly contradicted by the
            # target adopted in this override (e.g. "Never use spaces." retired when
            # "Actually use 2 spaces instead of tabs." supersedes "Use tabs.").
            if adopted_specs:
                for cand in eligible:
                    if cand is superseded_target or not cand.is_negative or cand.source == "manual":
                        continue
                    if cand.turn_index is not None and cand.turn_index >= turn.turn_index:
                        continue
                    _, cand_own, _, _, _, cand_prohibited = pin_meta[cand.pin_id]
                    if _contradicts_negative_pin(
                        adopted_specs,
                        own_tokens,
                        all_old_val_tokens,
                        bool(signal.old_values),
                        cand,
                        cand_own,
                        cand_prohibited,
                    ):
                        contradicted_negatives.append(cand)
                if superseded_target is None and contradicted_negatives:
                    superseded_target = contradicted_negatives.pop(0)
                    primary_was_contradicted_ban = True

        if (
            unit.longform
            and superseded_target is None
            and not base_directive
            and not (signal is not None and signal.kind == "hard")
        ):
            continue

        # Consecutive bare unscoped retraction ("Scratch that." twice in a row) is a no-op
        is_bare_retraction = bool(
            signal is not None and signal.is_unscoped_retraction and not own_tokens
        )
        if is_bare_retraction and superseded_target is None:
            continue

        # Deduplicate repeated identical/whitespace-equivalent active instructions,
        # UNLESS this turn supersedes a distinct active pin (e.g., repeated "Scratch that.").
        # Keep prev_non_chatter_pin pointing to the existing active pin so a following
        # "Scratch that." still retracts it (fixes B3 repeat-then-scratch edge case).
        if superseded_target is None and norm in active_by_norm:
            prev_non_chatter_pin = active_by_norm[norm]
            continue

        source = (
            "override"
            if (
                superseded_target is not None
                or (signal is not None and signal.kind == "hard" and not unit.longform)
            )
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
            if active_by_norm.get(old_norm) is superseded_target:
                active_by_norm.pop(old_norm, None)
            _, _, prev_chain, _, prev_has_date, _ = pin_meta[superseded_target.pin_id]
            # Cap inherited chain tokens to _MAX_CHAIN_TOKENS so long chains stay O(1)/step
            merged = set(own_tokens)
            if len(merged) < _MAX_CHAIN_TOKENS:
                remaining_slots = _MAX_CHAIN_TOKENS - len(merged)
                for pt in sorted(prev_chain):
                    if pt not in merged:
                        merged.add(pt)
                        remaining_slots -= 1
                        if remaining_slots <= 0:
                            break
            chain_tokens = frozenset(merged)
            override_has_date = override_has_date or prev_has_date

            # If the superseded override had retired contradictory negative constraints
            # (either as extra bans or as its promoted primary target), either keep them
            # retired (if this newer override still contradicts them) or reactivate them
            # (e.g. on "Scratch that." or switching back to the old choice).
            prev_extra_bans = extra_retired_by.pop(superseded_target.pin_id, [])
            for ban in prev_extra_bans:
                _, ban_own, _, _, _, ban_prohibited = pin_meta[ban.pin_id]
                still_contradicted = (
                    signal is not None
                    and not signal.is_explicit_retraction
                    and _contradicts_negative_pin(
                        adopted_specs,
                        own_tokens,
                        all_old_val_tokens,
                        bool(signal.old_values),
                        ban,
                        ban_own,
                        ban_prohibited,
                    )
                )
                if still_contradicted:
                    ban.superseded_by = new_pin.pin_id
                    extra_retired_by.setdefault(new_pin.pin_id, []).append(ban)
                else:
                    ban.superseded_by = None
                    if superseded_target.supersedes == ban.pin_id:
                        superseded_target.supersedes = None
                    ban_norm = normalize_pin_text(ban.text)
                    if ban_norm:
                        active_by_norm[ban_norm] = ban
                    _, _, ban_chain, _, ban_has_date, _ = pin_meta[ban.pin_id]
                    for tok in ban_chain:
                        bucket = token_index.setdefault(tok, [])
                        if ban not in bucket:
                            bucket.append(ban)
                        if len(tok) >= 4:
                            pbucket = prefix4_index.setdefault(tok[:4], [])
                            if ban not in pbucket:
                                pbucket.append(ban)
                    if ban_has_date and ban not in date_pins:
                        date_pins.append(ban)

        if primary_was_contradicted_ban and superseded_target is not None:
            extra_retired_by.setdefault(new_pin.pin_id, []).append(superseded_target)

        for extra_neg in contradicted_negatives:
            extra_neg.superseded_by = new_pin.pin_id
            extra_retired_by.setdefault(new_pin.pin_id, []).append(extra_neg)
            neg_norm = normalize_pin_text(extra_neg.text)
            if active_by_norm.get(neg_norm) is extra_neg:
                active_by_norm.pop(neg_norm, None)

        seen_pin_ids.add(new_pin.pin_id)
        active_by_norm[norm] = new_pin
        if is_bare_retraction:
            bare_retraction_ids.add(new_pin.pin_id)
        _index_pin(new_pin, own_tokens, chain_tokens, norm_lower, override_has_date)
        prev_non_chatter_pin = new_pin

    return result
