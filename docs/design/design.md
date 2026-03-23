# Book Translation Workflow - Design Document

## Project Overview

A semi-automated pipeline for translating public domain books from English to Spanish with quality assurance evaluations. The system supports both API-driven and manual copy/paste workflows.

## Core Philosophy

- **Modularity**: Each component is independent and can be run separately
- **Resumability**: State is tracked; pipeline can pause and resume at any stage
- **Flexibility**: Support both automated (API) and manual translation workflows
- **Quality-First**: Multiple evaluation layers catch issues before human review

## Architecture

### Pipeline Stages

```
1. INPUT → 2. CHUNK → 3. TRANSLATE → 4. COMBINE → 5. EVALUATE → 6. OUTPUT
   ↓           ↓           ↓             ↓            ↓            ↓
Chapters   Chunks +   Translated    Translated   QA Reports   Final
          Overlap     Chunks        Chapters                  Chapters
```

### Data Flow

```
projects/
└── {project_name}/
    ├── config.json           # Project settings
    ├── glossary.json         # Translation glossary
    ├── state.json            # Pipeline state tracking
    ├── chapters/
    │   ├── original/         # Source English chapters
    │   │   ├── chapter_01.txt
    │   │   └── chapter_02.txt
    │   └── translated/       # Final Spanish chapters
    │       ├── chapter_01.txt
    │       └── chapter_02.txt
    ├── chunks/
    │   ├── original/         # Chunked source text
    │   │   ├── ch01_chunk_001.json
    │   │   └── ch01_chunk_002.json
    │   └── translated/       # Translated chunks
    │       ├── ch01_chunk_001.json
    │       └── ch01_chunk_002.json
    └── reports/
        ├── eval_ch01_20250128.json
        └── eval_ch01_20250128.html
```

## Data Models

### Core Structures

**Chunk**
```python
{
    "id": str,                    # e.g., "ch01_chunk_003"
    "chapter_id": str,            # e.g., "chapter_01"
    "position": int,              # Sequence number in chapter
    "source_text": str,           # Original English text
    "translated_text": str | None,# Spanish translation (null if pending)
    "metadata": {
        "char_start": int,        # Position in original chapter
        "char_end": int,
        "overlap_start": int,     # Characters of overlap with previous
        "overlap_end": int,       # Characters of overlap with next
        "paragraph_count": int,
        "word_count": int
    },
    "status": str,                # "pending", "translated", "validated"
    "created_at": str,            # ISO timestamp
    "translated_at": str | None   # ISO timestamp
}
```

**EvalResult**
```python
{
    "eval_name": str,             # e.g., "dictionary_check"
    "eval_version": str,          # e.g., "1.0.0"
    "target_id": str,             # Chunk or chapter ID
    "target_type": str,           # "chunk" or "chapter"
    "passed": bool,               # Overall pass/fail
    "score": float | None,        # 0.0-1.0 if applicable
    "issues": [                   # List of problems found
        {
            "severity": str,      # "error", "warning", "info"
            "message": str,
            "location": str | None,  # Text snippet or position
            "suggestion": str | None # How to fix
        }
    ],
    "metadata": dict,             # Eval-specific data
    "executed_at": str            # ISO timestamp
}
```

**Glossary**
```python
{
    "terms": [
        {
            "english": str,
            "spanish": str,
            "type": str,          # "character", "place", "concept", "technical"
            "context": str | None,# Usage notes
            "alternatives": [str] # Other valid translations
        }
    ],
    "version": str,
    "updated_at": str
}
```

**ProjectConfig**
```python
{
    "project_name": str,
    "source_language": str,       # "en"
    "target_language": str,       # "es"
    "chunking": {
        "method": str,            # "paragraph", "semantic", "fixed"
        "target_size": int,       # Target words per chunk
        "overlap_paragraphs": int,# Overlap size (1-3)
        "min_chunk_size": int,    # Minimum words
        "max_chunk_size": int     # Maximum words
    },
    "translation": {
        "mode": str,              # "api" or "manual"
        "api_provider": str | None,  # "openai", "anthropic", etc.
        "model": str | None,
        "prompt_template": str,   # Relative path to prompt template (Jinja2)
        "context_prompt_template": str | None,  # For context generation
        "prompt_variables": dict  # Custom template variables
    },
    "evaluation": {
        "enabled_evals": [str],   # List of evaluator names
        "fail_on_errors": bool,   # Stop pipeline on eval failures
        "generate_reports": bool
    }
}
```

**PromptMetadata**
```python
{
    "prompt_type": str,           # "translation", "context_generation"
    "template_path": str,         # Path to template file used
    "rendered_at": str,           # ISO timestamp
    "template_version": str | None,  # Git hash or version
    "variables_used": dict        # Variables passed to template
}
```

## Prompt Management

### Overview

The system uses **Jinja2 templates** for all LLM prompts, allowing flexible customization per project while maintaining sensible defaults. Prompts are treated as first-class configuration entities with version tracking and variable substitution.

### Template Resolution

Prompts are resolved using a **two-tier search path**:

1. **Project Override**: `projects/{project_name}/prompts/{template_name}`
2. **Global Default**: `prompts/{template_name}`

If neither exists, the system raises an error. This allows projects to customize specific prompts without duplicating all defaults.

**Example**:
```
# Project uses custom translation prompt but default context generator
projects/don_quixote/prompts/translation.txt.jinja  ← Custom
prompts/context_generation.txt.jinja                 ← Default used
```

### Template Structure

All prompt templates use Jinja2 syntax with standardized variables:

```jinja2
{# translation.txt.jinja - Default translation prompt #}
You are translating a book from {{source_language}} to {{target_language}}.

Project: {{project_name}}

{% if book_context %}
Book Context:
{{book_context}}
{% endif %}

{% if glossary %}
Translation Glossary:
{{glossary}}

Please use these exact translations for consistency.
{% endif %}

{% if style_guide %}
Style Guidelines:
{{style_guide}}
{% endif %}

Translate the following text naturally and fluently while:
- Preserving paragraph structure exactly
- Maintaining the author's tone and style
- Using the glossary terms consistently
- Keeping special formatting (emphasis, etc.)

Source Text:
{{source_text}}

Provide only the translated text without explanation.
```

### Standard Template Variables

| Variable | Type | Description | Required |
|----------|------|-------------|----------|
| `project_name` | str | Project identifier | Yes |
| `source_language` | str | Source language code (e.g., "en") | Yes |
| `target_language` | str | Target language code (e.g., "es") | Yes |
| `source_text` | str | Text to translate | Yes (translation) |
| `glossary` | str | Formatted glossary terms | No |
| `book_context` | str | Book description, genre, style notes | No |
| `style_guide` | str | Translation style guidelines | No |
| `chunk_id` | str | Current chunk identifier | No |
| `chapter_id` | str | Current chapter identifier | No |
| `excerpt` | str | Text excerpt for analysis | No (context gen) |

Custom variables can be added via `TranslationConfig.prompt_variables`.

### Prompt Types

**Current** (Phase 1-4):
- `translation` - Main chunk translation prompt
- `context_generation` - Generate book context from excerpts

**Future** (Phase 5+):
- `fluency_evaluation` - LLM-based fluency assessment
- `style_evaluation` - Style consistency checking
- `glossary_suggestion` - Auto-generate glossary terms

### Glossary Formatting

When a glossary is provided, it's automatically formatted for prompt inclusion:

```
Glossary Terms:
- Harry: Harry (character)
- Hogwarts: Hogwarts (place)
- wand: varita (technical) [alternatives: vara mágica]
- spell: hechizo (technical)
```

Format is generated by `format_glossary_for_prompt()` in `src/utils/file_io.py`.

### Creating Project Overrides

To customize a prompt for a specific project:

```bash
# Copy global template to project directory
cp prompts/translation.txt.jinja projects/my_book/prompts/

# Edit the project-specific version
# System will automatically use project version
```

Or programmatically:
```python
from src.utils.file_io import create_project_prompt_override

create_project_prompt_override(
    project_path=Path("projects/don_quixote"),
    prompt_name="translation.txt.jinja"
)
```

### Template Validation

Templates are validated before use:
- File exists and is readable
- Jinja2 syntax is valid
- Required variables are used
- No undefined variables in render

Validation occurs at:
- Project initialization (`book-translate init`)
- Manual validation (`book-translate validate-prompts`)
- First use (with warning if not pre-validated)

### Prompt Versioning

Each translation tracks which prompt was used via `PromptMetadata`:

```python
chunk.prompt_metadata = PromptMetadata(
    prompt_type="translation",
    template_path="projects/my_book/prompts/translation.txt.jinja",
    rendered_at="2025-01-28T10:30:00",
    template_version="abc123",  # Git hash
    variables_used={
        "source_language": "en",
        "target_language": "es",
        "glossary": "...",
        # ...
    }
)
```

This enables:
- Reproducibility (re-render with same template)
- A/B testing (compare results from different prompts)
- Debugging (which prompt version caused issues)

### Best Practices

**DO**:
- Use conditional blocks (`{% if variable %}`) for optional sections
- Document expected variables in template comments
- Test templates with `book-translate test-prompt` before full runs
- Version control your project-specific prompts
- Keep prompts focused on single responsibility

**DON'T**:
- Hard-code project-specific details in global templates
- Use undefined variables without conditionals
- Make prompts excessively long (>2000 tokens)
- Include sensitive data in templates (use variables instead)

### Related Documentation

- Full guide: `PROMPT_GUIDE.md`
- Variable reference: `prompts/VARIABLES.md`
- Template directory: `prompts/README.md`
- Getting started: `GETTING_STARTED.md` (Customizing Prompts section)

## Module Responsibilities

### 1. Chunker (`src/chunker.py`)

**Purpose**: Divide chapters into translation-sized chunks with overlap

**Key Functions**:
- `chunk_chapter(chapter_text: str, config: ChunkingConfig) -> list[Chunk]`
- `detect_paragraph_boundaries(text: str) -> list[int]`
- `calculate_overlap(chunks: list[Chunk], overlap_paragraphs: int) -> list[Chunk]`

**Strategy**:
- Split on paragraph boundaries (double newline)
- Group paragraphs until target size reached
- Add overlap: include last N paragraphs and at least X characters of previous chunk
- Store boundary metadata for later recombination

**Output**: JSON files in `chunks/original/`

### 2. Translator (`src/translator.py`)

**Purpose**: Handle translation via API or generate manual workbooks

**Key Functions**:
- `translate_chunk_api(chunk: Chunk, glossary: Glossary, prompt: str) -> Chunk`
- `generate_workbook(chunks: list[Chunk], glossary: Glossary) -> str`
- `import_translations(workbook_path: str) -> list[Chunk]`

**API Mode**:
- Send chunk + glossary + prompt to LLM API
- Handle rate limiting and retries
- Update chunk with translation and save

**Manual Mode**:
- Generate formatted document with:
  - Chunk ID headers
  - Source text
  - Glossary reference
  - Clear "PASTE TRANSLATION HERE" markers
- Parse returned workbook to extract translations

**Output**: JSON files in `chunks/translated/`

### 3. Combiner (`src/combiner.py`)

**Purpose**: Merge translated chunks back into complete chapters

**Key Functions**:
- `combine_chunks(chunks: list[Chunk], strategy: str) -> str`
- `resolve_overlap(chunk_a: Chunk, chunk_b: Chunk) -> str`
- `validate_completeness(chunks: list[Chunk]) -> bool`

**Overlap Resolution Strategies**:
- **Use Previous**: Use the overlap section from the chunk that ends with the overlap, and discard it from the chunk that begins with the overlap
- **Similarity Match**: Compare overlaps, use most consistent version
- **Manual Review**: Flag discrepancies for human decision

**Output**: Text files in `chapters/translated/`

### 4. Evaluators (`src/evaluators/`)

**Purpose**: Automated quality checks on translations

**Base Class** (`base.py`):
```python
class BaseEvaluator(ABC):
    name: str
    version: str

    @abstractmethod
    def evaluate(self, chunk: Chunk, context: dict) -> EvalResult:
        """Run evaluation and return results"""

    def format_report(self, results: list[EvalResult]) -> str:
        """Generate human-readable report"""
```

#### 4.1 Length Evaluator (`length_eval.py`)

**Checks**:
- Target text length within expected range (Spanish typically 10-20% longer)
- Not suspiciously short (possible missing content)
- Not excessively long (possible duplication)

**Severity**: Error if outside 0.5x-2.0x, Warning if outside 1.1x-1.3x

#### 4.2 Paragraph Evaluator (`paragraph_eval.py`)

**Checks**:
- Paragraph count matches between source and translation
- Paragraph structure preserved (no merged or split paragraphs)
- Line breaks and formatting maintained

**Severity**: Error if count mismatch, Warning if structure differs

#### 4.3 Dictionary Evaluator (`dictionary_eval.py`)

**Checks**:
- All words exist in Spanish dictionary (PyEnchant/Hunspell)
- For words not in Spanish dictionary, flag remaining English words (check against English dictionary)
- Flag all words not in Spanish or English dictionary, unless they are in the glossary (providing glossary in this stage is optional)
- Offer an optional check for common OCR/typing errors

**Severity**: Error for English words, Warning for unknown Spanish words

#### 4.4 Grammar Evaluator (`grammar_eval.py`)

**Checks** (via LanguageTool):
- Gender/number agreement
- Verb conjugation
- Article usage
- Punctuation
- Common translation errors

**Severity**: Based on LanguageTool rule severity

#### 4.5 Glossary Evaluator (`glossary_eval.py`)

**Checks**:
- Glossary terms translated consistently
- Character names use specified translations
- Technical terms follow glossary
- No mixing of alternative translations

**Severity**: Error if glossary term incorrectly translated

#### 4.6 Completeness Evaluator (`completeness_eval.py`)

**Checks**:
- No missing chunks in sequence
- All chunks have translations
- No truncated text
- Special markers preserved (chapters, sections, etc.)

**Severity**: Error for missing content

#### 4.7 Overlap Consistency Evaluator (`overlap_eval.py`)

**Checks**:
- Overlapping regions between chunks match
- No contradictory translations in overlaps
- Smooth transitions at chunk boundaries

**Severity**: Warning for minor differences, Error for major inconsistencies

### 5. CLI Interface (`src/cli.py`)

**Commands**:

```bash
# Project management
book-translate init <project_name>
book-translate config <project_name> [--set KEY=VALUE]

# Pipeline stages
book-translate chunk <project_name> <chapter_file>
book-translate translate <project_name> [--chunk-id ID] [--mode api|manual]
book-translate combine <project_name> <chapter_id>

# Evaluation
book-translate eval <project_name> <target> [--eval-name NAME]
book-translate report <project_name> [--format html|json|text]

# Utilities
book-translate status <project_name>
book-translate glossary <project_name> [--add | --list | --edit]
book-translate resume <project_name>  # Continue from last state
```

## Evaluation Workflow

### Tier 1: Fast Code Evals (Run on Each Chunk)
- Length check
- Paragraph preservation
- Basic dictionary check
- Completeness

**Goal**: Catch obvious errors immediately, fast feedback

### Tier 2: Comprehensive Evals (Run on Combined Chapters)
- Full dictionary check
- Grammar check (LanguageTool)
- Glossary adherence
- Overlap consistency

**Goal**: Thorough quality check before human review

### Tier 3: Optional LLM Evals (Spot Check)
- Fluency assessment
- Style preservation
- Context retention
- Idiomatic usage

**Goal**: Subjective quality measures, expensive so used sparingly

## State Management

**state.json** tracks pipeline progress:

```python
{
    "project_name": str,
    "current_stage": str,         # "chunking", "translating", etc.
    "chapters": {
        "chapter_01": {
            "status": str,        # "pending", "chunked", "translated", etc.
            "chunks": [str],      # List of chunk IDs
            "completed_chunks": int,
            "last_updated": str
        }
    },
    "statistics": {
        "total_chunks": int,
        "translated_chunks": int,
        "validated_chunks": int,
        "total_words": int,
        "total_errors": int,
        "total_warnings": int
    },
    "last_command": str,
    "updated_at": str
}
```

## Error Handling Strategy

### Recoverable Errors
- API rate limits → Retry with backoff
- Dictionary unknown word → Flag for review, continue
- Grammar warnings → Log, continue

### Critical Errors
- Missing source file → Halt, report
- Invalid JSON state → Attempt recovery, backup, halt if failed
- API authentication failure → Halt, report
- Glossary term mismatched → Halt if config.fail_on_errors

### Logging
- All actions logged to `projects/{name}/pipeline.log`
- Separate error log: `projects/{name}/errors.log`
- Structured logging with timestamps and context

## Development Phases

### Phase 1: Foundation ✓
- [x] Project structure
- [ ] Data models (models.py)
- [ ] Base evaluator class
- [ ] Example fixtures
- [ ] Configuration management

### Phase 2: Evaluation System (CURRENT FOCUS)
- [ ] Length evaluator
- [ ] Paragraph evaluator
- [ ] Dictionary evaluator
- [ ] Glossary evaluator
- [ ] Completeness evaluator
- [ ] Evaluation reporting
- [ ] CLI commands for evaluation

### Phase 3: Chunking & Combination
- [ ] Paragraph-based chunker
- [ ] Overlap calculator
- [ ] Chunk combiner
- [ ] Overlap resolver
- [ ] CLI commands for chunking/combining

### Phase 4: Translation Interface
- [ ] Manual workbook generator
- [ ] Workbook parser/importer
- [ ] API translator (OpenAI/Anthropic)
- [ ] Translation queue management
- [ ] CLI commands for translation

### Phase 5: Advanced Evals
- [ ] Grammar evaluator (LanguageTool)
- [ ] Overlap consistency evaluator
- [ ] Optional LLM evaluators

### Phase 6: Polish & Usability
- [ ] HTML report generation
- [ ] Progress bars and status display
- [ ] Comprehensive error messages
- [ ] Documentation and examples
- [ ] Tests for all modules

## Technology Stack

**Core**:
- Python 3.11+
- Pydantic v2 (data validation)
- Jinja2 (prompt templates)
- Click or Typer (CLI)
- Rich (terminal output)

**Text Processing**:
- spaCy + es_core_news_md (Spanish NLP)
- PyEnchant or CyHunspell (spell-checking)
- language-tool-python (grammar checking)

**APIs** (optional):
- anthropic (Claude)
- openai (GPT models)

**Development**:
- pytest (testing)
- black (formatting)
- mypy (type checking)

## Key Design Decisions

### Why Overlap in Chunks?
- Maintains context across boundaries
- Allows validation of consistency
- Prevents loss of narrative flow
- Helps resolve ambiguous translations

### Why JSON for Chunks?
- Stores metadata alongside text
- Easy to version and track
- Can store multiple translation attempts
- Machine and human readable

### Why Modular Pipeline?
- Test each stage independently
- Resume from any point
- Swap implementations (API vs manual)
- Easier debugging
- Parallel development of modules

### Why Multiple Evaluation Tiers?
- Balance speed vs thoroughness
- Fast feedback during translation
- Comprehensive check before review
- Cost optimization (expensive LLM evals only when needed)

### Why Support Manual Mode?
- Not everyone has API access/budget
- Some translators prefer manual review
- Allows use of any LLM (copy/paste)
- Useful for testing before automating

## Testing Strategy

### Unit Tests
- Each evaluator with known good/bad examples
- Data model validation
- Utility functions

### Integration Tests
- Full pipeline on small test chapter
- State persistence and recovery
- Error handling scenarios

### Fixtures
- Sample English chapters (public domain)
- Manually translated Spanish versions
- Known errors for evaluators to catch
- Various edge cases

## Future Enhancements

- Web UI for translation review
- Multi-language support beyond Spanish
- Parallel chunk translation
- Translation memory/caching
- Custom dictionary support
- Export to EPUB/PDF formats
- Collaboration features (multiple translators)
- Version control for translations

## References & Resources

- Pydantic models: Core data structures defined in `src/models.py`
- Evaluator examples: See `src/evaluators/length_eval.py` as template
- Test fixtures: `examples/` directory
- CLI usage: Run `book-translate --help`

---

**Document Version**: 1.0
**Last Updated**: 2025-01-28
**Status**: Phase 2 (Evaluation System) in progress
