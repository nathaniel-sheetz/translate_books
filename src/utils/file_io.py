"""
File I/O utilities for the book translation workflow.

This module provides functions for loading and saving Pydantic models to/from JSON files,
with atomic writes, proper error handling, and automatic directory creation.
"""

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from src.models import Blacklist, Chunk, Glossary, ProjectState, StyleGuide


def load_chunk(chunk_path: Path) -> Chunk:
    """
    Load a Chunk from JSON file.

    Args:
        chunk_path: Path to the chunk JSON file

    Returns:
        Loaded Chunk object

    Raises:
        FileNotFoundError: If the chunk file doesn't exist
        json.JSONDecodeError: If the file contains invalid JSON
        ValidationError: If the JSON doesn't match the Chunk schema

    Example:
        >>> chunk = load_chunk(Path("chunks/original/ch01_chunk_001.json"))
    """
    if not chunk_path.exists():
        raise FileNotFoundError(f"Chunk file not found: {chunk_path}")

    try:
        with chunk_path.open('r', encoding='utf-8') as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise json.JSONDecodeError(
            f"Invalid JSON in chunk file {chunk_path}: {e.msg}",
            e.doc,
            e.pos
        )

    try:
        return Chunk.model_validate(data)
    except Exception as e:
        raise ValueError(f"Invalid Chunk data in {chunk_path}: {e}")


def save_chunk(chunk: Chunk, output_path: Path) -> None:
    """
    Save a Chunk to JSON file with atomic write.

    Uses a temporary file and atomic rename to prevent corruption if the write
    is interrupted. Creates parent directories if they don't exist.

    Args:
        chunk: The Chunk object to save
        output_path: Path where the chunk JSON should be saved

    Raises:
        OSError: If there are permission or disk space issues

    Example:
        >>> save_chunk(my_chunk, Path("chunks/translated/ch01_chunk_001.json"))
    """
    # Create parent directory if it doesn't exist
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Serialize chunk to JSON-compatible dict
    data = chunk.model_dump(mode='json')

    # Write to temporary file first (atomic write pattern)
    temp_path = output_path.with_suffix('.tmp')
    try:
        with temp_path.open('w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write('\n')  # Add trailing newline

        # Atomic rename (POSIX) or near-atomic (Windows)
        temp_path.replace(output_path)
    except Exception:
        # Clean up temp file on error
        temp_path.unlink(missing_ok=True)
        raise


def load_glossary(glossary_path: Path) -> Glossary:
    """
    Load a Glossary from JSON file.

    Args:
        glossary_path: Path to the glossary JSON file

    Returns:
        Loaded Glossary object

    Raises:
        FileNotFoundError: If the glossary file doesn't exist
        json.JSONDecodeError: If the file contains invalid JSON
        ValidationError: If the JSON doesn't match the Glossary schema

    Example:
        >>> glossary = load_glossary(Path("projects/my_book/glossary.json"))
    """
    if not glossary_path.exists():
        raise FileNotFoundError(f"Glossary file not found: {glossary_path}")

    try:
        with glossary_path.open('r', encoding='utf-8') as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise json.JSONDecodeError(
            f"Invalid JSON in glossary file {glossary_path}: {e.msg}",
            e.doc,
            e.pos
        )

    try:
        return Glossary.model_validate(data)
    except Exception as e:
        raise ValueError(f"Invalid Glossary data in {glossary_path}: {e}")


def save_glossary(glossary: Glossary, output_path: Path) -> None:
    """
    Save a Glossary to JSON file with atomic write.

    Uses a temporary file and atomic rename to prevent corruption if the write
    is interrupted. Creates parent directories if they don't exist.

    Args:
        glossary: The Glossary object to save
        output_path: Path where the glossary JSON should be saved

    Raises:
        OSError: If there are permission or disk space issues

    Example:
        >>> save_glossary(my_glossary, Path("projects/my_book/glossary.json"))
    """
    # Create parent directory if it doesn't exist
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Serialize glossary to JSON-compatible dict
    data = glossary.model_dump(mode='json')

    # Write to temporary file first (atomic write pattern)
    temp_path = output_path.with_suffix('.tmp')
    try:
        with temp_path.open('w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write('\n')  # Add trailing newline

        # Atomic rename
        temp_path.replace(output_path)
    except Exception:
        # Clean up temp file on error
        temp_path.unlink(missing_ok=True)
        raise


def load_blacklist(blacklist_path: Path) -> Blacklist:
    """
    Load a Blacklist from JSON file.

    Args:
        blacklist_path: Path to the blacklist JSON file

    Returns:
        Loaded Blacklist object

    Raises:
        FileNotFoundError: If the blacklist file doesn't exist
        json.JSONDecodeError: If the file contains invalid JSON
        ValidationError: If the JSON doesn't match the Blacklist schema

    Example:
        >>> blacklist = load_blacklist(Path("blacklist.json"))
    """
    if not blacklist_path.exists():
        raise FileNotFoundError(f"Blacklist file not found: {blacklist_path}")

    try:
        with blacklist_path.open('r', encoding='utf-8') as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise json.JSONDecodeError(
            f"Invalid JSON in blacklist file {blacklist_path}: {e.msg}",
            e.doc,
            e.pos
        )

    try:
        return Blacklist.model_validate(data)
    except Exception as e:
        raise ValueError(f"Invalid Blacklist data in {blacklist_path}: {e}")


def load_prompt_template(template_path: Optional[Path] = None) -> str:
    """
    Load a prompt template from file.

    Args:
        template_path: Path to the template file. If None, uses default
                      'prompts/translation.txt' from current directory.

    Returns:
        Template content as string

    Raises:
        FileNotFoundError: If the template file doesn't exist

    Example:
        >>> template = load_prompt_template()  # Loads default
        >>> template = load_prompt_template(Path("custom/my_template.txt"))
    """
    if template_path is None:
        template_path = Path("prompts/translation.txt")

    if not template_path.exists():
        raise FileNotFoundError(f"Template file not found: {template_path}")

    with template_path.open('r', encoding='utf-8') as f:
        return f.read()


def render_prompt(template: str, variables: dict[str, Any]) -> str:
    """
    Render a prompt template by substituting {{variable}} placeholders.

    Args:
        template: Template string with {{variable}} placeholders
        variables: Dictionary mapping variable names to values

    Returns:
        Rendered prompt with all variables substituted

    Raises:
        KeyError: If required variables are missing from the template

    Example:
        >>> template = "Translate {{source_text}} to {{target_language}}"
        >>> result = render_prompt(template, {
        ...     "source_text": "Hello",
        ...     "target_language": "Spanish"
        ... })
        >>> print(result)
        Translate Hello to Spanish
    """
    result = template

    # Replace all variables
    for key, value in variables.items():
        placeholder = f"{{{{{key}}}}}"  # Creates {{key}}
        result = result.replace(placeholder, str(value))

    # Check for any unreplaced variables
    remaining = re.findall(r'\{\{(\w+)\}\}', result)
    if remaining:
        raise KeyError(
            f"Missing required variables in template: {', '.join(remaining)}"
        )

    return result


def filter_glossary_for_chunk(glossary: Glossary, source_text: str) -> Glossary:
    """
    Filter a glossary to only include terms that appear in the chunk's source text.

    Matches case-insensitively and handles common English variants:
    plural (+s, +es), possessive ('s), and past tense (+d, +ed).

    Args:
        glossary: Full project glossary
        source_text: The chunk's English source text

    Returns:
        A new Glossary containing only matching terms
    """
    if not glossary.terms:
        return glossary

    text_lower = source_text.lower()

    matching_terms = []
    for term in glossary.terms:
        english_lower = term.english.lower()
        if english_lower in text_lower:
            matching_terms.append(term)
            continue
        # Check common variants: plural, possessive, past tense
        variants = [
            english_lower + "s",
            english_lower + "es",
            english_lower + "'s",
            english_lower + "\u2019s",  # curly apostrophe
        ]
        # Past tense variants (only if not already ending in e/d)
        if not english_lower.endswith("e"):
            variants.append(english_lower + "ed")
        if not english_lower.endswith("d"):
            variants.append(english_lower + "d")
        if any(v in text_lower for v in variants):
            matching_terms.append(term)

    return Glossary(
        terms=matching_terms,
        version=glossary.version,
        updated_at=glossary.updated_at,
    )


def format_glossary_for_prompt(glossary: Glossary) -> str:
    """
    Format a Glossary into human-readable text for prompt inclusion.

    Groups terms by type (CHARACTER, PLACE, CONCEPT, etc.) and formats them
    with alternatives if present.

    Args:
        glossary: The Glossary object to format

    Returns:
        Formatted glossary text suitable for prompt inclusion

    Example:
        >>> glossary = Glossary(terms=[
        ...     GlossaryTerm(english="Harry", spanish="Harry", type="character"),
        ...     GlossaryTerm(english="Hogwarts", spanish="Hogwarts", type="place")
        ... ])
        >>> print(format_glossary_for_prompt(glossary))
        CHARACTER NAMES:
        - Harry → Harry

        PLACE NAMES:
        - Hogwarts → Hogwarts
    """
    if not glossary.terms:
        return "No glossary terms specified."

    # Import here to avoid circular dependency
    from src.models import GlossaryTermType

    # Group terms by type
    terms_by_type: dict[str, list] = {}
    for term in glossary.terms:
        term_type = term.type.value if hasattr(term, 'type') else 'other'
        if term_type not in terms_by_type:
            terms_by_type[term_type] = []
        terms_by_type[term_type].append(term)

    # Format output
    sections = []

    # Define type order and labels
    type_labels = {
        GlossaryTermType.CHARACTER.value: "CHARACTER NAMES",
        GlossaryTermType.PLACE.value: "PLACE NAMES",
        GlossaryTermType.CONCEPT.value: "CONCEPTS",
        GlossaryTermType.TECHNICAL.value: "TECHNICAL TERMS",
        GlossaryTermType.OTHER.value: "OTHER TERMS"
    }

    for term_type, label in type_labels.items():
        if term_type in terms_by_type:
            sections.append(f"{label}:")
            for term in terms_by_type[term_type]:
                # Format alternatives
                if hasattr(term, 'alternatives') and term.alternatives:
                    alts = ", ".join(term.alternatives)
                    sections.append(f"- {term.english} → {term.spanish} (alternatives: {alts})")
                else:
                    sections.append(f"- {term.english} → {term.spanish}")
            sections.append("")  # Blank line between sections

    return "\n".join(sections).strip()


def load_style_guide(path: Path) -> StyleGuide:
    """
    Load a StyleGuide from JSON file.

    Args:
        path: Path to the style guide JSON file

    Returns:
        Loaded StyleGuide object

    Raises:
        FileNotFoundError: If the style guide file doesn't exist
        json.JSONDecodeError: If the file contains invalid JSON
        ValidationError: If the JSON doesn't match the StyleGuide schema

    Example:
        >>> style_guide = load_style_guide(Path("projects/my_book/style_guide.json"))
    """
    if not path.exists():
        raise FileNotFoundError(f"Style guide file not found: {path}")

    try:
        with path.open('r', encoding='utf-8') as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise json.JSONDecodeError(
            f"Invalid JSON in style guide file {path}: {e.msg}",
            e.doc,
            e.pos
        )

    try:
        return StyleGuide.model_validate(data)
    except Exception as e:
        raise ValueError(f"Invalid StyleGuide data in {path}: {e}")


def save_style_guide(style_guide: StyleGuide, output_path: Path) -> None:
    """
    Save a StyleGuide to JSON file with atomic write.

    Uses a temporary file and atomic rename to prevent corruption if the write
    is interrupted. Creates parent directories if they don't exist.

    Args:
        style_guide: The StyleGuide object to save
        output_path: Path where the style guide JSON should be saved

    Raises:
        OSError: If there are permission or disk space issues

    Example:
        >>> save_style_guide(my_style_guide, Path("projects/my_book/style_guide.json"))
    """
    # Create parent directory if it doesn't exist
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Serialize style guide to JSON-compatible dict
    data = style_guide.model_dump(mode='json')

    # Write to temporary file first (atomic write pattern)
    temp_path = output_path.with_suffix('.tmp')
    try:
        with temp_path.open('w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write('\n')  # Add trailing newline

        # Atomic rename
        temp_path.replace(output_path)
    except Exception:
        # Clean up temp file on error
        temp_path.unlink(missing_ok=True)
        raise


def load_state(project_path: Path) -> ProjectState:
    """
    Load ProjectState from project's state.json file.

    If the state file doesn't exist, returns a new ProjectState with defaults.
    This allows graceful initialization of new projects.

    Args:
        project_path: Path to the project directory

    Returns:
        Loaded or newly created ProjectState object

    Raises:
        json.JSONDecodeError: If the file contains invalid JSON
        ValidationError: If the JSON doesn't match the ProjectState schema

    Example:
        >>> state = load_state(Path("projects/my_book"))
    """
    state_path = project_path / "state.json"

    # If state file doesn't exist, return new state (graceful degradation)
    if not state_path.exists():
        return ProjectState(project_name=project_path.name)

    try:
        with state_path.open('r', encoding='utf-8') as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise json.JSONDecodeError(
            f"Invalid JSON in state file {state_path}: {e.msg}",
            e.doc,
            e.pos
        )

    try:
        return ProjectState.model_validate(data)
    except Exception as e:
        raise ValueError(f"Invalid ProjectState data in {state_path}: {e}")


def save_state(state: ProjectState, project_path: Path) -> None:
    """
    Save ProjectState to project's state.json file with atomic write.

    Updates the state's updated_at timestamp before saving. Creates project
    directory if it doesn't exist.

    Args:
        state: The ProjectState object to save
        project_path: Path to the project directory

    Raises:
        OSError: If there are permission or disk space issues

    Example:
        >>> save_state(my_state, Path("projects/my_book"))
    """
    # Ensure project directory exists
    project_path.mkdir(parents=True, exist_ok=True)

    # Update timestamp
    state.updated_at = datetime.now()

    # Serialize state to JSON-compatible dict
    data = state.model_dump(mode='json')

    # Write to state.json with atomic write pattern
    state_path = project_path / "state.json"
    temp_path = state_path.with_suffix('.tmp')
    try:
        with temp_path.open('w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write('\n')  # Add trailing newline

        # Atomic rename
        temp_path.replace(state_path)
    except Exception:
        # Clean up temp file on error
        temp_path.unlink(missing_ok=True)
        raise


def ensure_project_structure(project_path: Path) -> None:
    """
    Create standard project directory structure if it doesn't exist.

    Creates the following directory tree:
        project_path/
        ├── chapters/
        │   ├── original/
        │   └── translated/
        ├── chunks/
        │   ├── original/
        │   └── translated/
        └── reports/

    Args:
        project_path: Path to the project directory

    Raises:
        OSError: If there are permission issues creating directories

    Example:
        >>> ensure_project_structure(Path("projects/my_book"))
    """
    # Create main project directory
    project_path.mkdir(parents=True, exist_ok=True)

    # Create subdirectory structure
    subdirs = [
        "chapters/original",
        "chapters/translated",
        "chunks/original",
        "chunks/translated",
        "reports",
    ]

    for subdir in subdirs:
        (project_path / subdir).mkdir(parents=True, exist_ok=True)


def _generate_report_filename(chunk_id: str, extension: str) -> str:
    """
    Generate timestamped report filename.

    Args:
        chunk_id: ID of the chunk being evaluated
        extension: File extension (txt, json, html)

    Returns:
        Filename in format: eval_{chunk_id}_{timestamp}.{ext}

    Example:
        >>> _generate_report_filename("ch01_chunk_001", "html")
        'eval_ch01_chunk_001_20250131_143022.html'
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"eval_{chunk_id}_{timestamp}.{extension}"


def save_text_report(report: str, project_path: Path, chunk_id: str) -> Path:
    """
    Save text report to project's reports directory.

    Creates a timestamped text file in the reports/ directory. Uses atomic
    write pattern to prevent corruption.

    Args:
        report: Text report content (may include ANSI color codes)
        project_path: Path to the project directory
        chunk_id: ID of the chunk being evaluated

    Returns:
        Path to the saved report file

    Raises:
        OSError: If there are permission or disk space issues

    Example:
        >>> from src.evaluators.reporting import generate_text_report
        >>> report = generate_text_report(results, aggregated, chunk)
        >>> path = save_text_report(report, Path("projects/my_book"), "ch01_chunk_001")
        >>> print(f"Report saved to: {path}")
    """
    # Ensure reports directory exists
    reports_dir = project_path / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    # Generate filename with timestamp
    filename = _generate_report_filename(chunk_id, "txt")
    output_path = reports_dir / filename

    # Write with atomic pattern
    temp_path = output_path.with_suffix('.tmp')
    try:
        with temp_path.open('w', encoding='utf-8') as f:
            f.write(report)
            if not report.endswith('\n'):
                f.write('\n')  # Ensure trailing newline

        # Atomic rename
        temp_path.replace(output_path)
        return output_path
    except Exception:
        # Clean up temp file on error
        temp_path.unlink(missing_ok=True)
        raise


def save_json_report(report: str, project_path: Path, chunk_id: str) -> Path:
    """
    Save JSON report to project's reports directory.

    Creates a timestamped JSON file in the reports/ directory. Uses atomic
    write pattern to prevent corruption.

    Args:
        report: JSON report content (as string from generate_json_report)
        project_path: Path to the project directory
        chunk_id: ID of the chunk being evaluated

    Returns:
        Path to the saved report file

    Raises:
        OSError: If there are permission or disk space issues
        ValueError: If report is not valid JSON

    Example:
        >>> from src.evaluators.reporting import generate_json_report
        >>> report = generate_json_report(results, aggregated, chunk)
        >>> path = save_json_report(report, Path("projects/my_book"), "ch01_chunk_001")
        >>> print(f"Report saved to: {path}")
    """
    # Ensure reports directory exists
    reports_dir = project_path / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    # Validate JSON before saving
    try:
        json.loads(report)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON report: {e}")

    # Generate filename with timestamp
    filename = _generate_report_filename(chunk_id, "json")
    output_path = reports_dir / filename

    # Write with atomic pattern
    temp_path = output_path.with_suffix('.tmp')
    try:
        with temp_path.open('w', encoding='utf-8') as f:
            f.write(report)
            if not report.endswith('\n'):
                f.write('\n')  # Ensure trailing newline

        # Atomic rename
        temp_path.replace(output_path)
        return output_path
    except Exception:
        # Clean up temp file on error
        temp_path.unlink(missing_ok=True)
        raise


def save_html_report(report: str, project_path: Path, chunk_id: str) -> Path:
    """
    Save HTML report to project's reports directory.

    Creates a timestamped HTML file in the reports/ directory. Uses atomic
    write pattern to prevent corruption.

    Args:
        report: HTML report content (complete HTML document)
        project_path: Path to the project directory
        chunk_id: ID of the chunk being evaluated

    Returns:
        Path to the saved report file

    Raises:
        OSError: If there are permission or disk space issues

    Example:
        >>> from src.evaluators.reporting import generate_html_report
        >>> report = generate_html_report(results, aggregated, chunk)
        >>> path = save_html_report(report, Path("projects/my_book"), "ch01_chunk_001")
        >>> print(f"Report saved to: {path}")
        >>> # Open in browser: webbrowser.open(f"file://{path}")
    """
    # Ensure reports directory exists
    reports_dir = project_path / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    # Generate filename with timestamp
    filename = _generate_report_filename(chunk_id, "html")
    output_path = reports_dir / filename

    # Write with atomic pattern
    temp_path = output_path.with_suffix('.tmp')
    try:
        with temp_path.open('w', encoding='utf-8') as f:
            f.write(report)
            if not report.endswith('\n'):
                f.write('\n')  # Ensure trailing newline

        # Atomic rename
        temp_path.replace(output_path)
        return output_path
    except Exception:
        # Clean up temp file on error
        temp_path.unlink(missing_ok=True)
        raise
