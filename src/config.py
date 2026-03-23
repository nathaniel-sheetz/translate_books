"""
Configuration management for the book translation workflow.

This module provides functions for loading, saving, creating, and validating
ProjectConfig objects. Configurations are stored as JSON files in the project
directory.
"""

import json
from pathlib import Path
from typing import Optional

from src.models import (
    ProjectConfig,
    TranslationMode,
    APIProvider,
    ChunkingConfig,
    TranslationConfig,
    EvaluationConfig,
)


def load_project_config(project_path: Path) -> ProjectConfig:
    """
    Load a ProjectConfig from the project's config.json file.

    Args:
        project_path: Path to the project directory

    Returns:
        Loaded ProjectConfig object

    Raises:
        FileNotFoundError: If the config file doesn't exist
        json.JSONDecodeError: If the file contains invalid JSON
        ValueError: If the JSON doesn't match the ProjectConfig schema

    Example:
        >>> config = load_project_config(Path("projects/my_book"))
    """
    config_path = project_path / "config.json"

    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    try:
        with config_path.open('r', encoding='utf-8') as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise json.JSONDecodeError(
            f"Invalid JSON in config file {config_path}: {e.msg}",
            e.doc,
            e.pos
        )

    try:
        return ProjectConfig.model_validate(data)
    except Exception as e:
        raise ValueError(f"Invalid ProjectConfig data in {config_path}: {e}")


def save_project_config(project_path: Path, config: ProjectConfig) -> None:
    """
    Save a ProjectConfig to the project's config.json file with atomic write.

    Uses a temporary file and atomic rename to prevent corruption if the write
    is interrupted. Creates project directory if it doesn't exist.

    Args:
        project_path: Path to the project directory
        config: The ProjectConfig object to save

    Raises:
        OSError: If there are permission or disk space issues

    Example:
        >>> save_project_config(Path("projects/my_book"), my_config)
    """
    # Ensure project directory exists
    project_path.mkdir(parents=True, exist_ok=True)

    # Serialize config to JSON-compatible dict
    data = config.model_dump(mode='json')

    # Write to config.json with atomic write pattern
    config_path = project_path / "config.json"
    temp_path = config_path.with_suffix('.tmp')
    try:
        with temp_path.open('w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write('\n')  # Add trailing newline

        # Atomic rename
        temp_path.replace(config_path)
    except Exception:
        # Clean up temp file on error
        temp_path.unlink(missing_ok=True)
        raise


def create_default_config(project_name: str) -> ProjectConfig:
    """
    Create a ProjectConfig with sensible defaults.

    This generates a configuration suitable for most English-to-Spanish
    translation projects using manual translation mode.

    Args:
        project_name: Identifier for the project (required)

    Returns:
        ProjectConfig object with default settings

    Example:
        >>> config = create_default_config("don_quixote")
        >>> config.source_language
        'en'
        >>> config.translation.mode
        TranslationMode.MANUAL
    """
    return ProjectConfig(
        project_name=project_name,
        source_language="en",
        target_language="es",
        chunking=ChunkingConfig(
            target_size=2000,
            overlap_paragraphs=2,
            min_chunk_size=500,
            max_chunk_size=3000
        ),
        translation=TranslationConfig(
            mode=TranslationMode.MANUAL,
            api_provider=None,
            model=None,
            prompt_template="prompts/translation_prompt.txt"
        ),
        evaluation=EvaluationConfig(
            enabled_evals=["length", "paragraph", "completeness"],
            fail_on_errors=False,
            generate_reports=True
        )
    )


def validate_config(config: ProjectConfig) -> list[str]:
    """
    Validate a ProjectConfig for business rules beyond Pydantic validation.

    Performs additional checks that require cross-field validation or
    external knowledge (e.g., known evaluator names).

    Args:
        config: The ProjectConfig to validate

    Returns:
        List of error messages (empty list if valid)

    Example:
        >>> config = create_default_config("test")
        >>> errors = validate_config(config)
        >>> len(errors)
        0

        >>> bad_config = ProjectConfig(
        ...     project_name="test",
        ...     translation=TranslationConfig(mode=TranslationMode.API)
        ... )
        >>> errors = validate_config(bad_config)
        >>> len(errors) > 0
        True
    """
    errors = []

    # Check API mode requirements
    if config.translation.mode == TranslationMode.API:
        if config.translation.api_provider is None:
            errors.append(
                "API mode requires 'api_provider' to be set "
                "(openai, anthropic, or custom)"
            )
        if config.translation.model is None:
            errors.append(
                "API mode requires 'model' to be specified "
                "(e.g., 'gpt-4', 'claude-3-opus-20240229')"
            )

    # Validate language codes (basic check - 2 or 3 letter codes)
    if not (2 <= len(config.source_language) <= 3):
        errors.append(
            f"Invalid source_language '{config.source_language}': "
            "must be 2-3 character language code (e.g., 'en', 'es')"
        )
    if not (2 <= len(config.target_language) <= 3):
        errors.append(
            f"Invalid target_language '{config.target_language}': "
            "must be 2-3 character language code (e.g., 'en', 'es')"
        )

    # Validate chunking constraints (should be caught by Pydantic, but double-check)
    if config.chunking.max_chunk_size <= config.chunking.min_chunk_size:
        errors.append(
            f"Chunking max_chunk_size ({config.chunking.max_chunk_size}) "
            f"must be greater than min_chunk_size ({config.chunking.min_chunk_size})"
        )

    if config.chunking.target_size < config.chunking.min_chunk_size:
        errors.append(
            f"Chunking target_size ({config.chunking.target_size}) "
            f"should be >= min_chunk_size ({config.chunking.min_chunk_size})"
        )

    if config.chunking.target_size > config.chunking.max_chunk_size:
        errors.append(
            f"Chunking target_size ({config.chunking.target_size}) "
            f"should be <= max_chunk_size ({config.chunking.max_chunk_size})"
        )

    # Validate enabled evaluators (check against known evaluators)
    known_evaluators = [
        "length",
        "paragraph",
        "completeness",
        "dictionary",
        "glossary",
        "grammar",
        "overlap"
    ]
    for eval_name in config.evaluation.enabled_evals:
        if eval_name not in known_evaluators:
            errors.append(
                f"Unknown evaluator '{eval_name}'. "
                f"Known evaluators: {', '.join(known_evaluators)}"
            )

    # Check for empty project name (should be caught by Pydantic min_length, but verify)
    if not config.project_name or config.project_name.strip() == "":
        errors.append("project_name cannot be empty")

    return errors
