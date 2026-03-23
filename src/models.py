"""
Core data models for the book translation workflow.

This module defines the Pydantic models that represent all data structures
used throughout the translation pipeline.
"""

from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Optional

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
    """
    severity: IssueLevel
    message: str
    location: Optional[str] = None
    suggestion: Optional[str] = None


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
            annotation_count = len(self.review_data.annotations)

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
        return len(self.review_data.annotations)


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
    version: str = Field(default="1.0", description="Version (manually maintained by user)")
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)


class ChunkingMethod(str, Enum):
    """Methods for dividing chapters into chunks."""
    PARAGRAPH = "paragraph"
    SEMANTIC = "semantic"
    FIXED = "fixed"


class ChunkingConfig(BaseModel):
    """Configuration for chunking chapters."""
    method: ChunkingMethod = ChunkingMethod.PARAGRAPH
    target_size: int = Field(default=2000, ge=100, description="Target words per chunk")
    overlap_paragraphs: int = Field(default=2, ge=0, le=5, description="Minimum paragraphs of overlap")
    min_overlap_words: int = Field(default=100, ge=0, description="Minimum words in overlap")
    min_chunk_size: int = Field(default=500, ge=50, description="Minimum words per chunk")
    max_chunk_size: int = Field(default=3000, ge=100, description="Maximum words per chunk")

    @field_validator('max_chunk_size')
    @classmethod
    def max_greater_than_min(cls, v: int, info) -> int:
        """Ensure max_chunk_size > min_chunk_size."""
        if 'min_chunk_size' in info.data and v <= info.data['min_chunk_size']:
            raise ValueError('max_chunk_size must be > min_chunk_size')
        return v


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
        description="Chapter pattern type: 'roman', 'numeric', or 'custom'"
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
        default=2,
        ge=0,
        le=10,
        description="Number of paragraphs from end of previous chapter to include"
    )
    context_words: Optional[int] = Field(
        default=None,
        ge=0,
        description="Alternative to context_paragraphs: number of words from end of previous chapter"
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
