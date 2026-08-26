"""
Core data models for the book translation workflow.

This module defines the Pydantic models that represent all data structures
used throughout the translation pipeline.
"""

import math
import unicodedata
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator, computed_field


class IssueLevel(str, Enum):
    """Severity levels for evaluation issues."""
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class AnnotationType(str, Enum):
    """Types of annotations for review mode."""
    # New workflow-specific categories
    USAGE_DOUBT = "usage_doubt"
    TRANSLATION_DOUBT = "translation_doubt"
    PROBLEM = "problem"
    OTHER = "other"
    FOOTNOTE = "footnote"
    # Legacy categories (kept for backward compatibility)
    NOTE = "note"
    ISSUE = "issue"
    TERMINOLOGY = "terminology"
    QUESTION = "question"


class Issue(BaseModel):
    """
    An individual problem found during evaluation.

    Example:
        Issue(
            severity=IssueLevel.ERROR,
            message="Translation is 50% shorter than expected",
            location="chunk_01",
            suggestion="Check for missing paragraphs"
        )

    ``rule_id`` / ``category`` carry the checker's own identifier for the rule
    that fired, when it has one (today: LanguageTool, via the grammar
    evaluator). They exist so a rule can be suppressed or have its precision
    tracked by a stable key — the human-readable ``message`` is localized
    Spanish and changes with the LanguageTool version, so it is not one. Both
    are optional and default to None: evaluators without a rule concept simply
    omit them, and evaluations persisted before these fields existed still
    parse.

    ``term`` is the surface form the finding is *about*, when the finding is
    about one — the flagged word for the dictionary evaluator, the flagged token
    for a grammar match. It exists for the same reason: it was previously
    recoverable only by regex-parsing the quoted word back out of ``message``,
    which makes it unusable as a key. Same optional/additive contract.
    """
    severity: IssueLevel
    message: str
    location: Optional[str] = None
    suggestion: Optional[str] = None
    rule_id: Optional[str] = None
    category: Optional[str] = None
    term: Optional[str] = None


class Annotation(BaseModel):
    """
    Word-level annotation for review notes.

    Uses word-based positioning (word index) instead of character offset
    to remain stable when text is edited.

    Example:
        Annotation(
            id="ann_1234567890",
            word_index=42,
            word_text="magia",
            annotation_type=AnnotationType.TRANSLATION_DOUBT,
            content="Check if this should be 'hechicería' instead",
            tags=["verify", "terminology"]
        )
    """
    id: str = Field(description="Unique annotation ID")
    word_index: int = Field(ge=0, description="Zero-based word position in translated_text")
    word_text: str = Field(description="The annotated word (for reference)")
    annotation_type: AnnotationType
    content: Optional[str] = Field(default=None, description="Optional note/comment text")
    tags: list[str] = Field(default_factory=list, description="Tags for categorization")
    context_before: list[str] = Field(default_factory=list, description="Up to 2 words before the annotated word, for relocation")
    context_after: list[str] = Field(default_factory=list, description="Up to 2 words after the annotated word, for relocation")
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: Optional[datetime] = None


class ChunkReviewData(BaseModel):
    """
    Review mode data stored in chunk JSON.

    Contains annotations and evaluation cache for the review workflow.

    Example:
        ChunkReviewData(
            annotations=[annotation1, annotation2],
            last_evaluated_at=datetime.now()
        )
    """
    annotations: list[Annotation] = Field(default_factory=list, description="Word-level annotations")
    last_evaluated_at: Optional[datetime] = Field(default=None, description="When last evaluation was run")


class ChunkMetadata(BaseModel):
    """Metadata about a chunk's position and characteristics."""
    char_start: int = Field(ge=0, description="Character position in original chapter")
    char_end: int = Field(ge=0, description="Character position end in original chapter")
    overlap_start: int = Field(ge=0, description="Characters of overlap with previous chunk")
    overlap_end: int = Field(ge=0, description="Characters of overlap with next chunk")
    paragraph_count: int = Field(ge=0, description="Number of paragraphs in chunk")
    word_count: int = Field(ge=0, description="Number of words in chunk")

    @field_validator('char_end')
    @classmethod
    def end_after_start(cls, v: int, info) -> int:
        """Ensure char_end is after char_start."""
        if 'char_start' in info.data and v < info.data['char_start']:
            raise ValueError('char_end must be >= char_start')
        return v


class ChunkStatus(str, Enum):
    """Status of a chunk in the translation pipeline."""
    PENDING = "pending"
    TRANSLATED = "translated"
    IN_REVIEW = "in_review"  # Has annotations to address
    VALIDATED = "validated"
    FAILED = "failed"


class Chunk(BaseModel):
    """
    A segment of text for translation with metadata.

    Chunks are created by dividing chapters into manageable pieces
    with overlapping content for context preservation.

    Example:
        Chunk(
            id="ch01_chunk_003",
            chapter_id="chapter_01",
            position=3,
            source_text="The sun rose over...",
            translated_text="El sol salió sobre...",
            metadata=ChunkMetadata(...),
            status=ChunkStatus.TRANSLATED
        )
    """
    model_config = {"extra": "ignore"}  # Allow extra fields for backward compatibility

    id: str = Field(description="Unique identifier (e.g., 'ch01_chunk_003')")
    chapter_id: str = Field(description="Parent chapter identifier")
    position: int = Field(ge=0, description="Sequence number in chapter")
    source_text: str = Field(min_length=1, description="Original English text")
    translated_text: Optional[str] = Field(default=None, description="Spanish translation")
    metadata: ChunkMetadata
    status: ChunkStatus = ChunkStatus.PENDING
    created_at: datetime = Field(default_factory=datetime.now)
    translated_at: Optional[datetime] = None
    prompt_metadata: Optional["PromptMetadata"] = Field(default=None, description="Prompt version tracking")
    review_data: Optional[ChunkReviewData] = Field(
        default=None,
        description="Review mode data (annotations, eval cache)"
    )
    last_llm_log: Optional[str] = Field(
        default=None,
        description="Relative path under prompts/history pointing at the LLM call that produced this translated_text. Set at LLM-write time, preserved across user edits.",
    )

    @computed_field
    @property
    def word_count(self) -> int:
        """Count words in source text."""
        return len(self.source_text.split())

    @computed_field
    @property
    def has_translation(self) -> bool:
        """Check if chunk has been translated."""
        return self.translated_text is not None and len(self.translated_text.strip()) > 0

    @computed_field
    @property
    def translation_word_count(self) -> int:
        """Count words in translated text."""
        if not self.translated_text:
            return 0
        return len(self.translated_text.split())

    @computed_field
    @property
    def display_status(self) -> str:
        """
        Determine display status based on translation and annotations.

        Returns:
            - "pending": No translation yet
            - "in_review": Has translation and active annotations
            - "translated": Has translation, no annotations (complete)
        """
        if not self.has_translation:
            return "pending"

        annotation_count = 0
        if self.review_data and self.review_data.annotations:
            annotation_count = sum(
                1 for a in self.review_data.annotations
                if a.annotation_type != AnnotationType.FOOTNOTE
            )

        if annotation_count > 0:
            return "in_review"
        else:
            return "translated"

    @computed_field
    @property
    def annotation_count(self) -> int:
        """Count of active annotations on this chunk."""
        if not self.review_data or not self.review_data.annotations:
            return 0
        return sum(
            1 for a in self.review_data.annotations
            if a.annotation_type != AnnotationType.FOOTNOTE
        )


class EvalResult(BaseModel):
    """
    Results from running an evaluator on a chunk or chapter.

    Example:
        EvalResult(
            eval_name="length_check",
            eval_version="1.0.0",
            target_id="ch01_chunk_003",
            target_type="chunk",
            passed=True,
            score=0.95,
            issues=[],
            metadata={"ratio": 1.15}
        )
    """
    eval_name: str = Field(description="Name of the evaluator")
    eval_version: str = Field(description="Version of the evaluator")
    target_id: str = Field(description="ID of chunk or chapter evaluated")
    target_type: str = Field(description="'chunk' or 'chapter'")
    passed: bool = Field(description="Overall pass/fail status")
    score: Optional[float] = Field(default=None, ge=0.0, le=1.0, description="Score 0.0-1.0")
    issues: list[Issue] = Field(default_factory=list, description="Problems found")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Evaluator-specific data")
    executed_at: datetime = Field(default_factory=datetime.now)

    @computed_field
    @property
    def error_count(self) -> int:
        """Count of error-level issues."""
        return sum(1 for issue in self.issues if issue.severity == IssueLevel.ERROR)

    @computed_field
    @property
    def warning_count(self) -> int:
        """Count of warning-level issues."""
        return sum(1 for issue in self.issues if issue.severity == IssueLevel.WARNING)

    @computed_field
    @property
    def info_count(self) -> int:
        """Count of info-level issues."""
        return sum(1 for issue in self.issues if issue.severity == IssueLevel.INFO)


class GlossaryTermType(str, Enum):
    """Types of glossary terms."""
    CHARACTER = "character"
    PLACE = "place"
    CONCEPT = "concept"
    TECHNICAL = "technical"
    OTHER = "other"


class GlossaryTerm(BaseModel):
    """
    A term that should be consistently translated.

    Example:
        GlossaryTerm(
            english="magic",
            spanish="magia",
            type=GlossaryTermType.CONCEPT,
            context="Use 'magia' not 'hechicería' in this book",
            alternatives=["hechicería"]
        )
    """
    english: str = Field(min_length=1, description="English term")
    spanish: str = Field(min_length=1, description="Spanish translation")
    type: GlossaryTermType = GlossaryTermType.OTHER
    context: Optional[str] = Field(default=None, description="Usage notes")
    alternatives: list[str] = Field(default_factory=list, description="Other valid translations")


class Glossary(BaseModel):
    """
    Collection of terms for consistent translation.

    Example:
        Glossary(
            terms=[
                GlossaryTerm(english="Harry", spanish="Harry"),
                GlossaryTerm(english="Hogwarts", spanish="Hogwarts")
            ],
            version="1.0"
        )
    """
    terms: list[GlossaryTerm] = Field(default_factory=list)
    version: str = "1.0"
    updated_at: datetime = Field(default_factory=datetime.now)

    def find_term(self, english: str) -> Optional[GlossaryTerm]:
        """Find a term by its English value (case-insensitive)."""
        english_lower = english.lower()
        for term in self.terms:
            if term.english.lower() == english_lower:
                return term
        return None

    def find_term_by_spanish(self, spanish: str) -> Optional[GlossaryTerm]:
        """Find a term by its Spanish value or alternatives (case-insensitive)."""
        spanish_lower = spanish.lower()
        for term in self.terms:
            # Check primary Spanish translation
            if term.spanish.lower() == spanish_lower:
                return term
            # Check alternatives
            for alternative in term.alternatives:
                if alternative.lower() == spanish_lower:
                    return term
        return None

    def matches_word(self, word: str) -> bool:
        """
        Check if a word corresponds to any glossary term.

        Matching is case- and accent-insensitive, supports multi-word term
        tokens, and tolerates Spanish plurals by expanding each glossary term
        into its plural form (vowel-final -> +s, consonant-final -> +es) rather
        than stripping the word under test. Proper nouns already ending in 's'
        (Atlas, Pericles) are not pluralized, so they don't match a genuinely
        different word (atlases).
        """
        if not word:
            return False

        def _fold(s: str) -> str:
            nfd = unicodedata.normalize("NFD", s)
            return "".join(c for c in nfd if unicodedata.category(c) != "Mn").lower()

        def _plural_variants(candidate: str) -> set[str]:
            """Folded candidate plus its Spanish plural form(s).

            Shares the core strategy of ``_term_pattern`` in
            ``utils/glossary_context.py``: generate plurals by appending to the
            (curated) term rather than stripping the word under test, using the
            Spanish rule (vowel-final -> +s, consonant-final -> +es) and skipping
            proper nouns already ending in 's' (Atlas, Pericles) so they don't
            match a genuinely different word. It intentionally diverges in that
            the caller also applies this per-token to multi-word terms, and here
            a ``len(folded) <= 3`` guard keeps short terms/articles exact.
            """
            folded = _fold(candidate)
            variants = {folded}
            if len(folded) <= 3:  # keep articles la/el/los/una exact
                return variants
            if folded.endswith("s") and candidate[:1].isupper():
                return variants  # Atlas, Pericles: no plural suffix
            variants.add(folded + ("s" if folded[-1] in "aeiou" else "es"))
            return variants

        word_folded = _fold(word)

        for term in self.terms:
            candidates = [term.spanish, term.english, *term.alternatives]
            for candidate in candidates:
                if not candidate:
                    continue
                if word_folded in _plural_variants(candidate):
                    return True
                for token in candidate.split():
                    if word_folded in _plural_variants(token):
                        return True
        return False

    def get_translation(self, english: str) -> Optional[str]:
        """Get the Spanish translation for an English term."""
        term = self.find_term(english)
        return term.spanish if term else None


class BlacklistEntry(BaseModel):
    """
    A forbidden word or phrase that should not appear in translations.

    Supports explicit variations list for handling conjugations, plurals, etc.
    Each entry can have its own severity level (error, warning, info) to allow
    flexible enforcement of translation preferences.

    Example:
        BlacklistEntry(
            term="coger",
            variations=["coger", "coge", "cogió", "cogería", "coja"],
            reason="Offensive in Latin America - use 'tomar' or 'agarrar'",
            severity="warning",
            whole_word=True,
            alternatives=["tomar", "agarrar"]
        )
    """
    term: str = Field(min_length=1, description="Base forbidden term (used in issue messages)")
    variations: list[str] = Field(default_factory=list, description="All forms to match (conjugations, plurals)")
    reason: str = Field(min_length=1, description="Why this term is forbidden")
    severity: str = Field(default="error", description="Issue severity: error, warning, or info")
    case_sensitive: bool = Field(default=False, description="Whether matching is case-sensitive")
    whole_word: bool = Field(default=True, description="Require word boundaries (avoid partial matches)")
    alternatives: list[str] = Field(default_factory=list, description="Suggested replacement terms")

    @field_validator('severity')
    @classmethod
    def validate_severity(cls, v: str) -> str:
        """Ensure severity is one of the valid values."""
        valid = ['error', 'warning', 'info']
        if v.lower() not in valid:
            raise ValueError(f"Severity must be one of {valid}, got '{v}'")
        return v.lower()


class Blacklist(BaseModel):
    """
    Collection of forbidden terms for translation quality control.

    Example:
        Blacklist(
            entries=[
                BlacklistEntry(term="zumo", variations=["zumo", "zumos"],
                              reason="Use 'jugo' for Latin American Spanish"),
                BlacklistEntry(term="coger", variations=["coger", "coge", "cogió"],
                              reason="Offensive in Latin America", severity="warning")
            ],
            version="1.0"
        )
    """
    entries: list[BlacklistEntry] = Field(default_factory=list)
    version: str = "1.0"


class IgnoredTerm(BaseModel):
    """One finding the reviewer has decided is noise for a whole book.

    Written from the reader, cleared from the desktop dashboard. Deliberately
    NOT a glossary entry: the glossary is a translation contract for the
    recurring cast (median 14 occurrences per book), while these are the hapax
    tail (median 1) -- proper nouns, Latin, French place names that the
    dictionary cannot know and that there is nothing to be *consistent* about.

    ``rule_id`` is required for ``eval_name="grammar"`` and unused for
    ``"dictionary"``. A grammar finding is about a rule firing on a word, not
    about the word: the words reviewers actually ignore there are function words
    (``el`` occurs 1412x in one book), so a word-only key would silence every
    present and future rule on that token. Keying on the pair bounds it to the
    one rule -- and on the marked corpus it cost half as many real defects for
    identical benefit.
    """
    term: str
    eval_name: str
    rule_id: Optional[str] = None
    added_at: Optional[datetime] = None
    added_from: Optional[str] = None
    note: Optional[str] = None

    def identity(self) -> tuple[str, str, Optional[str]]:
        """Dedup/removal key: evaluator, case-folded term, rule.

        The rule slot is populated for ``grammar`` only, mirroring
        :meth:`IgnoredTerms.matches`. Keying on ``rule_id`` unconditionally
        would let a non-grammar entry that carries one -- from a hand-edited
        file, a restored backup, or a future client -- suppress findings via
        ``matches`` while being unreachable by the removal route, which nulls
        the rule slot for everything but grammar. That entry could then never
        be cleared through the UI.
        """
        rule = self.rule_id or None if self.eval_name == "grammar" else None
        return (self.eval_name, self.term.strip().casefold(), rule)


class IgnoredTerms(BaseModel):
    """Per-book ignore list, stored at ``projects/<id>/ignored_terms.json``."""
    version: int = 1
    terms: list[IgnoredTerm] = Field(default_factory=list)

    def matches(
        self,
        eval_name: str,
        term: Optional[str],
        rule_id: Optional[str] = None,
    ) -> bool:
        """Is this finding ignored for this book?

        Case-insensitive, **accent-sensitive**, and with no plural expansion --
        all three deliberately unlike :meth:`Glossary.matches_word`. A glossary
        term is a curated identity, so folding accents and generating plurals
        helps it; an ignore entry means "this exact surface form is fine", so
        the same tolerance would over-suppress. Folding accents would make
        ignoring ``nivea`` also silence ``nivea``-with-a-tilde, and a missing
        tilde is one of the few genuine Spanish typos the dictionary evaluator
        actually catches. Measured on the marked corpus, the tolerant matcher's
        two over-suppression modes are accent folding (an unaccented entry
        silencing its accented neighbour, which is usually the real typo) and
        plural generation (a singular entry silencing an unrelated longer word),
        and neither buys any additional true suppression on the hapax tail these
        entries live on.

        A grammar finding with no ``rule_id`` is never ignored: without it there
        is no bounded thing to suppress. Evaluations written before rule ids
        were persisted are in that state until the chunk is re-evaluated.
        """
        if not term:
            return False
        if eval_name == "grammar" and not rule_id:
            return False
        folded = term.strip().casefold()
        if not folded:
            return False
        for entry in self.terms:
            if entry.eval_name != eval_name:
                continue
            if entry.term.strip().casefold() != folded:
                continue
            if eval_name == "grammar" and (entry.rule_id or None) != rule_id:
                continue
            return True
        return False


class PromptMetadata(BaseModel):
    """
    Version tracking for prompts used to generate translations.

    Tracks which versions of the template, glossary, and style guide were used
    for translating a particular chunk. This enables traceability and helps
    identify if chunks need re-translation when prompts are updated.

    Example:
        PromptMetadata(
            template_version="1.0",
            glossary_version="2.3",
            style_guide_version="1.1",
            timestamp=datetime.now()
        )
    """
    template_version: str = Field(description="Version of the prompt template used")
    glossary_version: str = Field(description="Version of the glossary used")
    style_guide_version: str = Field(description="Version of the style guide used")
    timestamp: datetime = Field(default_factory=datetime.now, description="When prompt was rendered")


class StyleGuide(BaseModel):
    """
    Style guide for translation with manual version tracking.

    Contains style preferences such as tone, formality level, dialect preferences,
    and special instructions. Version is manually maintained by the user.

    Example:
        StyleGuide(
            content="TONE: Formal but accessible\\nFORMALITY: Medium-high\\nDIALECT: Neutral Spanish",
            version="1.0",
            created_at=datetime.now(),
            updated_at=datetime.now()
        )
    """
    content: str = Field(description="The style guide text")
    light_content: Optional[str] = Field(
        default=None,
        description="Optional shorter style guide used for single-sentence retranslation. Falls back to content when empty.",
    )
    version: str = Field(default="1.0", description="Version (manually maintained by user)")
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)


# ── Forms-of-address (usted/tú) expectations map ────────────────────────────
#
# The per-book expectations the `address` LLM judge checks against. Unlike the
# universal dialogue rules, the *correct* form depends on who addresses whom, the
# relationship, the public/private situation, and can change as the story
# progresses. So each pair models address PER DIRECTION as an ordered list of
# context-conditioned rules (see docs/ADDRESS_JUDGE.md).

# v1 supports tú/usted only (voseo / ustedes-vosotros plural address are out of
# scope). Aliases fold the common accent-dropped draft into the canonical form.
ADDRESS_FORMS: frozenset[str] = frozenset({"tú", "usted"})
_ADDRESS_FORM_ALIASES: dict[str, str] = {"tu": "tú", "tú": "tú", "usted": "usted"}

# The two directions of a pair. ``a_to_b`` is how character ``a`` addresses ``b``.
ADDRESS_DIRECTIONS: tuple[str, ...] = ("a_to_b", "b_to_a")


class AddressRule(BaseModel):
    """One context-conditioned address expectation within a direction.

    Rules are evaluated in list order: the first whose ``when`` condition and
    ``since``/``until`` chapter window match the scene wins, falling back to the
    ``when="default"`` rule that every non-empty direction must carry.

    Example:
        AddressRule(form="usted", when="public", notes="formal deference before others")
    """
    form: str = Field(description="Expected address form: 'tú' or 'usted'")
    when: str = Field(
        default="default",
        description="Scene condition: 'default' | 'public' | 'private' | free-text situation",
    )
    since: Optional[str] = Field(default=None, description="Chapter id this rule takes effect from")
    until: Optional[str] = Field(default=None, description="Chapter id this rule stops applying after")
    after_event: Optional[str] = Field(default=None, description="Story event that triggers this rule")
    notes: Optional[str] = Field(default=None, description="Rationale / usage notes")

    @field_validator("form", mode="before")
    @classmethod
    def _normalize_form(cls, v: Any) -> str:
        key = str(v or "").strip().lower()
        if key not in _ADDRESS_FORM_ALIASES:
            raise ValueError(
                f"Unknown address form {v!r}; expected one of {sorted(ADDRESS_FORMS)} "
                "(v1 supports tú/usted only)."
            )
        return _ADDRESS_FORM_ALIASES[key]

    @field_validator("when", mode="before")
    @classmethod
    def _default_when(cls, v: Any) -> str:
        return str(v or "").strip() or "default"


class AddressPair(BaseModel):
    """Expected address between two characters, modeled per direction.

    ``directions`` maps ``a_to_b`` / ``b_to_a`` to an ordered rule list. The two
    directions are independent, so asymmetric cases (A→B tú while B→A usted in
    public) are expressible.
    """
    a: str = Field(min_length=1, description="First character (canonical/Spanish name)")
    b: str = Field(min_length=1, description="Second character")
    relationship: Optional[str] = Field(default=None, description="How the two relate")
    directions: dict[str, list[AddressRule]] = Field(default_factory=dict)

    @field_validator("directions")
    @classmethod
    def _check_directions(cls, v: dict[str, list["AddressRule"]]) -> dict[str, list["AddressRule"]]:
        for key, rules in v.items():
            if key not in ADDRESS_DIRECTIONS:
                raise ValueError(
                    f"Unknown direction {key!r}; expected one of {list(ADDRESS_DIRECTIONS)}."
                )
            # First-match semantics: specific when-rules first, exactly one
            # when="default" last so every scene resolves.
            if rules:
                default_idxs = [i for i, r in enumerate(rules) if r.when == "default"]
                if len(default_idxs) != 1 or default_idxs[0] != len(rules) - 1:
                    raise ValueError(
                        f"Direction {key!r} must end with exactly one when='default' rule "
                        "(put specific when-rules before it)."
                    )
        return v


class AddressMap(BaseModel):
    """Per-book forms-of-address expectations for the ``address`` judge.

    ``content`` is the human-readable prose the judge actually reads (it must
    state the asymmetric/contextual rules plainly); ``pairs`` / ``global_rules``
    are the structured mirror for future UI / deterministic use.

    ``style_guide_summary`` is a different audience from ``content``: it is the
    condensed version folded into the style guide, and therefore read by a
    translator who sees ONE chunk and does not know which chapter it came from.
    It states the general rules plus only the high-frequency exceptions, and must
    never reference a chapter, a mid-book transition, or the map itself. Optional
    so address maps written before this field remain valid.
    """
    content: str = Field(default="", description="Prose the judge reads")
    style_guide_summary: Optional[str] = Field(
        default=None,
        description="Chunk-local condensation folded into the style guide",
    )
    pairs: list[AddressPair] = Field(default_factory=list)
    global_rules: str = Field(
        default="", description="Fallback rules when no pair matches (seeded from style guide)"
    )
    version: str = "1.0"
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)


class ChunkingMethod(str, Enum):
    """Methods for dividing chapters into chunks."""
    PARAGRAPH = "paragraph"
    SEMANTIC = "semantic"
    FIXED = "fixed"


def derive_chunk_bounds(
    target: int, min_ratio: float = 0.25, max_ratio: float = 1.5
) -> tuple[int, int]:
    """Derive ``(min_chunk_size, max_chunk_size)`` from ``target * ratios``.

    Clamped to the ChunkingConfig pydantic floors (min ≥ 50, max ≥ 100, max >
    min). With the default ratios (0.25 / 1.5) and ``target=2000`` this
    reproduces the historical 500 / 3000 bounds exactly. Pure: it does **not**
    validate the ratios or construct a model (so callers can derive bounds for
    sub-100 targets); :meth:`ChunkingConfig.from_target` layers that validation.
    """
    target = int(target)
    min_chunk = max(50, round(target * min_ratio))
    max_chunk = max(100, round(target * max_ratio))
    if max_chunk <= min_chunk:
        max_chunk = min_chunk + 1
    return min_chunk, max_chunk


class ChunkingConfig(BaseModel):
    """Configuration for chunking chapters."""
    method: ChunkingMethod = ChunkingMethod.PARAGRAPH
    target_size: int = Field(default=2000, ge=100, description="Target words per chunk")
    overlap_paragraphs: int = Field(default=0, ge=0, le=5, description="Minimum paragraphs of overlap")
    min_overlap_words: int = Field(default=0, ge=0, description="Minimum words in overlap")
    min_chunk_size: int = Field(default=500, ge=50, description="Minimum words per chunk")
    max_chunk_size: int = Field(default=3000, ge=100, description="Maximum words per chunk")
    min_ratio: float = Field(default=0.25, gt=0.0, description="min_chunk_size as a fraction of target_size when auto-derived (Advanced)")
    max_ratio: float = Field(default=1.5, gt=0.0, description="max_chunk_size as a multiple of target_size when auto-derived (Advanced)")
    split_quality_weight: float = Field(default=0.5, ge=0.0, le=2.0, description="Weight for split-point quality vs even sizing (0=pure even, higher=prefer good boundaries)")

    @field_validator('max_chunk_size')
    @classmethod
    def max_greater_than_min(cls, v: int, info) -> int:
        """Ensure max_chunk_size > min_chunk_size."""
        if 'min_chunk_size' in info.data and v <= info.data['min_chunk_size']:
            raise ValueError('max_chunk_size must be > min_chunk_size')
        return v

    @classmethod
    def from_target(
        cls,
        target: int,
        *,
        min_ratio: float = 0.25,
        max_ratio: float = 1.5,
        overlap_paragraphs: int = 0,
        min_overlap_words: int = 0,
    ) -> "ChunkingConfig":
        """Build a config whose min/max bounds scale with ``target``.

        ``min_chunk_size``/``max_chunk_size`` are derived as ``target * min_ratio``
        / ``target * max_ratio`` and clamped to the pydantic floors (min ≥ 50,
        max ≥ 100, max > min). With the default ratios (0.25 / 1.5) and
        ``target=2000`` this reproduces the historical 500 / 3000 bounds exactly,
        so the unweighted default path is unchanged. Centralizes the derivation
        shared by the web UI (``_resolve_chunking``) and the CLI chunk stage so a
        per-chapter ``target_size`` actually rescales the bounds and bites.
        """
        target = int(target)
        if not (math.isfinite(min_ratio) and math.isfinite(max_ratio)):
            raise ValueError("min_ratio and max_ratio must be finite numbers")
        if max_ratio <= min_ratio:
            raise ValueError(
                f"max_ratio ({max_ratio}) must be greater than min_ratio ({min_ratio})"
            )
        min_chunk, max_chunk = derive_chunk_bounds(target, min_ratio, max_ratio)
        return cls(
            target_size=target,
            min_chunk_size=min_chunk,
            max_chunk_size=max_chunk,
            min_ratio=min_ratio,
            max_ratio=max_ratio,
            overlap_paragraphs=overlap_paragraphs,
            min_overlap_words=min_overlap_words,
        )


class TranslationMode(str, Enum):
    """Translation workflow modes."""
    API = "api"
    MANUAL = "manual"


class APIProvider(str, Enum):
    """Supported LLM API providers."""
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    CUSTOM = "custom"


class TranslationConfig(BaseModel):
    """Configuration for translation process."""
    mode: TranslationMode = TranslationMode.MANUAL
    api_provider: Optional[APIProvider] = None
    model: Optional[str] = None
    prompt_template: str = Field(default="prompts/translation_prompt.txt")
    style_guide_path: Optional[str] = Field(default=None, description="Path to style guide JSON file")

    @field_validator('api_provider')
    @classmethod
    def api_provider_required_for_api_mode(cls, v: Optional[APIProvider], info) -> Optional[APIProvider]:
        """Ensure api_provider is set when mode is API."""
        if 'mode' in info.data and info.data['mode'] == TranslationMode.API and v is None:
            raise ValueError('api_provider required when mode is API')
        return v


class EvaluationConfig(BaseModel):
    """Configuration for evaluation process."""
    enabled_evals: list[str] = Field(
        default_factory=lambda: ["length", "paragraph", "completeness"],
        description="List of evaluator names to run"
    )
    fail_on_errors: bool = Field(default=False, description="Stop pipeline on evaluation errors")
    generate_reports: bool = Field(default=True, description="Generate evaluation reports")


class ChapterDetectionConfig(BaseModel):
    """
    Configuration for automatic chapter detection and context.

    Controls how books are split into chapters and how previous chapter
    context is included in translation prompts for continuity.

    Example:
        ChapterDetectionConfig(
            pattern_type="roman",
            include_previous_context=True,
            context_paragraphs=2
        )
    """
    pattern_type: str = Field(
        default="roman",
        description=(
            "Chapter pattern type: 'auto' (detect the best fit from the text), "
            "any named pattern in split_patterns.json ('roman', 'numeric', "
            "'chapter_roman_titled', 'chapter_numeric_titled', 'allcaps_heading', "
            "'bare_roman'), or 'custom' (with custom_pattern)"
        )
    )
    custom_pattern: Optional[str] = Field(
        default=None,
        description="Custom regex pattern for chapter detection (if pattern_type is 'custom')"
    )
    include_previous_context: bool = Field(
        default=True,
        description="Include previous chapter ending in translation prompts"
    )
    context_paragraphs: int = Field(
        default=3,
        ge=0,
        le=10,
        description="Number of paragraphs from end of previous section to include"
    )
    min_context_chars: int = Field(
        default=200,
        ge=0,
        description="Minimum characters of context from previous section (dual-constraint with context_paragraphs)"
    )


class ProjectConfig(BaseModel):
    """
    Overall project configuration.

    Example:
        ProjectConfig(
            project_name="don_quixote",
            source_language="en",
            target_language="es",
            chunking=ChunkingConfig(),
            translation=TranslationConfig(mode=TranslationMode.MANUAL),
            evaluation=EvaluationConfig(),
            chapter_detection=ChapterDetectionConfig()
        )
    """
    project_name: str = Field(min_length=1, description="Project identifier")
    source_language: str = Field(default="en", min_length=2, max_length=3)
    target_language: str = Field(default="es", min_length=2, max_length=3)
    chunking: ChunkingConfig = Field(default_factory=ChunkingConfig)
    translation: TranslationConfig = Field(default_factory=TranslationConfig)
    evaluation: EvaluationConfig = Field(default_factory=EvaluationConfig)
    chapter_detection: ChapterDetectionConfig = Field(default_factory=ChapterDetectionConfig)


class ChapterStatus(str, Enum):
    """Status of a chapter in the translation pipeline."""
    PENDING = "pending"
    CHUNKED = "chunked"
    TRANSLATING = "translating"
    TRANSLATED = "translated"
    VALIDATED = "validated"
    FAILED = "failed"


class ChapterState(BaseModel):
    """State tracking for a single chapter."""
    status: ChapterStatus = ChapterStatus.PENDING
    chunks: list[str] = Field(default_factory=list, description="List of chunk IDs")
    completed_chunks: int = Field(default=0, ge=0)
    last_updated: datetime = Field(default_factory=datetime.now)


class ProjectStatistics(BaseModel):
    """Overall project statistics."""
    total_chunks: int = Field(default=0, ge=0)
    translated_chunks: int = Field(default=0, ge=0)
    validated_chunks: int = Field(default=0, ge=0)
    total_words: int = Field(default=0, ge=0)
    total_errors: int = Field(default=0, ge=0)
    total_warnings: int = Field(default=0, ge=0)


class JudgeScore(BaseModel):
    """Per-dimension absolute scores from the LLM judge (1-5 scale)."""
    fluency: int = Field(ge=1, le=5)
    fidelity: int = Field(ge=1, le=5)
    regional: int = Field(ge=1, le=5)
    voice: Optional[int] = Field(default=None, ge=1, le=5)
    rationale: str
    raw_response: str

    @computed_field
    @property
    def normalized_score(self) -> float:
        """Maps avg of present dims [1,5] to [0.0, 1.0] for EvalResult.score."""
        dims = [self.fluency, self.fidelity, self.regional]
        if self.voice is not None:
            dims.append(self.voice)
        return (sum(dims) / len(dims) - 1) / 4


class PairwiseVerdict(BaseModel):
    """Per-dimension pairwise winner verdicts from the LLM judge."""
    fluency_winner: Literal['A', 'B', 'tie']
    fidelity_winner: Literal['A', 'B', 'tie']
    regional_winner: Literal['A', 'B', 'tie']
    voice_winner: Literal['A', 'B', 'tie', 'N/A']
    overall_winner: Literal['A', 'B', 'tie']
    rationale: str
    raw_response: str


class RetranslationResult(BaseModel):
    """Result of a single-sentence retranslation from the reader UI."""
    new_translation: str = Field(description="Cleaned LLM output ready to insert")
    model: str = Field(description="Model id used for the call")
    provider: str = Field(description="Provider id used for the call")
    prompt_tokens: int = Field(ge=0, description="Estimated input tokens")
    completion_tokens: int = Field(ge=0, description="Estimated output tokens")
    cost_usd: float = Field(ge=0.0, description="Estimated cost in USD")
    raw_response: str = Field(description="Unprocessed LLM response for replay/debugging")


class PipelineStage(str, Enum):
    """Current stage in the translation pipeline."""
    INIT = "init"
    CHUNKING = "chunking"
    TRANSLATING = "translating"
    COMBINING = "combining"
    EVALUATING = "evaluating"
    COMPLETED = "completed"
    FAILED = "failed"


class ProjectState(BaseModel):
    """
    Tracks the current state of a translation project.

    This allows the pipeline to resume from any point.
    """
    project_name: str
    current_stage: PipelineStage = PipelineStage.INIT
    chapters: dict[str, ChapterState] = Field(default_factory=dict)
    statistics: ProjectStatistics = Field(default_factory=ProjectStatistics)
    last_command: Optional[str] = None
    updated_at: datetime = Field(default_factory=datetime.now)
