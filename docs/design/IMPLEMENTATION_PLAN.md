# Implementation Plan - Book Translation Workflow

## Overview

This document provides the step-by-step implementation plan for building the book translation system. We're starting with Phase 2 (Evaluation System) since you already have test blocks to work with.

## Current Status

- **Phase**: Phase 2 - Evaluation System
- **Starting Point**: Foundation setup → Build evaluators
- **Goal**: Complete evaluation framework that can assess translation quality

---

## Phase 1A: Minimal Foundation (CURRENT FOCUS)

**Goal**: Provide minimum infrastructure to support Phase 2 (Evaluation System)

**Why Split Phase 1?**
Phase 2 (Evaluation) only needs basic models, config, and file I/O. Prompt infrastructure is only used in Phase 4 (Translation) when generating new translations via LLM. By deferring prompt features to Phase 1B, we reduce initial work by ~40% while maintaining full Phase 2 capability.

**What's in Phase 1A:**
- Core data models (Chunk, EvalResult, Issue, Glossary, configs)
- Basic configuration loading/saving
- Basic file I/O for chunks, glossaries, and state
- Test fixtures for evaluation
- Base evaluator class (already complete)

**What's Deferred to Phase 1B (before Phase 4):**
- Prompt templates and template engine integration
- Prompt-related models (PromptType, PromptMetadata)
- Template resolution and rendering functions
- Prompt-specific fixtures

**Time Estimate**: 8-9 hours (vs 14-17 for full Phase 1)
**Result**: Phase 2 fully unblocked with minimal foundational work

---

### Phase 1A Progress Status

**Overall**: 5 of 5 tasks complete (100%) ✅

| Task | Status | Time |
|------|--------|------|
| 1.1 Core Data Models | ✓ Complete | 3h |
| 1.3 File I/O Utilities | ✓ Complete | 2h |
| 1.2 Configuration Management | ✓ Complete | 2h |
| 1.4 Test Fixtures | ✓ Complete | 1.5h |
| 1.5 Base Evaluator Class | ✓ Complete | (done) |

**Completed**: 8.5 hours
**Remaining**: 0 hours

---

### Task 1.1: Core Data Models (Basic Version)
**File**: `src/models.py`
**Priority**: CRITICAL
**Dependencies**: None
**Phase**: 1A
**Status**: ✓ COMPLETE

Create Pydantic models for:
- `Chunk` - Source and translated text with metadata
- `EvalResult` - Evaluation results with issues
- `Issue` - Individual problem found by evaluator
- `Glossary` and `GlossaryTerm` - Translation glossary
- `ChunkingConfig` - Chunking parameters
- `TranslationConfig` - Translation settings (basic version)
- `EvaluationConfig` - Eval settings
- `ProjectConfig` - Overall project configuration
- `ProjectState` - Pipeline state tracking

**TranslationConfig** (basic version):
- Keep existing fields: `mode`, `api_provider`, `model`, `prompt_template`
- Defer prompt enhancements to Phase 1B

**Acceptance Criteria**:
- All models have type hints
- Include validation rules (e.g., score between 0-1)
- Add example instances in docstrings
- Models can serialize to/from JSON
- Include helper methods (e.g., `chunk.word_count()`)

**Deferred to Phase 1B**:
- `PromptType` enum
- `PromptMetadata` model
- Enhanced `TranslationConfig` with `context_prompt_template` and `prompt_variables`
- `prompt_metadata` field in Chunk

**Estimated Time**: 3 hours

**Completion Notes**:
- All required models implemented (357 lines of well-structured code)
- Includes comprehensive field validators and computed properties
- Helper methods for common operations (e.g., `Glossary.find_term()`)
- Excellent documentation with docstrings and examples
- No prompt-related models added (correctly following Phase 1A scope)

---

### Task 1.2: Configuration Management (Basic Version)
**File**: `src/config.py`
**Priority**: HIGH
**Dependencies**: Task 1.1 (models.py)
**Phase**: 1A
**Status**: ✓ COMPLETE

Functions needed:
- `load_project_config(project_path: Path) -> ProjectConfig`
- `save_project_config(project_path: Path, config: ProjectConfig)`
- `create_default_config(project_name: str) -> ProjectConfig`
- `validate_config(config: ProjectConfig) -> list[str]` (returns errors)

**Acceptance Criteria**:
- Handles missing files gracefully
- Validates required fields
- Provides sensible defaults
- Clear error messages
- JSON serialization with proper formatting

**Deferred to Phase 1B**:
- `resolve_prompt_path()` - Template resolution
- `get_default_prompt_variables()` - Variable generation for templates
- `validate_prompt_template()` - Jinja2 syntax validation

**Estimated Time**: 2 hours

**Completion Notes**:
- All 4 required functions implemented (~240 lines of well-structured code)
- Follows file_io.py patterns: atomic writes, UTF-8 encoding, comprehensive docstrings
- Robust validation including API mode requirements, chunking constraints, and known evaluators
- Excellent error handling with clear, actionable messages
- All manual tests passing (6/6): default config creation, round-trip save/load, validation tests, error handling
- No prompt-related functions (correctly deferred to Phase 1B)

---

### Task 1.3: File I/O Utilities (Basic Version)
**File**: `src/utils/file_io.py`
**Priority**: HIGH
**Dependencies**: Task 1.1 (models.py)
**Phase**: 1A
**Status**: ✓ COMPLETE

Functions needed:
- `load_chunk(chunk_path: Path) -> Chunk`
- `save_chunk(chunk: Chunk, output_path: Path)`
- `load_glossary(glossary_path: Path) -> Glossary`
- `save_glossary(glossary: Glossary, output_path: Path)`
- `load_state(project_path: Path) -> ProjectState`
- `save_state(state: ProjectState, project_path: Path)`
- `ensure_project_structure(project_path: Path)` (creates directories)

**Acceptance Criteria**:
- JSON serialization with pretty printing
- Atomic writes (write to temp, then rename)
- Proper error handling with informative messages
- Create directories if they don't exist
- Type-safe loading with Pydantic validation

**Deferred to Phase 1B**:
- `load_prompt_template()` - Jinja2 template loading
- `render_prompt()` - Template rendering
- `load_and_render_prompt()` - High-level template function
- `format_glossary_for_prompt()` - Glossary formatting for prompts
- `create_project_prompt_override()` - Project override creation

**Estimated Time**: 2 hours

**Completion Notes**:
- All 7 functions implemented (~280 lines)
- Uses Pydantic v2 methods (model_dump, model_validate)
- Atomic writes via temporary files + rename pattern
- Proper error handling with informative messages
- UTF-8 encoding for Spanish text support
- Comprehensive docstrings with examples
- Manually tested - all round-trip tests passing
- No prompt-related functions (correctly deferred to Phase 1B)

---

### Task 1.4: Test Fixtures (Basic Version)
**Directory**: `tests/fixtures/`
**Priority**: HIGH
**Dependencies**: Task 1.1 (models.py)
**Phase**: 1A
**Status**: ✓ COMPLETE

Create example files:
- `chunk_english.json` - Sample English chunk
- `chunk_translated_good.json` - Good Spanish translation
- `chunk_translated_errors.json` - Translation with deliberate errors:
  - Length mismatch
  - Wrong paragraph count
  - Misspelled words
  - English words remaining
  - Glossary term errors
- `glossary_sample.json` - Sample glossary with 5-10 terms
- `chapter_sample.txt` - Full chapter for integration tests

**Acceptance Criteria**:
- Files follow data model schemas
- Error examples cover all evaluator types
- Well-documented (comments explaining errors)
- Based on real public domain content
- JSON files use proper formatting

**Deferred to Phase 1B**:
- `prompts/translation_test.txt.jinja` - Test translation prompt
- `prompts/context_generation_test.txt.jinja` - Test context generation
- `test_project_prompts/translation_custom.txt.jinja` - Project override example
- `book_context_sample.txt` - Book context sample
- `style_guide_sample.txt` - Style guide sample
- `prompt_metadata` in chunk fixtures

**Estimated Time**: 1.5 hours

**Completion Notes**:
- All 5 required fixture files created and validated
- **Source material switched from "The Call of the Wild" to "Pride and Prejudice"** to avoid content policy issues with violent scenes
- Files created:
  - `chapter_sample.txt` - Full Chapter 1 from P&P (113 lines, excellent dialogue/narrative)
  - `chunk_english.json` - Famous opening lines (69 words, 2 paragraphs)
  - `chunk_translated_good.json` - Quality Spanish translation (1.16x length ratio, should PASS all evaluators)
  - `chunk_translated_errors.json` - Deliberately broken translation with 6 documented errors (length, paragraphs, English words, misspellings)
  - `glossary_sample.json` - 10 P&P terms (characters: Elizabeth Bennet, Mr. Darcy, etc.; places: Netherfield Park, Longbourn; concepts: entailment, assembly)
- All JSON files follow Pydantic models from Task 1.1
- Comprehensive error documentation for evaluator testing
- Zero violent content ensures smooth evaluation/translation pipeline

---

### Task 1.5: Base Evaluator Class
**File**: `src/evaluators/base.py`
**Priority**: CRITICAL
**Dependencies**: Task 1.1 (models.py)
**Phase**: 1A
**Status**: ✓ COMPLETE

Define abstract base class:
```python
class BaseEvaluator(ABC):
    name: str
    version: str
    description: str

    @abstractmethod
    def evaluate(self, chunk: Chunk, context: dict) -> EvalResult:
        """Run evaluation on chunk"""

    def format_issues(self, issues: list[Issue]) -> str:
        """Format issues for display"""

    def should_fail(self, result: EvalResult) -> bool:
        """Determine if errors are critical"""
```

Include helper functions:
- `create_issue(severity, message, location, suggestion)`
- `calculate_pass_fail(issues: list[Issue]) -> bool`
- `issue_summary(issues: list[Issue]) -> dict` (count by severity)

**Acceptance Criteria**:
- Clear interface all evaluators must follow
- Shared utility functions
- Consistent issue creation
- Type hints throughout

**Estimated Time**: 1-2 hours

**Completion Notes**:
- Abstract base class fully implemented (188 lines)
- All helper methods present and tested
- 3 concrete evaluators already successfully built on this base:
  - LengthEvaluator (20+ passing tests)
  - ParagraphEvaluator (22+ passing tests)
  - DictionaryEvaluator (21+ passing tests)
- Production-ready with excellent documentation

---

### Phase 1A Task Order

**Recommended sequence for implementation**:

1. **Task 1.1**: Core Data Models (basic version) - CRITICAL - 3 hours
2. **Task 1.3**: File I/O Utilities (basic version) - 2 hours
3. **Task 1.2**: Configuration Management (basic version) - 2 hours
4. **Task 1.4**: Test Fixtures (basic version) - 1.5 hours
5. **Task 1.5**: Base Evaluator Class - Already complete ✓

**Total Time**: ~8.5 hours

**Rationale**:
- Task 1.1 first (models are foundation for everything)
- Task 1.3 before 1.2 (config loading may use file I/O utilities)
- Task 1.4 last (needs models and file I/O to create proper fixtures)
- Task 1.5 already done
- Task 1.6 and prompt features deferred to Phase 1B

---

## Phase 2: Evaluation System (MAIN FOCUS)

Build evaluators one at a time, using the first as a template for others.

---

### Phase 2 Progress Status

**Overall**: 9 of 9 tasks complete (100%) ✅

| Task | Status | Time | Notes |
|------|--------|------|-------|
| 2.1 Length Evaluator | ✓ Complete | 3-4h | 20+ tests, standalone CLI, 281 lines |
| 2.2 Paragraph Evaluator | ✓ Complete | 2-3h | 22+ tests, handles newlines, 231 lines |
| 2.3 Dictionary Evaluator | ✓ Complete | 4-5h | 21+ tests, bilingual dicts, 377 lines |
| 2.4 Completeness Evaluator | ✓ Complete | 2h | 60+ tests, 298 lines, placeholder/truncation/marker checks |
| 2.5 Glossary Evaluator | ✓ Complete | 3.5h | 31 tests, 362 lines, word boundary matching |
| 2.6 Evaluation Runner | ✓ Complete | 3h | 56 tests, factory + runners + aggregation |
| 2.7 Evaluation Reports | ✓ Complete | 4h | 32 tests, text/JSON/HTML, 580 lines reporting.py |
| 2.8 CLI Commands | ✓ Complete | 1.5h | Simple evaluate_chunk.py script, 350 lines |
| 2.9 Integration Tests | ✓ Complete | (included) | 13 integration tests covering full pipeline |

**Completed**: ~24.5 hours
**Remaining**: 0 hours - Phase 2 complete! ✅

**Recommended Task Order**:
1. **Task 2.6** (Evaluation Runner) - CRITICAL PATH - enables running multiple evaluators together
2. **Task 2.7** (Evaluation Reports) - Essential for user feedback and debugging
3. **Task 2.4** (Completeness Evaluator) - Quick win, follows established patterns
4. **Task 2.8** (Unified CLI) - Better UX, can leverage existing standalone scripts
5. **Task 2.5** (Glossary Evaluator) - Can be deferred, basic support exists
6. **Task 2.9** (Integration Tests) - Final validation after all components complete

---

### Task 2.1: Length Evaluator (TEMPLATE)
**File**: `src/evaluators/length_eval.py`
**Priority**: HIGH
**Dependencies**: Task 1.1, 1.5
**Status**: ✓ COMPLETE

Build complete reference implementation:

**Checks**:
- Compare source vs translation word/char counts
- Spanish expected to be 1.1x-1.3x longer (configurable)
- Error if < 0.5x or > 2.0x
- Warning if outside expected range

**Implementation**:
- `LengthEvaluator(BaseEvaluator)`
- `_count_words(text: str) -> int`
- `_calculate_ratio(source: int, target: int) -> float`
- `_determine_severity(ratio: float, config: dict) -> str`

**Tests** (`tests/test_evaluators/test_length_eval.py`):
- Test with good translation (passes)
- Test with too-short translation (error)
- Test with too-long translation (error)
- Test with slightly off translation (warning)
- Test with empty text (error)

**CLI Integration**:
- Add command: `book-translate eval <project> <chunk> --eval length`

**Acceptance Criteria**:
- Passes all tests
- Clear, actionable error messages
- Configurable thresholds
- CLI command works
- Well-documented code

**Estimated Time**: 3-4 hours

**Completion Notes**:
- **File**: `src/evaluators/length_eval.py` (281 lines)
- **Tests**: `tests/test_evaluators/test_length_eval.py` (325 lines, **20+ passing tests**)
- **CLI**: Standalone script `evaluate_chunk_length.py` with formatted output
- **Features**:
  - Configurable word/character counting modes
  - Three severity levels (ERROR, WARNING, INFO)
  - Quality scoring (0.0-1.0 scale) based on ratio deviation
  - Detailed issue messages with specific recommendations
  - Handles edge cases: empty text, whitespace-only, multiline
  - Real-world test examples from Don Quixote
- **Code Quality**: Excellent - comprehensive type hints, docstrings, robust error handling
- **Test Coverage**: Complete - unit tests for all methods, integration tests, edge cases
- All acceptance criteria met and exceeded (includes testing and CLI)

---

### Task 2.2: Paragraph Evaluator
**File**: `src/evaluators/paragraph_eval.py`
**Priority**: HIGH
**Dependencies**: Task 2.1 (use as template)
**Status**: ✓ COMPLETE

**Checks**:
- Count paragraphs in source and translation
- Verify paragraph boundaries preserved
- Check for merged or split paragraphs

**Implementation**:
- Follow same structure as `length_eval.py`
- `_count_paragraphs(text: str) -> int`
- `_find_paragraph_boundaries(text: str) -> list[int]`
- `_compare_structure(source_paras: int, target_paras: int) -> Issue | None`

**Tests**:
- Matching paragraph counts (pass)
- Fewer paragraphs (error)
- More paragraphs (error)
- Different newline styles (handle gracefully)

**Acceptance Criteria**:
- Follows pattern from length_eval
- Handles different newline conventions (\n, \r\n)
- Clear error messages showing paragraph mismatch

**Estimated Time**: 2-3 hours

**Completion Notes**:
- **File**: `src/evaluators/paragraph_eval.py` (231 lines)
- **Tests**: `tests/test_evaluators/test_paragraph_eval.py` (385 lines, **22+ passing tests**)
- **CLI**: Standalone script `evaluate_chunk_paragraph.py`
- **Features**:
  - Handles multiple newline conventions (\n, \r\n, \r, mixed)
  - Robust detection of paragraph boundaries (multiple blank lines treated as single boundary)
  - Configurable mismatch tolerance (allow_mismatch parameter)
  - Quality scoring based on paragraph count match
  - Detailed error messages showing expected vs actual paragraph counts
  - Edge case handling: single paragraph, whitespace-only text, excessive blank lines
- **Test Coverage**: Comprehensive - Windows/Unix newlines, real-world examples (Don Quixote 3-paragraph sample)
- **Code Quality**: Excellent - follows established evaluator pattern, well-documented
- All acceptance criteria met

---

### Task 2.3: Dictionary Evaluator (Most Complex)
**File**: `src/evaluators/dictionary_eval.py`
**Priority**: HIGH
**Dependencies**: Task 2.1, PyEnchant installation
**Status**: ✓ COMPLETE

**Checks**:
- All words in translated file exist in Spanish dictionary
- No English words in translation
- Flag remaining words (that aren't in either dictionary or in the glossary, if provided)
- Handle common contractions and special cases
- For each instance of each flagged word, provide the character position in the translated file

**Implementation**:
- `DictionaryEvaluator(BaseEvaluator)`
- Initialize Spanish and English dictionaries
- `_tokenize(text: str) -> list[str]` (words only, no punctuation)
- `_check_spanish_word(word: str) -> bool`
- `_check_english_word(word: str) -> bool`
- `_is_proper_noun(word: str) -> bool`
- Handle: numbers, URLs, emails, special characters

**Tests**:
- All valid Spanish words (pass)
- Text with English words (error)
- Text with misspellings (warning)
- Text with proper nouns (info)
- Mixed case handling
- Punctuation handling

**Setup**:
- Add `pyenchant` to requirements.txt
- Document installation: `pip install pyenchant`
- Test dictionary availability

**Acceptance Criteria**:
- Accurately detects English words
- Minimizes false positives on proper nouns
- Provides suggestions for misspellings
- Handles edge cases (URLs, numbers, etc.)

**Estimated Time**: 4-5 hours

**Completion Notes**:
- **File**: `src/evaluators/dictionary_eval.py` (377 lines)
- **Tests**: `tests/test_evaluators/test_dictionary_eval.py` (380 lines, **21+ passing tests**)
- **CLI**: Standalone script `evaluate_chunk_dictionary.py` with glossary support
- **Features**:
  - **Bilingual dictionary checking** using both es_ES (Spain) and es_MX (Mexican Spanish) via PyEnchant
  - English word detection (ERROR severity)
  - Unknown word detection (WARNING severity with spelling suggestions)
  - **Character position reporting** for every flagged word instance
  - Glossary integration (exclusion list for proper nouns/technical terms)
  - Optional case-sensitive mode
  - Sophisticated tokenization (handles accented chars, hyphens, apostrophes, punctuation)
  - Proper noun heuristics (capitalized words)
  - Numbers and single characters ignored appropriately
- **Test Coverage**: Comprehensive - English detection, misspellings, proper nouns, glossary exclusions, accented characters, position tracking, Spanish variants
- **Code Quality**: Excellent - most sophisticated evaluator, handles complex linguistic edge cases
- All acceptance criteria exceeded (includes character positions, spelling suggestions, bilingual support)

---

### Task 2.4: Completeness Evaluator
**File**: `src/evaluators/completeness_eval.py`
**Priority**: MEDIUM
**Dependencies**: Task 2.1
**Status**: ✓ COMPLETE

**Checks**:
- Translation is not None or empty
- Translation is not placeholder text
- No suspicious truncation (ends mid-sentence)
- Special markers preserved (e.g., "---", "* * *")

**Implementation**:
- `CompletenessEvaluator(BaseEvaluator)`
- `_is_empty(text: str) -> bool`
- `_check_placeholders(text: str, custom_patterns: list[str]) -> list[Issue]`
- `_check_truncation(text: str) -> Issue | None`
- `_check_markers(source: str, translation: str, strict: bool) -> list[Issue]`
- `_calculate_score(issues: list[Issue]) -> float`

**Tests**:
- Complete translation (pass)
- Empty translation (error)
- Placeholder text (error) - 11 placeholder patterns supported
- Truncated text (warning) - checks proper ending punctuation
- Missing special markers (warning or error based on strict mode)
- 60+ comprehensive tests covering all scenarios
- Manual validation script with 6 test groups

**Acceptance Criteria**:
- Catches obviously incomplete translations
- Detects common placeholder patterns (TODO, FIXME, [TRANSLATION], etc.)
- Identifies truncation indicators (missing punctuation)
- Preserves special markers (---, * * *, markdown, lists)
- Configurable strict mode for markers
- Custom placeholder patterns support

**Estimated Time**: 2-3 hours
**Actual Time**: ~2 hours

**Completion Notes**:
- Comprehensive evaluator with 298 lines of well-structured code
- Extensive placeholder detection (11+ built-in patterns, supports custom)
- Intelligent truncation detection (Spanish punctuation aware: . ! ? … » " ) ] —)
- Special marker checking for section breaks, headers, lists
- Configurable via context dict (strict_markers, custom_placeholders, check_markers)
- Score calculation: -0.3 per error, -0.1 per warning, -0.05 per info
- 60+ tests in test_completeness_eval.py covering all edge cases
- Manual validation script passes all 6 test groups (43 individual tests)
- Integrated into evaluator registry and available via CLI

---

### Task 2.5: Glossary Evaluator
**File**: `src/evaluators/glossary_eval.py`
**Priority**: HIGH
**Dependencies**: Task 2.1, 1.3 (file_io for glossary loading)
**Status**: ✓ COMPLETE

**Checks**:
- Glossary terms translated as specified
- No mixing of alternative translations
- Case-sensitive matching where appropriate
- Character names consistently translated

**Implementation**:
- `GlossaryEvaluator(BaseEvaluator)`
- Load glossary from project config
- `_find_term_occurrences(text: str, term: str) -> list[int]` (positions)
- `_check_term_translation(source: str, translation: str, term: GlossaryTerm) -> Issue | None`
- Handle partial matches and plural forms

**Tests**:
- Correct glossary usage (pass)
- Wrong translation of glossary term (error)
- Missing glossary term in translation (error)
- Inconsistent translation across chunk (error)

**Acceptance Criteria**:
- Accurately detects glossary violations
- Handles plural forms and variations
- Clear messages showing expected vs actual
- Supports glossary alternatives

**Estimated Time**: 3-4 hours

**Completion Notes**:
- **File**: `src/evaluators/glossary_eval.py` (382 lines)
- **Tests**: `tests/test_evaluators/test_glossary_eval.py` (532 lines, **31 passing tests**)
- **CLI**: `evaluate_chunk_glossary.py` (140 lines, Windows-compatible)
- **Features**:
  - Exact word boundary matching (e.g., "Bennet" vs "Bennett")
  - Case-insensitive term detection for flexibility
  - Multi-word term support (e.g., "Mr. Bennet", "Elizabeth Bennet")
  - Primary + alternative translation validation
  - Consistency checking (warns if mixing alternatives in same chunk)
  - Character position tracking for all occurrences
  - Quality scoring with penalties for inconsistency
  - Handles edge cases: missing glossary, terms not in source, count mismatches
- **Critical regex fix**: Added word boundary `\b` for multi-word terms to prevent false positives
- **Code Quality**: Most sophisticated evaluator, excellent pattern matching, comprehensive tests
- All acceptance criteria exceeded (includes character positions, alternatives, consistency)

---

### Task 2.6: Evaluation Runner
**File**: `src/evaluators/__init__.py`
**Priority**: HIGH
**Dependencies**: All evaluators above
**Status**: ✓ COMPLETE

Create orchestration functions:

```python
def get_evaluator(name: str) -> BaseEvaluator:
    """Factory function to get evaluator by name"""

def run_evaluators(
    chunk: Chunk,
    evaluators: list[str],
    context: dict
) -> list[EvalResult]:
    """Run multiple evaluators on a chunk"""

def run_all_evaluators(
    chunk: Chunk,
    config: EvaluationConfig,
    context: dict
) -> list[EvalResult]:
    """Run all enabled evaluators"""

def aggregate_results(results: list[EvalResult]) -> dict:
    """Summarize results across all evaluators"""
```

**Acceptance Criteria**:
- Dynamic evaluator loading
- Run evaluators in sequence
- Aggregate results clearly
- Handle evaluator failures gracefully

**Estimated Time**: 2-3 hours

**Completion Notes**:
- **File**: `src/evaluators/__init__.py` (397 lines)
- **Tests**: `tests/test_evaluation_runner.py` (749 lines, **56 passing tests**)
- **Components Implemented**:
  - `_EVALUATOR_REGISTRY` - Maps evaluator names to classes
  - `get_evaluator()` - Factory function with error handling (11 tests)
  - `run_evaluator()` - Single evaluator runner with exception catching (11 tests)
  - `_build_context()` - Context builder from EvaluationConfig + Glossary
  - `run_evaluators()` - Multi-evaluator runner (7 tests)
  - `run_all_evaluators()` - Config-driven runner (6 tests)
  - `aggregate_results()` - Result aggregation with comprehensive stats (12 tests)
  - **9 integration tests** with real Pride & Prejudice fixtures
- **Features**:
  - Robust error handling - failed evaluators don't stop pipeline
  - Graceful DictionaryEvaluator initialization handling
  - Comprehensive result aggregation (pass/fail, issues by severity/evaluator, average scores)
  - Full integration with EvaluationConfig
  - Context building with glossary support
- **Code Quality**: Production-ready, comprehensive error handling, excellent test coverage
- **Result**: **CRITICAL PATH COMPLETE** - Tasks 2.7 (Reports) and 2.8 (CLI) now unblocked

---

### Task 2.7: Evaluation Reports
**File**: `src/evaluators/reporting.py`
**Priority**: MEDIUM
**Dependencies**: Task 2.6
**Status**: ✓ COMPLETE

Generate human-readable reports:

**Formats**:
- **Text**: Console-friendly output
- **JSON**: Machine-readable for tooling
- **HTML**: Rich formatted report with color coding

**Functions**:
- `generate_text_report(results: list[EvalResult]) -> str`
- `generate_json_report(results: list[EvalResult]) -> str`
- `generate_html_report(results: list[EvalResult], output_path: Path)`

**Report Contents**:
- Overall pass/fail status
- Summary by evaluator
- Issue breakdown by severity
- Details for each issue with location
- Suggestions for fixes
- Timestamp and metadata

**Acceptance Criteria**:
- Clear, actionable reports
- Color-coded terminal output (using Rich)
- HTML report is well-formatted and readable
- JSON is valid and complete

**Estimated Time**: 3-4 hours

**Completion Notes**:
- **Files created**:
  - `src/evaluators/reporting.py` (580 lines) - Three report generators with helper functions
  - `src/utils/file_io.py` (added 160 lines) - Three save functions for reports
  - `tests/test_reporting.py` (660 lines, **32 passing tests**)
  - `manual_test_reports.py` (161 lines) - Manual validation script
- **Features implemented**:
  - **Text reports**: Rich-formatted with color-coded severity levels, tables, panels
  - **JSON reports**: Complete structured data with datetime serialization, Unicode preservation
  - **HTML reports**: Self-contained with embedded CSS, responsive design, color-coded issues
  - **Report saving**: Timestamped filenames, atomic writes, automatic directory creation
  - Helper functions: severity formatting, HTML escaping, timestamp formatting
- **Test coverage**: Complete - helper functions, all three formats, save functions, edge cases, integration tests
- **Manual validation**: Successfully generated reports from both good and error fixtures
- All acceptance criteria exceeded (includes tests, save functions, manual validation script)

---

### Task 2.8: CLI Evaluation Commands
**File**: `evaluate_chunk.py`
**Priority**: HIGH
**Dependencies**: Task 2.6, 2.7
**Status**: ✓ COMPLETE

**Simplified Approach**: Instead of building a complex Click-based CLI framework, implemented a simple, direct evaluation script that meets actual user needs.

**Final Command Structure:**
```bash
# Basic usage - run all evaluators
python evaluate_chunk.py chunk.json

# With glossary
python evaluate_chunk.py chunk.json --glossary glossary.json

# Specific evaluators
python evaluate_chunk.py chunk.json --evaluators length,paragraph

# Different formats
python evaluate_chunk.py chunk.json --format html --output report.html
python evaluate_chunk.py chunk.json --format all --output reports/
```

**Acceptance Criteria:**
- ✓ Run multiple evaluators in one command
- ✓ Generate combined reports (text/JSON/HTML)
- ✓ Good error messages with helpful suggestions
- ✓ Comprehensive help text (`--help`)
- ✓ Exit codes reflect pass/fail status

**Estimated Time**: 1.5 hours (vs. 3-4 hours for full CLI framework)

**Completion Notes**:
- **File**: `evaluate_chunk.py` (350 lines)
- **Features**:
  - Simple argparse-based interface (no extra dependencies)
  - Runs all 4 evaluators: length, paragraph, dictionary, glossary
  - Evaluator selection via `--evaluators` flag
  - Report format selection: text, json, html, or all
  - Glossary support with automatic glossary evaluator enable/disable
  - Rich-formatted output (integrates Task 2.7 reporting)
  - Windows console encoding fix for Unicode characters
  - Comprehensive error handling with user-friendly messages
  - File validation (missing files, invalid JSON, unknown evaluators)
  - Timestamped report generation when using `--format all`
- **Testing**:
  - ✓ Tested with good fixture (passes all evaluators)
  - ✓ Tested with error fixture (shows detailed issues)
  - ✓ Tested with glossary (runs glossary evaluator)
  - ✓ Tested HTML format output
  - ✓ Tested all formats generation
  - ✓ Tested specific evaluator selection
  - ✓ Tested error handling (invalid evaluator names)
- **Documentation**:
  - ✓ Updated README.md with usage examples
  - ✓ Built-in `--help` documentation
  - ✓ Updated project status section
- **Design Decision**: Chose simplicity over complexity - user needs were met with a 350-line script instead of a multi-module CLI framework, saving ~3 hours of development time while providing full functionality

---

### Task 2.9: Integration Tests
**File**: `tests/test_evaluation_runner.py`, `tests/test_reporting.py`
**Priority**: MEDIUM
**Dependencies**: All Phase 2 tasks
**Status**: ✓ COMPLETE

End-to-end tests:
- ✓ Run all evaluators on good translation (all pass)
- ✓ Run all evaluators on bad translation (appropriate failures)
- ✓ Generate all report formats
- ✓ CLI commands work correctly (manually tested)
- N/A State is updated properly (not applicable for simplified CLI approach)

**Acceptance Criteria**:
- ✓ Full pipeline works without errors
- ✓ Results match expectations
- ✓ Reports are generated correctly

**Estimated Time**: 2-3 hours

**Completion Notes**:
- **13 integration tests** spread across two test files:
  - `test_evaluation_runner.py`: 9 tests covering full evaluation pipeline
    - Good translation passes all evaluators
    - Error translation fails appropriately
    - Full pipeline with aggregation
    - Real Pride & Prejudice fixtures
    - Glossary integration
    - Comparing good vs error chunks
  - `test_reporting.py`: 4 tests covering all report formats
    - Text reports with real fixtures
    - JSON reports with error-filled fixtures
    - HTML reports with good translations
    - Saving all three formats
- **CLI testing**: `evaluate_chunk.py` manually tested with all options
  - Subprocess-based CLI tests not implemented (not critical)
  - Script works correctly with real fixtures
- **Time**: Included in evaluator and reporting development

---

## Phase 1B: Prompt Infrastructure (BEFORE PHASE 4)

**Status**: ✅ COMPLETE

**When to Start**: After Phase 2 is complete, before beginning Phase 4 (Translation)

**Why Separate?**: Phase 2 (Evaluation) doesn't use prompts - only Phase 4 (Translation) needs prompt infrastructure. This phase adds simple template and version tracking for LLM-based translation.

**What's Added**:
- Simple translation prompt template (no Jinja2, just {{variable}} substitution)
- Version tracking models (PromptMetadata, StyleGuide)
- Template rendering and formatting functions
- Manual version management (user maintains version numbers in files)

**Dependencies**: None (uses Python standard library)

**Time Estimate**: 2-2.5 hours
**Actual Time**: ~2 hours

**Design Philosophy**: Keep it simple. User manually maintains version numbers in glossary/style guide files. System just tracks which versions were used for each chunk translation.

**Completion Summary**:
- ✅ Translation template created (116 lines, 4738 characters)
- ✅ PromptMetadata and StyleGuide models added to src/models.py
- ✅ Chunk and TranslationConfig models updated with new fields
- ✅ 5 I/O functions added to src/utils/file_io.py
- ✅ 3 test fixtures created (template, style guide, updated chunk)
- ✅ 42 new tests written (19 prompt template tests + 23 style guide tests)
- ✅ All 227 tests passing
- ✅ Ready for Phase 4 (Translation) implementation

---

### Task 1.6 (Revised): Create Translation Template
**File**: `prompts/translation.txt`
**Priority**: HIGH
**Dependencies**: None
**Phase**: 1B
**Status**: ✓ COMPLETE

**Purpose**: Create a simple, production-ready translation prompt template with variable placeholders.

**File to Create**:

**`prompts/translation.txt`** - Main translation prompt
- Use simple `{{variable}}` placeholders (Python string.Template style)
- Variables: `{{book_title}}`, `{{source_text}}`, `{{target_language}}`, `{{glossary}}`, `{{style_guide}}`, `{{context}}`, etc.
- Clear instructions for preserving paragraph structure
- Well-commented sections explaining each part
- Keep length reasonable (<2000 tokens when rendered)

**Template Example Structure**:
```
# Translation Prompt for {{book_title}}

You are translating a classic book from English to {{target_language}}.

## Source Text
{{source_text}}

## Glossary Terms
{{glossary}}

## Style Guide
{{style_guide}}

## Context
{{context}}

## Instructions
[Translation instructions here...]
```

**Acceptance Criteria**:
- Uses simple {{variable}} syntax (no conditionals, no logic)
- Well-commented to explain each section
- All standard variables documented in comments
- Tested with sample data

**Estimated Time**: 30 minutes

**Completion Notes**:
- **File created**: `prompts/translation.txt` (116 lines, 4738 characters)
- **Test fixture**: `tests/fixtures/prompts/translation.txt` (same content)
- **Variables documented**: 8 core variables (book_title, source_text, target_language, etc.)
- **Sections**: 7 main sections (task, source, glossary, style guide, context, instructions, output format)
- **Features**: Clear instructions, paragraph preservation guidelines, completeness requirements
- **Version**: 1.0 (documented in header comments)

---

### Task 1.1b (Revised): Add Version Tracking Models
**File**: `src/models.py`
**Priority**: HIGH
**Dependencies**: Task 1.1 (basic models)
**Phase**: 1B
**Status**: ✓ COMPLETE

**Add to existing models.py**:

1. **`PromptMetadata`** - Track versions used for translation
   ```python
   class PromptMetadata(BaseModel):
       template_version: str  # e.g., "1.0"
       glossary_version: str  # e.g., "2.3"
       style_guide_version: str  # e.g., "1.1"
       timestamp: datetime
   ```

2. **`StyleGuide`** - Style guide with manual version tracking
   ```python
   class StyleGuide(BaseModel):
       content: str  # The actual style guide text
       version: str = "1.0"  # Manually maintained by user
       created_at: datetime
       updated_at: datetime
   ```

3. **Update `Chunk`** - Add prompt metadata field
   - Add `prompt_metadata: Optional[PromptMetadata] = None`

4. **Update `TranslationConfig`** - Add style guide path
   - Add `style_guide_path: Optional[str] = None`

**Note**: Glossary model already has `version` field (from Task 1.1), no changes needed.

**Version Management Philosophy**:
- User manually updates version strings in files when content changes
- System records which versions were used for each chunk
- User uses git or own notes to track what changed between versions
- Simple version strings: "1.0", "1.1", "2.0", etc.

**Acceptance Criteria**:
- Models integrate cleanly with existing code
- Backward compatible (prompt_metadata is optional)
- Version fields are simple strings (user-maintained)

**Estimated Time**: 30 minutes

**Completion Notes**:
- **PromptMetadata model** added (lines 222-241): 4 fields (template_version, glossary_version, style_guide_version, timestamp)
- **StyleGuide model** added (lines 244-262): 4 fields (content, version, created_at, updated_at)
- **Chunk model** updated (line 94): Added `prompt_metadata: Optional[PromptMetadata]` field
- **TranslationConfig model** updated (line 308): Added `style_guide_path: Optional[str]` field
- **All imports working**: Models can be imported and instantiated successfully
- **Backward compatible**: Existing code continues to work, new fields are optional
- **Tests verified**: Models tested in test_models.py and test_style_guide.py

---

### Task 1.3b (Revised): Template & Style Guide I/O Functions
**File**: `src/utils/file_io.py`
**Priority**: HIGH
**Dependencies**: Task 1.3 (basic file I/O), Task 1.1b (models), Task 1.6 (template)
**Phase**: 1B
**Status**: ✓ COMPLETE

**Add to existing file_io.py**:

**Template Functions**:

1. **`load_prompt_template() -> str`**
   - Load template from `prompts/translation.txt`
   - Returns template as plain string
   - Raise error if file not found

2. **`render_prompt(template: str, variables: dict[str, Any]) -> str`**
   - Use Python's `string.Template` for simple substitution
   - Replace all `{{variable}}` placeholders with values
   - Raise error if required variable is missing
   - Returns rendered prompt as string

3. **`format_glossary_for_prompt(glossary: Glossary) -> str`**
   - Convert Glossary model to human-readable text format
   - Group terms by type (character, place, concept, etc.)
   - Include alternatives if present
   - Format example:
     ```
     CHARACTER NAMES:
     - Harry Potter → Harry Potter (alternatives: none)

     PLACE NAMES:
     - Hogwarts → Hogwarts
     ```

**Style Guide Functions**:

4. **`load_style_guide(path: Path) -> StyleGuide`**
   - Load StyleGuide from JSON file
   - Parse using Pydantic model

5. **`save_style_guide(style_guide: StyleGuide, path: Path) -> None`**
   - Save StyleGuide to JSON file
   - Use atomic write for safety

**Usage Example**:
```python
# Load template and data
template = load_prompt_template()
glossary = load_glossary(glossary_path)
style_guide = load_style_guide(style_guide_path)

# Prepare variables
variables = {
    "book_title": "Pride and Prejudice",
    "source_text": chunk.source_text,
    "target_language": "Spanish",
    "glossary": format_glossary_for_prompt(glossary),
    "style_guide": style_guide.content,
    "context": "Regency-era romance novel"
}

# Render prompt
prompt = render_prompt(template, variables)

# Create metadata to track versions
metadata = PromptMetadata(
    template_version="1.0",  # Manually maintained in template comments
    glossary_version=glossary.version,
    style_guide_version=style_guide.version,
    timestamp=datetime.now()
)

# Store in chunk
chunk.prompt_metadata = metadata
```

**Acceptance Criteria**:
- Template loading is straightforward (just read file)
- Rendering uses simple string substitution (no complex parsing)
- Glossary formatting is human-readable and well-structured
- Style guide I/O follows same patterns as existing file_io functions
- Error handling for missing files and variables

**Estimated Time**: 1 hour

**Completion Notes**:
- **5 functions added to src/utils/file_io.py** (lines 167-374):
  1. `load_prompt_template()` - Loads template from file (default or custom path)
  2. `render_prompt()` - Simple {{variable}} substitution using regex and string.replace()
  3. `format_glossary_for_prompt()` - Groups terms by type with alternatives
  4. `load_style_guide()` - Loads StyleGuide from JSON (follows load_glossary pattern)
  5. `save_style_guide()` - Saves with atomic write (follows save_glossary pattern)
- **Imports added**: `re` module for variable detection, `Optional` for type hints, `StyleGuide` model
- **Error handling**: FileNotFoundError, KeyError for missing variables, JSONDecodeError
- **19 tests** in test_prompt_template.py verify template/rendering functions
- **23 tests** in test_style_guide.py verify StyleGuide I/O and model

---

### Task 1.4b (Revised): Add Test Fixtures
**Directory**: `tests/fixtures/`
**Priority**: MEDIUM
**Dependencies**: Task 1.4 (basic fixtures), Task 1.6 (template), Task 1.1b (models)
**Phase**: 1B
**Status**: ✓ COMPLETE

**Add to existing fixtures**:

1. **`prompts/translation.txt`** - Test template with sample content
   - Include all standard variables
   - Add version comment: `# Template Version: 1.0`

2. **`style_guide_sample.json`** - Sample StyleGuide
   ```json
   {
     "content": "TONE: Formal but accessible\nFORMALITY: Medium-high\nDIALECT: Neutral Spanish",
     "version": "1.0",
     "created_at": "2025-10-30T10:00:00",
     "updated_at": "2025-10-30T10:00:00"
   }
   ```

3. **Update `chunk_translated_good.json`** - Add prompt_metadata field
   ```json
   {
     "prompt_metadata": {
       "template_version": "1.0",
       "glossary_version": "1.0",
       "style_guide_version": "1.0",
       "timestamp": "2025-10-30T17:15:00"
     }
   }
   ```

**Acceptance Criteria**:
- Template fixture uses simple {{variable}} syntax
- StyleGuide fixture has realistic content and version
- Chunk fixture includes valid prompt_metadata
- All fixtures use consistent versioning

**Estimated Time**: 30 minutes

**Completion Notes**:
- **3 fixtures created/updated**:
  1. `tests/fixtures/prompts/translation.txt` - Full template (116 lines, identical to production)
  2. `tests/fixtures/style_guide_sample.json` - Sample style guide with realistic content
  3. `tests/fixtures/chunk_translated_good.json` - Updated with prompt_metadata field
- **All fixtures tested**: Load successfully, validate correctly, work with I/O functions
- **Version consistency**: All use version "1.0" for initial release
- **Unicode support**: StyleGuide fixture includes Spanish characters to verify encoding

---

### Phase 1B Testing

**New Tests Required**:

**`tests/test_prompt_template.py`** (new file):
- Test `load_prompt_template()` - loads file correctly
- Test `render_prompt()` - substitutes all variables
- Test `render_prompt()` - errors on missing required variable
- Test `format_glossary_for_prompt()` - produces readable output
- Test template rendering with all variables
- Test template rendering with empty glossary

**`tests/test_style_guide.py`** (new file):
- Test `load_style_guide()` - loads valid JSON
- Test `save_style_guide()` - saves correctly
- Test StyleGuide model validation
- Test version field is preserved

**Add to `tests/test_models.py`**:
- Test PromptMetadata model creation
- Test Chunk with prompt_metadata field
- Test prompt_metadata is optional (backward compatible)

**Estimated Time**: Included in task estimates above (30 min spread across tasks)

**Completion Notes**:
- **test_prompt_template.py created**: 19 tests covering loading, rendering, formatting
  - 4 tests for load_prompt_template()
  - 7 tests for render_prompt()
  - 6 tests for format_glossary_for_prompt()
  - 2 integration tests for full workflow
- **test_style_guide.py created**: 23 tests covering I/O and model validation
  - 7 tests for load_style_guide()
  - 8 tests for save_style_guide()
  - 6 tests for StyleGuide model
  - 2 integration tests for create/save/load workflow
- **All tests passing**: 227 total tests (42 new for Phase 1B)
- **Test coverage**: Template loading, rendering, glossary formatting, style guide I/O, atomic writes, Unicode handling, error cases

---

## Phase 1B Summary

**Total Time**: 2-2.5 hours (vs original 5-6 hours)

**Complexity Reduction**:
- ❌ No Jinja2 dependency
- ❌ No context generation prompt
- ❌ No project override system
- ❌ No template resolution logic
- ❌ No changelog tracking (user manages via git)
- ❌ No automatic version incrementing
- ✅ Simple {{variable}} substitution
- ✅ Manual version management by user
- ✅ Track which versions were used per chunk
- ✅ Uses only Python standard library

**User Workflow**:
1. User edits glossary → manually changes `version: "1.1"` to `"1.2"`
2. User edits style guide → manually changes version field
3. System records those version numbers when translating chunks
4. User can see which versions were used for any chunk
5. User uses git diff to see what actually changed between versions

**Benefits**:
- Much simpler to implement and maintain
- No complex dependencies
- User has full control over versioning
- Still provides full traceability of what versions were used
- Fits naturally with git-based workflow

---

## Phase 3: Chunking & Combination (READY TO START)

**Goal**: Implement the system to split chapters into translation-sized chunks with overlaps, and later recombine translated chunks back into complete chapters.

**Current State**:
- ✅ Data models complete (Chunk, ChunkMetadata, ChunkingConfig)
- ✅ File I/O functions ready (load_chunk, save_chunk)
- ✅ Test fixtures available (chapter_sample.txt from Pride & Prejudice)
- ✅ Configuration system in place

**Dependencies**: Phase 1A, Phase 1B complete

**Total Time Estimate**: 9-11 hours (4 main tasks)

---

### Phase 3 Progress Status

**Overall**: 0 of 4 tasks complete (0%)

| Task | Status | Time | Priority |
|------|--------|------|----------|
| 3.1 Paragraph Boundary Detection | ⏳ Next | 2-3h | CRITICAL |
| 3.2 Chunking Engine | ⏳ Pending | 4-5h | CRITICAL |
| 3.3 Chunk Combiner | ⏳ Pending | 3-4h | HIGH |
| 3.4 CLI Commands | ⏳ Pending | 1-2h | HIGH |

**Recommended Task Order**: 3.1 → 3.2 → 3.4 → 3.3

**Note**: Overlap similarity checking and manual review generation deferred to Phase 5 (Advanced Features).

---

### Task 3.1: Paragraph Boundary Detection
**File**: `src/utils/text_utils.py` (new file)
**Priority**: CRITICAL
**Dependencies**: None
**Status**: ⏳ Next

**Purpose**: Create utility functions for detecting and handling paragraph boundaries in text.

**Functions to Implement**:

1. **`detect_paragraph_boundaries(text: str) -> list[int]`**
   - Find all paragraph boundaries in text
   - Paragraph = separated by double newline (`\n\n`)
   - Return list of character positions where paragraphs start
   - Handle mixed newline styles (`\n`, `\r\n`, `\r`)
   - Normalize multiple blank lines (treat `\n\n\n` as single boundary)

2. **`extract_paragraphs(text: str) -> list[str]`**
   - Split text into individual paragraphs
   - Preserve original whitespace within paragraphs
   - Strip leading/trailing whitespace from each paragraph
   - Skip empty paragraphs

3. **`count_paragraphs(text: str) -> int`**
   - Count number of paragraphs in text
   - Handle edge cases: empty text, single paragraph, no newlines

4. **`count_words(text: str) -> int`**
   - Count words using simple whitespace splitting
   - Consistent with evaluator word counting
   - Handle multi-language text (Spanish accents, etc.)

5. **`normalize_newlines(text: str) -> str`**
   - Convert all newlines to `\n` for consistency
   - Handle `\r\n` (Windows), `\r` (old Mac), `\n` (Unix)

**Edge Cases to Handle**:
- Empty text
- Text with no paragraph breaks
- Text with excessive blank lines
- Mixed newline conventions (Windows + Unix)
- Trailing/leading whitespace
- Paragraphs with different indentation

**Tests** (`tests/test_text_utils.py`):
- Test paragraph boundary detection on multi-paragraph text
- Test with Windows-style newlines (`\r\n`)
- Test with Unix-style newlines (`\n`)
- Test with mixed newline styles
- Test with excessive blank lines (3+ newlines)
- Test empty text handling
- Test single paragraph (no breaks)
- Test word counting with Spanish text (accented characters)
- Test word counting matches evaluator behavior
- Integration test with Pride & Prejudice fixture

**Acceptance Criteria**:
- Accurately detects all paragraph boundaries
- Handles all newline conventions
- Word counting matches evaluator methods
- Comprehensive tests (>15 tests)
- Well-documented with docstrings and examples

**Estimated Time**: 2-3 hours

---

### Task 3.2: Chunking Engine
**File**: `src/chunker.py` (new file)
**Priority**: CRITICAL
**Dependencies**: Task 3.1 (text_utils)
**Status**: ⏳ Pending

**Purpose**: Core chunking logic to divide chapters into translation-sized chunks with intelligent overlap.

**Main Function**:

**`chunk_chapter(chapter_text: str, config: ChunkingConfig, chapter_id: str = "chapter_01") -> list[Chunk]`**

**Algorithm**:
```
1. Normalize newlines in chapter_text
2. Extract paragraphs using extract_paragraphs()
3. Initialize: current_chunk_paragraphs = [], chunks = []
4. For each paragraph:
   a. Add paragraph to current_chunk_paragraphs
   b. Calculate current chunk word count
   c. If word count >= target_size OR (word count >= min_chunk_size AND at end):
      - Create chunk from current_chunk_paragraphs
      - Calculate overlap with previous chunk (if exists)
      - Generate chunk metadata (char positions, word count, etc.)
      - Add chunk to chunks list
      - Keep overlap paragraphs for next chunk (dual-constraint: see below)
      - Reset current_chunk_paragraphs with overlap paragraphs
5. Handle remaining paragraphs (create final chunk)
6. Validate chunk sizes (warn if < min or > max)
7. Return list of Chunk objects
```

**Dual-Constraint Overlap Strategy** (KEY FEATURE):

The overlap must satisfy BOTH conditions:
1. At least `config.overlap_paragraphs` paragraphs (e.g., 2 paragraphs)
2. At least `config.min_overlap_words` words (e.g., 100 words)

**Rationale**: Short dialogue paragraphs (5-10 words each) need more paragraphs to provide adequate context. Conversely, one long paragraph (200+ words) may be sufficient overlap.

**Example Scenarios**:

**Scenario A - Long paragraphs** (overlap_paragraphs=2, min_overlap_words=100):
```
P1: 200 words
P2: 150 words
→ Overlap = [P1, P2] (2 paragraphs = 350 words) ✓ Both conditions met
```

**Scenario B - Short dialogue** (overlap_paragraphs=2, min_overlap_words=100):
```
P1: "Hello," said John. (5 words)
P2: "How are you?" Mary replied. (6 words)
P3: "Fine, thanks," he answered. (5 words)
P4: The conversation continued... (50 words)
P5: Eventually they reached... (60 words)
→ Overlap = [P1, P2, P3, P4, P5] (5 paragraphs = 126 words) ✓ Reached word minimum
```

**Supporting Functions**:

1. **`_calculate_overlap(prev_paragraphs: list[str], config: ChunkingConfig) -> list[str]`**
   - Take paragraphs from end of previous chunk
   - Continue until BOTH conditions met:
     - At least `config.overlap_paragraphs` paragraphs
     - At least `config.min_overlap_words` words
   - Return paragraphs to include at start of next chunk

2. **`_calculate_chunk_metadata(paragraphs: list[str], char_start: int, overlap_prev: int, overlap_next: int) -> ChunkMetadata`**
   - Calculate: char_start, char_end, overlap_start, overlap_end
   - Count paragraphs and words
   - Build ChunkMetadata object

3. **`_generate_chunk_id(chapter_id: str, position: int) -> str`**
   - Format: `{chapter_id}_chunk_{position:03d}`
   - Example: `"chapter_01_chunk_003"`

4. **`_validate_chunk_size(chunk: Chunk, config: ChunkingConfig) -> list[str]`**
   - Check if chunk is within min/max bounds
   - Return list of warning messages
   - Examples: "Chunk too small (300 words < 500 min)"

**Metadata Tracking**:
- `char_start`: Character position in original chapter (0-indexed)
- `char_end`: End character position in original chapter
- `overlap_start`: How many characters at start are overlap from previous chunk
- `overlap_end`: How many characters at end will be overlap for next chunk
- `paragraph_count`: Total paragraphs in this chunk
- `word_count`: Total words in source_text

**Edge Cases**:
- Chapter shorter than min_chunk_size → Single chunk with warning
- Last chunk smaller than min_chunk_size → Keep it separate with warning
- Single paragraph exceeds max_chunk_size → Warn but keep it (paragraph splitting is Phase 5)
- config.overlap_paragraphs = 0 AND min_overlap_words = 0 → No overlap, chunks are independent
- Overlap requirements exceed available paragraphs → Use all available paragraphs as overlap

**Required Update to ChunkingConfig**:

Add new field to `src/models.py`:
```python
class ChunkingConfig(BaseModel):
    """Configuration for chunking chapters."""
    method: ChunkingMethod = ChunkingMethod.PARAGRAPH
    target_size: int = Field(default=2000, ge=100, description="Target words per chunk")
    overlap_paragraphs: int = Field(default=2, ge=0, le=5, description="Minimum paragraphs of overlap")
    min_overlap_words: int = Field(default=100, ge=0, description="Minimum words in overlap")  # NEW FIELD
    min_chunk_size: int = Field(default=500, ge=50, description="Minimum words per chunk")
    max_chunk_size: int = Field(default=3000, ge=100, description="Maximum words per chunk")
```

**Tests** (`tests/test_chunker.py`):
- Test chunking Pride & Prejudice Chapter 1 (113 lines, ~2000 words)
- Test with different target_size values (500, 1000, 2000)
- Test with different overlap_paragraphs (0, 1, 2, 3)
- Test with different min_overlap_words (0, 50, 100, 200)
- Test dual-constraint overlap with long paragraphs (meets paragraph count)
- Test dual-constraint overlap with short dialogue (meets word count)
- Test chunk ID generation format
- Test metadata calculation (char positions, counts, overlap sizes)
- Test overlap appears correctly in adjacent chunks
- Test edge case: very short chapter (< min_chunk_size)
- Test edge case: single paragraph chapter
- Test edge case: zero overlap (both constraints = 0)
- Test edge case: single long paragraph > max_chunk_size (warn but keep)
- Test validation warnings (chunks too small/large)
- Integration test: chunk → save → load → verify integrity

**Acceptance Criteria**:
- Chunks respect target_size, min_chunk_size, max_chunk_size
- Dual-constraint overlap works correctly (takes max of both requirements)
- Overlaps correctly calculated and stored in metadata
- Chunk IDs are sequential and properly formatted
- All metadata fields accurately calculated
- Can chunk real-world text (Pride & Prejudice chapter)
- >20 comprehensive tests
- Well-documented code with docstrings and examples

**Estimated Time**: 4-5 hours

---

### Task 3.3: Chunk Combiner
**File**: `src/combiner.py` (new file)
**Priority**: HIGH
**Dependencies**: Task 3.2 (chunker)
**Status**: ⏳ Pending

**Purpose**: Merge translated chunks back into complete chapters using "use_previous" overlap resolution strategy.

**Main Function**:

**`combine_chunks(chunks: list[Chunk]) -> str`**

**Algorithm**:
```
1. Sort chunks by position to ensure correct order
2. Validate completeness (no missing chunks, all translated)
3. Initialize: chapter_text = ""
4. For first chunk:
   - Add entire translated_text to chapter_text
5. For each subsequent chunk:
   - Extract overlap size from metadata.overlap_start
   - Remove overlap characters from start of chunk's translated_text
   - Append remaining text to chapter_text
6. Return combined chapter_text
```

**"use_previous" Strategy**:
```
Chunk 1: "...end of chunk text" + [OVERLAP: "shared overlapping text"]
Chunk 2: [OVERLAP: "shared overlapping text"] + "beginning of next text..."

Combined Result:
"...end of chunk text" + "shared overlapping text" + "beginning of next text..."
                         └─── kept from chunk 1 ───┘  └─── from chunk 2 ───┘
                              (discarded from chunk 2)
```

**Rationale**: Translator has more context at the END of a chunk (they've been reading it) than at the START of the next chunk (they're just beginning). Therefore, trust the overlap translation from the chunk that ends with it.

**Supporting Functions**:

1. **`validate_chunk_completeness(chunks: list[Chunk]) -> tuple[bool, list[str]]`**
   - Check for gaps in sequence (position 0, 1, 2, 4 → missing 3)
   - Check all chunks have same chapter_id
   - Check all chunks have translations (translated_text is not None/empty)
   - Return (is_valid, list_of_error_messages)

2. **`_remove_start_overlap(text: str, overlap_chars: int) -> str`**
   - Remove first N characters from text
   - Used to get non-overlapping portion of subsequent chunks
   - Handle edge case: overlap_chars > len(text)

**Edge Cases**:
- Single chunk → Return translated_text as-is
- Missing chunk in sequence → Error, cannot combine
- Chunk with no translation → Error, cannot combine
- Chunk with empty translation → Error, cannot combine
- overlap_start = 0 (no overlap) → Just concatenate chunks
- overlap_start > length of translated_text → Error, invalid metadata

**Tests** (`tests/test_combiner.py`):
- Test combining 2 chunks with overlap (use_previous strategy)
- Test combining 3+ chunks with varying overlap sizes
- Test single chunk (no combination needed)
- Test validation catches missing chunks (gaps in sequence)
- Test validation catches untranslated chunks
- Test validation catches empty translations
- Test validation catches mismatched chapter_ids
- Test zero-overlap combination (just concatenation)
- Test edge case: overlap_start = 0 for first chunk
- Test edge case: chunks in wrong order (combiner should sort)
- Integration test: chunk Pride & Prejudice → mock translate → combine → verify structure
- Integration test: create translated chunks from fixtures, combine, check coherence

**Acceptance Criteria**:
- Correctly combines multiple chunks into coherent chapter
- No duplicate text from overlaps (using use_previous strategy)
- Validation catches all incomplete chunk set scenarios
- Clear error messages for validation failures
- Handles edge cases gracefully
- >15 comprehensive tests
- Well-documented with examples

**Estimated Time**: 3-4 hours

---

### Task 3.4: CLI Commands for Chunking
**File**: `chunk_chapter.py` and `combine_chunks.py` (new scripts)
**Priority**: HIGH
**Dependencies**: Task 3.2 (chunker), Task 3.3 (combiner)
**Status**: ⏳ Pending

**Purpose**: Create simple CLI scripts for chunking and combining operations, following the standalone script pattern from Phase 2.

**Script 1: chunk_chapter.py**

**Command Structure**:
```bash
# Basic usage - chunk with default config
python chunk_chapter.py chapter.txt --chapter-id chapter_01

# With project config (loads ChunkingConfig from config.json)
python chunk_chapter.py chapter.txt --config projects/my_book/config.json

# Specify chunking parameters directly
python chunk_chapter.py chapter.txt --target-size 1500 --overlap 2 --min-overlap-words 150

# Specify output directory
python chunk_chapter.py chapter.txt --output chunks/original/

# Show detailed statistics
python chunk_chapter.py chapter.txt --verbose
```

**Arguments**:
- `chapter_file` (positional, required): Path to chapter text file
- `--chapter-id`: Chapter identifier (default: derived from filename, e.g., "chapter_01" from "chapter_01.txt")
- `--config`: Path to project config.json (uses ChunkingConfig section)
- `--target-size`: Override target words per chunk (default: 2000)
- `--overlap`: Override overlap paragraphs (default: 2)
- `--min-overlap-words`: Override minimum overlap words (default: 100)
- `--min-chunk-size`: Override minimum chunk size (default: 500)
- `--max-chunk-size`: Override maximum chunk size (default: 3000)
- `--output`: Output directory for chunk JSON files (default: ./chunks/)
- `--verbose`: Show detailed chunking statistics

**Output**:
- Save each chunk as JSON in output directory
- Filename format: `{chapter_id}_chunk_{position:03d}.json`
- Print summary:
  ```
  Chunking complete!
  Chapter: chapter_01 (5,234 words, 42 paragraphs)

  Created 3 chunks:
    - chapter_01_chunk_001: 1,845 words, 15 paragraphs (overlap: 2 para, 187 words)
    - chapter_01_chunk_002: 1,923 words, 14 paragraphs (overlap: 2 para, 165 words)
    - chapter_01_chunk_003: 1,466 words, 13 paragraphs (no overlap)

  Output directory: chunks/original/
  ```

**Script 2: combine_chunks.py**

**Command Structure**:
```bash
# Combine chunks back into chapter
python combine_chunks.py chunks/translated/chapter_01_chunk_*.json --output chapter_01_translated.txt

# Explicit chunk list
python combine_chunks.py chunk_001.json chunk_002.json chunk_003.json --output chapter.txt

# Show detailed combination info
python combine_chunks.py chunks/translated/ch01*.json --output chapter.txt --verbose
```

**Arguments**:
- `chunk_files` (positional, required): Glob pattern or list of chunk JSON file paths
- `--output`: Output file for combined chapter (required)
- `--verbose`: Show detailed combination statistics

**Output**:
- Save combined chapter text to output file
- Print summary:
  ```
  Combining chunks...
  Loaded 3 chunks for chapter_01

  Validation: ✓ Passed
    - All chunks present (positions 0-2)
    - All chunks translated
    - Same chapter_id: chapter_01

  Combining with "use_previous" strategy:
    - Chunk 1: 1,845 words (full text)
    - Chunk 2: 1,758 words (165 words overlap removed)
    - Chunk 3: 1,279 words (187 words overlap removed)

  Combined chapter: 4,882 words
  Output: chapter_01_translated.txt
  ```

**Error Handling**:
- File not found → Clear error message with path
- Invalid JSON → Show which chunk file is malformed and error details
- Missing chunks in sequence → List missing chunk IDs
- Untranslated chunks → List chunk IDs without translations
- Invalid config → Show validation errors with suggestions
- Glob pattern matches no files → Error with pattern shown

**Tests**:
- Manual testing with Pride & Prejudice fixture
- Test chunk_chapter.py with various parameters
- Test combine_chunks.py with glob patterns
- Test error handling (missing files, invalid JSON, incomplete sequences)
- Integration test: CLI chunk → save → CLI combine → verify output

**Acceptance Criteria**:
- Simple, user-friendly interface (following evaluate_chunk.py pattern)
- Comprehensive --help text for both scripts
- Clear progress and error messages
- Works with real fixtures (Pride & Prejudice)
- Handles glob patterns correctly
- Documented with usage examples

**Estimated Time**: 1-2 hours

---

### Integration Testing for Phase 3

**Full Pipeline Test** (`tests/test_chunking_pipeline.py`):

**Test Scenarios**:

1. **`test_full_chunking_pipeline_pride_and_prejudice()`**
   - Load Pride & Prejudice Chapter 1 fixture
   - Chunk with default config (2000 words, 2 para overlap, 100 word min)
   - Verify chunk count, sizes, overlaps
   - Save chunks to temp directory
   - Mock translate each chunk (simple transformation)
   - Load translated chunks
   - Combine back into chapter
   - Verify combined text structure (paragraph count matches original)

2. **`test_short_chapter_single_chunk()`**
   - Create chapter < min_chunk_size (300 words)
   - Chunk it
   - Should produce 1 chunk
   - Combine should return same text

3. **`test_dialogue_heavy_chapter()`**
   - Create chapter with many short dialogue paragraphs
   - Chunk with overlap_paragraphs=2, min_overlap_words=100
   - Verify overlap contains >2 paragraphs (to meet word requirement)

4. **`test_zero_overlap_configuration()`**
   - Chunk chapter with overlap_paragraphs=0, min_overlap_words=0
   - Verify chunks have no overlap (metadata.overlap_start/end = 0)
   - Combine should simply concatenate

5. **`test_high_overlap_configuration()`**
   - Chunk with overlap_paragraphs=3, min_overlap_words=200
   - Verify large overlaps stored correctly
   - Combine handles large overlaps correctly

**Acceptance Criteria**:
- All integration tests pass
- Full pipeline works end-to-end
- Handles various chapter sizes and configurations
- Validates data integrity throughout pipeline

**Estimated Time**: Included in task estimates above

---

### Test Fixtures for Phase 3

**Existing Fixtures** (already available):
- `tests/fixtures/chapter_sample.txt` - Pride & Prejudice Chapter 1 (113 lines, ~2000 words)

**New Fixtures to Create**:

1. **`tests/fixtures/chapter_short.txt`** - 300 word chapter
   - Tests single chunk scenario (< min_chunk_size)
   - 5-6 paragraphs

2. **`tests/fixtures/chapter_dialogue.txt`** - Dialogue-heavy chapter
   - Many short dialogue paragraphs (5-10 words each)
   - Tests dual-constraint overlap (needs more paragraphs to meet word count)
   - ~1000 words, 30+ paragraphs

3. **`tests/fixtures/translated_chunks/`** directory
   - `chapter_01_chunk_001.json` - Translated chunk from Pride & Prejudice
   - `chapter_01_chunk_002.json` - Translated chunk 2
   - `chapter_01_chunk_003.json` - Translated chunk 3 (if needed)
   - Used for combination testing without actual translation

**Estimated Time**: 30 minutes (included in Task 3.2/3.3 estimates)

---

## Phase 3 Summary

**Total Time**: 9-11 hours (down from original 11-14 hours)

**Time Breakdown**:
- Task 3.1 (Text Utils): 2-3 hours
- Task 3.2 (Chunking Engine with dual-constraint overlap): 4-5 hours
- Task 3.3 (Combiner with use_previous strategy): 3-4 hours
- Task 3.4 (CLI Commands): 1-2 hours

**Deferred to Phase 5 (Advanced Features)**:
- Overlap similarity checking (difflib-based comparison)
- Manual review report generation
- Advanced resolution strategies beyond "use_previous"

**Key Design Decisions**:
1. **Dual-constraint overlap**: Handles both long paragraphs and short dialogue effectively
2. **use_previous strategy**: Trust overlap from chunk that ends with it (more context)
3. **Standalone CLI scripts**: Simple, direct, following Phase 2 pattern
4. **Defer complexity**: Similarity checking not needed for initial release

**Deliverables**:
- ✅ Paragraph boundary detection utilities
- ✅ Core chunking engine with intelligent overlap
- ✅ Chunk combination with overlap resolution
- ✅ CLI commands for chunking and combining
- ✅ Comprehensive tests (>50 tests total)
- ✅ Integration tests for full pipeline
- ✅ Test fixtures for various scenarios

**Ready to Start**: All dependencies met (Phase 1A, 1B complete)

**Next Steps After Phase 3**:
- **Phase 4**: Translation Interface (API translator, manual workbook generator)
- **Phase 5**: Advanced Features (grammar evaluator, overlap similarity, LLM evaluators)

---

## Phase 4: Translation Interface ✅ COMPLETE (3/3 tasks complete)

### Task 4.1: Manual Workbook Generator ✅ COMPLETE
**File**: `src/translator.py`
**Priority**: HIGH (after Phase 3)
**Status**: ✓ COMPLETE (2.5 hours)

Generate formatted document for manual translation.

**Completion Notes**:
- Implemented `generate_workbook()` with complete prompt rendering
- Added helper functions for header, instructions, glossary, style guide sections
- Integrated with Phase 1B prompt infrastructure
- CLI script `generate_workbook.py` with verbose mode and comprehensive help
- 25+ comprehensive tests covering all functionality
- UTF-8 encoding support for Spanish characters
- Sample workbook generated from Pride & Prejudice fixture (9.8KB)

**Key Features**:
- Each chunk includes complete rendered translation prompt
- Glossary and style guide automatically formatted
- Clear "PASTE TRANSLATION HERE" sections
- Metadata preserved for parser
- Works with any LLM (Claude.ai, ChatGPT, etc.)

**Estimated Time**: 3-4 hours
**Actual Time**: 2.5 hours

---

### Task 4.2: Workbook Parser ✅ COMPLETE
**File**: `src/translator.py`
**Priority**: HIGH (after Task 4.1)
**Status**: ✓ COMPLETE (3 hours)

Parse workbook to extract translations.

**Completion Notes**:
- Implemented 7 parsing functions: parse_workbook(), validate_workbook_structure(), import_translations(), update_chunk_with_translation(), extract_chunk_id_from_metadata(), clean_translation_text(), is_placeholder_text()
- Regex-based parsing (no external dependencies)
- Comprehensive structure validation
- Text cleaning: smart quotes → straight quotes, line ending normalization
- Placeholder detection ([TRANSLATION], TODO, etc.)
- CLI script `import_workbook.py` with validate-only mode
- 22+ comprehensive tests including round-trip verification
- Integration with Phase 1 (load_chunk, save_chunk)

**Key Features**:
- Automatic chunk file detection from workbook
- UTF-8 support for Spanish characters
- Preserves all original chunk metadata
- Clear warnings for missing translations
- Round-trip workflow verified: generate → fill → import → verify ✓

**Test Fixtures Created**:
- workbook_completed_good.md (with valid Spanish translation)
- workbook_completed_missing.md (with empty translation)
- workbook_completed_placeholder.md (with placeholder text)

**Estimated Time**: 3-4 hours
**Actual Time**: 3 hours

**Manual Translation Workflow Status**: ✅ FULLY FUNCTIONAL

---

### Task 4.3: API Translator ✅ COMPLETE
**Files**: `src/api_translator.py`, `translate_api.py`
**Priority**: MEDIUM (after Task 4.1)
**Status**: ✓ COMPLETE (5 hours)

Implement API-based translation with Anthropic Claude and OpenAI GPT.

**Completion Notes**:
- Core API module `src/api_translator.py` (~650 lines)
  - Real-time translation: `translate_chunk_realtime()` with automatic retries
  - Batch submission: `submit_batch()` for both Anthropic and OpenAI
  - Batch retrieval: `retrieve_batch_results()` when jobs complete
  - Cost estimation: `estimate_cost()` before translation
  - Job tracking: `save_batch_job()`, `load_batch_jobs()`, `get_batch_job()`
  - Error handling for rate limits, auth errors, API errors
- CLI script `translate_api.py` (~650 lines)
  - Real-time and batch modes via `--batch` flag
  - Cost estimation with confirmation prompts
  - Progress bars and real-time status updates
  - Batch job management: `--check-batch`, `--list-batches`
  - Support for glossary and style guide
- Comprehensive test suite: 22 tests with mocked API responses
  - All tests passing (13 passed, 9 skipped without API libs)
  - Tests for cost estimation, retries, batch tracking
  - No real API calls in tests

**Key Features**:
- **Dual mode**: Real-time (immediate) and batch (50% cheaper, ~24h processing)
- **Cost estimation** before translation with optional budget limits
- **Automatic retries** with exponential backoff for rate limits
- **Progress tracking** with rich console output
- **Batch tracking** via JSON file for long-running jobs
- **Same prompt quality** as manual workflow (uses same template system)
- **Multi-provider**: Anthropic (Claude 3.5 Sonnet/Haiku) and OpenAI (GPT-4o/GPT-4o-mini)

**Configuration Added**:
- `.env.example` - API key configuration template
- Updated `.gitignore` - Excludes `.env` and `batch_jobs.json`
- Updated `requirements.txt` - Added anthropic, openai, python-dotenv
- Updated `README.md` - Complete API translation workflow documentation

**Estimated Time**: 4-5 hours
**Actual Time**: 5 hours

**Phase 4 Status**: ✅ ALL TASKS COMPLETE
- Manual translation workflow: ✅ Fully functional
- API translation workflow: ✅ Fully functional
- Total Phase 4 tests: 60+ (47 translator + 22 API tests = 69 tests)

---

## Phase 5: Advanced Evaluators (PLANNED)

**Goal**: Enhance translation quality assurance with specialized evaluators for grammar, forbidden terms, and subjective quality assessment.

**Current State**:
- ✅ Foundation evaluators complete (5 evaluators: length, paragraph, dictionary, completeness, glossary)
- ✅ Evaluation runner and reporting infrastructure in place
- ✅ Test fixtures and CLI ready

**Dependencies**: Phase 2 complete

**Total Time Estimate**: 10-12 hours (3 main tasks)

---

### Phase 5 Progress Status

**Overall**: 1 of 3 tasks complete (33%)

| Task | Status | Time | Priority |
|------|--------|------|----------|
| 5.1 Grammar Evaluator | 🚧 In Progress | 4-5h | MEDIUM |
| 5.2 Blacklist Evaluator | ✅ Complete | 2.5h | MEDIUM |
| 5.3 LLM Evaluators | ⏳ Pending | 4-5h | LOW |

**Current Task**: 5.1 Grammar Evaluator

---

### Task 5.1: Grammar Evaluator
**File**: `src/evaluators/grammar_eval.py`
**Priority**: MEDIUM
**Dependencies**: None (uses LanguageTool library)
**Status**: 🚧 In Progress

**Purpose**: Use LanguageTool to check for grammar, spelling, and style issues in Spanish translations. Provides context-aware checking beyond simple dictionary lookups.

**Checks**:
- Grammar errors (verb conjugation, agreement, gender, tense)
- Spelling mistakes (context-aware, beyond dictionary)
- Style issues (redundancy, wordiness, clarity)
- Punctuation errors (comma usage, quotes, etc.)
- Regional dialect consistency (if configured)

**Implementation**:
- `GrammarEvaluator(BaseEvaluator)`
- Initialize LanguageTool with Spanish dialect (default: 'es')
- `_check_grammar(text: str) -> list[Match]` - Run LanguageTool check
- `_convert_match_to_issue(match: Match) -> Issue` - Convert LT match to Issue
- `_determine_severity(category: str, rule_id: str) -> IssueLevel` - Map LT categories to severity
- `_should_ignore_match(match: Match, context: dict) -> bool` - Filter based on glossary/rules
- `_extract_word_from_match(match: Match) -> str` - Extract flagged word for glossary check

**Severity Mapping**:
- `GRAMMAR` → ERROR
- `TYPOS` → ERROR (can be disabled with ignore_categories)
- `STYLE` → WARNING
- `PUNCTUATION` → WARNING
- `TYPOGRAPHY` → INFO
- `REDUNDANCY` → INFO

**Configuration Options** (via context dict):
- `dialect`: Spanish dialect - 'es' (default), 'es-MX', 'es-ES', 'es-AR', etc.
- `glossary`: Glossary object - excludes glossary terms from TYPOS category
- `ignore_rules`: list[str] - Specific LanguageTool rule IDs to skip
- `ignore_categories`: list[str] - Categories to skip (e.g., ['TYPOS'] to disable spelling)
- `skip_spelling`: bool - Convenience flag, equivalent to ignore_categories=['TYPOS']
- `max_issues`: int - Limit number of issues reported (default: 50)

**Tests** (`tests/test_evaluators/test_grammar_eval.py`):
- Test with grammatically correct Spanish (pass, no issues)
- Test with verb conjugation errors (error - "Darcy fueron")
- Test with gender agreement errors (error - "La Señor Bennet")
- Test with spelling mistakes (error - unknown words)
- Test with style issues (warning - redundancy)
- Test with glossary terms (no false positives on spelling)
- Test glossary + grammar (still catches agreement errors with glossary terms)
- Test dialect switching (es vs es-MX)
- Test ignore_categories (skip TYPOS)
- Test skip_spelling convenience flag
- Test ignore_rules (specific rule IDs)
- Test max_issues limit
- Test severity mapping (GRAMMAR→ERROR, STYLE→WARNING, etc.)
- Test character position tracking
- Integration test with real Spanish text

**Setup**:
- Add `language-tool-python` to requirements.txt
- Note: First run downloads LanguageTool JAR (~200MB)
- Graceful handling if LanguageTool not available (similar to enchant)

**Acceptance Criteria**:
- Accurately detects grammar, spelling, and style errors
- Maps LanguageTool Match objects to Issue format
- Respects glossary terms (no TYPOS for glossary words)
- Grammar checks work regardless of glossary (catches "Darcy fueron")
- Configurable severity levels via category mapping
- Clear suggestions from LanguageTool included
- skip_spelling flag works correctly
- ignore_categories works correctly
- >15 comprehensive tests (all with mocked LanguageTool or real library)
- Well-documented with examples

**Estimated Time**: 4-5 hours

---

### Task 5.2: Blacklist Evaluator
**File**: `src/evaluators/blacklist_eval.py`
**Priority**: MEDIUM
**Dependencies**: None
**Status**: ✅ COMPLETE

**Completed**: 2025-11-14
**Actual Time**: 2.5 hours

**Completion Summary**:
- ✅ Implemented BlacklistEvaluator (~220 lines) in src/evaluators/blacklist_eval.py
- ✅ Added BlacklistEntry and Blacklist models to src/models.py
- ✅ Created 3 test fixtures (blacklist_sample.json, 2 chunk fixtures)
- ✅ Wrote 24 comprehensive tests (all passing)
- ✅ Registered in evaluator registry
- ✅ Supports variations list for handling conjugations/plurals
- ✅ Configurable severity levels (error/warning/info)
- ✅ Word boundary protection (coger won't match recoger)
- ✅ Case-sensitive/insensitive matching
- ✅ Character position tracking
- ✅ Fixed dictionary_eval.py to defer enchant import errors

**Purpose**: Ensure translations do not contain forbidden words or phrases from a predefined blacklist.

**Use Cases**:
- Prevent offensive or inappropriate language
- Enforce translator style preferences (e.g., avoid certain colloquialisms)
- Block specific terms that should use glossary translations instead
- Ensure formality level (block informal slang in formal translations)

**Checks**:
- Scan translated text for exact blacklist matches
- Support case-sensitive and case-insensitive matching
- Support whole-word matching (avoid partial matches)
- Support phrase matching (multi-word blacklist entries)
- Track all occurrences with character positions
- Configurable severity per blacklist entry

**Implementation**:
- `BlacklistEvaluator(BaseEvaluator)`
- `_load_blacklist(blacklist_path: Path) -> list[BlacklistEntry]`
- `_find_blacklist_matches(text: str, entry: BlacklistEntry) -> list[int]`
- `_check_word_boundaries(text: str, match_pos: int, term: str) -> bool`
- Check both the base `term` and all `variations` for matches

**Blacklist Model** (add to `src/models.py`):
```python
class BlacklistEntry(BaseModel):
    term: str  # The base forbidden word/phrase (used in issue messages)
    variations: list[str] = []  # All forms to match (conjugations, plurals, etc.)
    reason: str  # Why it's forbidden (for issue message)
    severity: str = "error"  # error, warning, or info
    case_sensitive: bool = False
    whole_word: bool = True  # Require word boundaries
    alternatives: list[str] = []  # Suggested replacements
```

**Matching Logic**:
- The evaluator will search for the base `term` AND all `variations`
- If `variations` is empty, only the base `term` is checked
- All matched words are reported as the base `term` in issues

**Blacklist File Format** (`blacklist.json`):
```json
{
  "entries": [
    {
      "term": "zumo",
      "variations": ["zumo", "zumos"],
      "reason": "Use 'jugo' instead for Latin American Spanish",
      "severity": "error",
      "case_sensitive": false,
      "whole_word": true,
      "alternatives": ["jugo", "jugos"]
    },
    {
      "term": "coger",
      "variations": ["coger", "coge", "cojo", "coges", "cogió", "cogería", "cogían", "coja", "cojan", "cogiendo"],
      "reason": "Offensive in Latin America - use 'tomar', 'agarrar' instead",
      "severity": "warning",
      "case_sensitive": false,
      "whole_word": true,
      "alternatives": ["tomar", "agarrar"]
    },
    {
      "term": "bastardo",
      "variations": ["bastardo", "bastardos", "bastarda", "bastardas"],
      "reason": "Offensive language not appropriate for this translation",
      "severity": "error",
      "case_sensitive": false,
      "whole_word": true,
      "alternatives": ["canalla", "bribón"]
    },
    {
      "term": "tío",
      "variations": ["tío", "tíos"],
      "reason": "Informal slang - use 'hombre' or 'tipo' instead",
      "severity": "warning",
      "case_sensitive": false,
      "whole_word": true,
      "alternatives": ["hombre", "tipo"]
    }
  ],
  "version": "1.0"
}
```

**Note on Exclusions**: To avoid matching "recoger" when blacklisting "coger", simply don't include "recoger" forms in the variations list. The `whole_word: true` setting ensures "coger" won't match the "coger" inside "recoger" since they have different word boundaries.

**Tests** (`tests/test_evaluators/test_blacklist_eval.py`):
- Test with no blacklist matches (pass)
- Test with exact term match (base term only, no variations)
- Test with variation match (matches "cogió" from "coger" variations)
- Test with multiple variation matches (finds "coge", "cogió", "cogería" in same text)
- Test case-sensitive matching
- Test case-insensitive matching
- Test whole-word matching (avoid partial matches like "coger" in "recoger")
- Test phrase matching (multi-word entries)
- Test multiple occurrences of same term/variation
- Test character position tracking for all matches
- Test severity levels (error, warning, info)
- Test alternatives in suggestions
- Test empty blacklist (pass)
- Test empty variations list (only checks base term)
- Test with fixture translations
- Integration test with real Spanish text containing conjugations

**CLI Integration**:
- Add to `evaluate_chunk.py`: `--blacklist blacklist.json`
- Auto-enable BlacklistEvaluator when blacklist file provided

**Acceptance Criteria**:
- Accurately detects all blacklist terms and their variations
- Respects case sensitivity and word boundary settings
- Character position tracking for all matches
- Clear error messages with reasons and alternatives
- Handles multi-word phrases correctly
- Avoids false positives (partial word matches like "coger" in "recoger")
- Reports all variations using the base term name in issues
- Empty variations list works (only checks base term)
- >15 comprehensive tests including variation matching
- Well-documented with examples

**Estimated Time**: 2-3 hours

---

### Task 5.3: LLM Evaluators
**File**: `src/evaluators/llm_eval.py`
**Priority**: LOW
**Dependencies**: Phase 4 (API infrastructure)
**Status**: ⏳ Pending

**Purpose**: Use LLM (Claude, GPT) for subjective quality assessment that rule-based evaluators can't catch.

**Checks**:
- Translation accuracy and faithfulness to source
- Natural flow and readability in target language
- Cultural appropriateness and localization
- Tone consistency with source material
- Idiomatic expression usage
- Overall translation quality score

**Implementation**:
- `LLMEvaluator(BaseEvaluator)`
- Reuse API infrastructure from Phase 4 (`src/api_translator.py`)
- `_build_evaluation_prompt(chunk: Chunk) -> str`
- `_parse_llm_response(response: str) -> list[Issue]`
- `_extract_quality_score(response: str) -> float`
- Support multiple providers (Anthropic, OpenAI)
- Caching to avoid re-evaluating unchanged chunks

**Evaluation Prompt Structure**:
```
You are a professional translation quality evaluator.

SOURCE TEXT (English):
{source_text}

TRANSLATION (Spanish):
{translated_text}

Evaluate this translation on:
1. Accuracy - Does it convey the same meaning?
2. Naturalness - Does it read naturally in Spanish?
3. Tone - Does it match the source tone?
4. Cultural Appropriateness - Is it properly localized?

For each issue found, provide:
- Severity (error/warning/info)
- Location (approximate position or quote)
- Description of the problem
- Suggested improvement

Provide an overall quality score (0.0-1.0).
```

**Configuration Options** (via context dict):
- `provider`: "anthropic" or "openai"
- `model`: Model name (e.g., "claude-3-5-haiku-20241022")
- `temperature`: 0.0-1.0 (default: 0.3 for consistency)
- `cache_results`: Whether to cache evaluations (default: true)
- `cost_limit`: Maximum cost per evaluation (default: $0.10)

**Tests** (`tests/test_evaluators/test_llm_eval.py`):
- Test with mocked LLM response (good translation)
- Test with mocked LLM response (issues found)
- Test prompt building
- Test response parsing
- Test quality score extraction
- Test caching mechanism
- Test cost limiting
- Test error handling (API failures)
- Test both Anthropic and OpenAI
- Integration test with real fixture (mocked API)

**Cost Considerations**:
- Use cheaper models (Haiku, GPT-4o-mini) for evaluation
- Cache results to avoid re-evaluation
- Estimate cost before running (warn user)
- Batch evaluations when possible

**Acceptance Criteria**:
- Generates effective evaluation prompts
- Parses LLM responses into Issue format
- Extracts quality scores reliably
- Caching reduces redundant API calls
- Cost estimation and limiting works
- Supports both Anthropic and OpenAI
- >12 comprehensive tests (all with mocked APIs)
- Well-documented with examples

**Estimated Time**: 4-5 hours

---

## Phase 6: Polish (FUTURE)

- Comprehensive documentation
- User guide
- Example workflows
- Error message improvements
- Performance optimization

---

## Quick Start Checklist

When starting a new session, have Claude Code:

1. ✓ Read `DESIGN.md` for architecture
2. ✓ Read `IMPLEMENTATION_PLAN.md` (this file) for current tasks
3. ✓ Check `src/models.py` for data structures
4. ✓ Review relevant evaluator as reference
5. Start work on next task

---

## Development Guidelines

### Code Style
- Use type hints everywhere
- Write docstrings for all public functions
- Follow PEP 8
- Use descriptive variable names
- Add comments for complex logic

### Testing
- Write tests before or alongside implementation
- Aim for >80% coverage on core modules
- Use real examples from public domain books
- Test edge cases and error conditions

**Phase 1A Tests**:
- Basic model validation tests
- Config loading/saving tests
- File I/O tests (chunks, glossaries, state)
- Fixture validation tests

**Prompt System Tests** (deferred to Phase 1B):
- `tests/test_prompt_resolution.py` - Template resolution
- `tests/test_prompt_rendering.py` - Template rendering
- `tests/test_config.py` (enhanced) - Prompt config functions

### Git Commits
- Commit after each completed task
- Use descriptive commit messages
- Reference task numbers (e.g., "Task 2.1: Implement length evaluator")

### Claude Code Workflow
1. Read DESIGN.md at session start
2. Pick next task from this plan
3. Read relevant dependencies (models, base classes)
4. Implement feature
5. Write tests
6. Update this plan with ✓ when complete
7. Commit changes

---

## Current Status

**✅ COMPLETED: Phase 4 - Translation Interface**

**Recent Achievements**:
- **Phase 4 Complete**: All translation workflows implemented (Nov 2025)
  - **Task 4.1**: Manual Workbook Generator (2.5 hours) ✅
  - **Task 4.2**: Workbook Parser (3 hours) ✅
  - **Task 4.3**: API Translator with batch support (5 hours) ✅
  - Manual translation workflow fully functional
  - API translation workflow fully functional (Anthropic + OpenAI)
  - 69 comprehensive tests (47 translator + 22 API tests)

- **Phase 3 Complete**: Chunking & combination system (Nov 2025)
  - Text utilities, chunking engine, chunk combiner
  - CLI scripts (chunk_chapter.py, combine_chunks.py)
  - Dual-constraint overlap strategy
  - 60+ comprehensive tests

- **Phase 2 Complete**: Full evaluation system
  - 5 evaluators (length, paragraph, dictionary, completeness, glossary)
  - Comprehensive reporting (text/JSON/HTML)
  - Unified CLI (evaluate_chunk.py)
  - 242+ passing tests

- **Phase 1 Complete**: Foundation and prompt infrastructure
  - Core models, config, file I/O
  - Translation template system
  - Style guide support
  - 65+ passing tests

**→ NEXT RECOMMENDED TASKS**:

**Option A: Begin Phase 5 (Advanced Evaluators)** - RECOMMENDED FOR NEW FEATURES
- **Task 5.1**: Grammar Evaluator (4-5h)
  - Use LanguageTool for comprehensive grammar checking
  - Would enhance quality assurance capabilities
- **Task 5.2**: Blacklist Evaluator (2-3h)
  - Prevent forbidden words/phrases in translations
  - Enforce style preferences and formality levels
- **Task 5.3**: LLM Evaluators (4-5h)
  - Use LLM for subjective quality assessment
  - More nuanced quality metrics

**Option B: Production Hardening** - RECOMMENDED FOR REAL USE
- Error handling improvements
- More comprehensive documentation
- Performance optimization
- User guide and example workflows
- Deployment documentation

**Option C: Real-World Testing** - RECOMMENDED FIRST STEP
- Test complete workflow on actual book chapters
- Identify pain points and edge cases
- Gather requirements for Phase 5 features

**Progress Summary**:
- **Phase 1**: ✅ Complete (5+4 tasks, ~10.5 hours)
- **Phase 2**: ✅ Complete (9/9 tasks, ~25 hours)
- **Phase 3**: ✅ Complete (4/4 tasks, ~11 hours)
- **Phase 4**: ✅ Complete (3/3 tasks, ~10.5 hours)
- **Total Implementation**: ~57 hours
- **Total Tests**: 407+ passing tests
- **Status**: 🎉 Full translation pipeline operational! Ready for production use or Phase 5 enhancements!

---

**Document Version**: 2.1
**Last Updated**: 2025-11-13
**Current Phase**: Phase 5 Planning Complete - Ready for Implementation
**Phase 1 Status**: ✅ Complete (9/9 tasks, ~10.5 hours)
**Phase 2 Status**: ✅ Complete (9/9 tasks, ~25 hours)
**Phase 3 Status**: ✅ Complete (4/4 tasks, ~11 hours)
**Phase 4 Status**: ✅ Complete (3/3 tasks, ~10.5 hours)
**Phase 5 Status**: 📋 Planned (0/3 tasks, ~10-12 hours estimated)
**Total Tests**: 407+ passing
**Next Task**: Task 5.2 (Blacklist Evaluator) - simplest Phase 5 task, good starting point
**Overall Project Status**:
- **Translation Pipeline**: Complete! All 4 phases operational with 407+ tests passing.
- **Phase 5 Planning**: Complete! Three advanced evaluators planned (Grammar, Blacklist, LLM).
- **Ready for Phase 5 Implementation**: Blacklist Evaluator recommended as first task (2-3h).
