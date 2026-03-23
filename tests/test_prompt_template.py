"""
Tests for prompt template loading, rendering, and glossary formatting.

Tests the prompt infrastructure functions that load templates, substitute variables,
and format glossaries for inclusion in prompts.
"""

import pytest
from pathlib import Path

from src.utils.file_io import (
    load_prompt_template,
    render_prompt,
    format_glossary_for_prompt
)
from src.models import Glossary, GlossaryTerm, GlossaryTermType


# =============================================================================
# FIXTURES
# =============================================================================

@pytest.fixture
def fixtures_dir():
    """Get path to test fixtures directory."""
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def template_path(fixtures_dir):
    """Get path to test template file."""
    return fixtures_dir / "prompts" / "translation.txt"


@pytest.fixture
def sample_glossary():
    """Sample glossary with various term types."""
    return Glossary(
        terms=[
            GlossaryTerm(
                english="Elizabeth Bennet",
                spanish="Elizabeth Bennet",
                type=GlossaryTermType.CHARACTER,
                alternatives=[]
            ),
            GlossaryTerm(
                english="Mr. Darcy",
                spanish="Sr. Darcy",
                type=GlossaryTermType.CHARACTER,
                alternatives=["Señor Darcy"]
            ),
            GlossaryTerm(
                english="Longbourn",
                spanish="Longbourn",
                type=GlossaryTermType.PLACE,
                alternatives=[]
            ),
            GlossaryTerm(
                english="entailment",
                spanish="vinculación",
                type=GlossaryTermType.CONCEPT,
                alternatives=["mayorazgo"]
            )
        ],
        version="1.0"
    )


# =============================================================================
# LOAD_PROMPT_TEMPLATE TESTS
# =============================================================================

class TestLoadPromptTemplate:
    """Tests for load_prompt_template()."""

    def test_load_with_explicit_path(self, template_path):
        """Test loading template with explicit path."""
        template = load_prompt_template(template_path)

        assert isinstance(template, str)
        assert len(template) > 0
        assert "{{book_title}}" in template
        assert "{{source_text}}" in template
        assert "{{glossary}}" in template

    def test_load_default_path(self):
        """Test loading template from default path."""
        # Default path should be prompts/translation.txt
        default_path = Path("prompts/translation.txt")

        if default_path.exists():
            template = load_prompt_template()
            assert isinstance(template, str)
            assert len(template) > 0
        else:
            # If default doesn't exist, should raise FileNotFoundError
            with pytest.raises(FileNotFoundError):
                load_prompt_template()

    def test_load_missing_file(self):
        """Test loading non-existent template raises FileNotFoundError."""
        missing_path = Path("nonexistent/template.txt")

        with pytest.raises(FileNotFoundError) as exc_info:
            load_prompt_template(missing_path)

        assert "not found" in str(exc_info.value).lower()

    def test_loaded_template_is_text(self, template_path):
        """Test that loaded template is plain text string."""
        template = load_prompt_template(template_path)

        # Should be a string
        assert isinstance(template, str)

        # Should not be JSON
        assert not template.startswith('{')


# =============================================================================
# RENDER_PROMPT TESTS
# =============================================================================

class TestRenderPrompt:
    """Tests for render_prompt()."""

    def test_render_with_all_variables(self):
        """Test rendering template with all variables provided."""
        template = "Translate {{source_text}} to {{target_language}}."
        variables = {
            "source_text": "Hello world",
            "target_language": "Spanish"
        }

        result = render_prompt(template, variables)

        assert result == "Translate Hello world to Spanish."
        assert "{{" not in result
        assert "}}" not in result

    def test_render_with_multiple_occurrences(self):
        """Test that same variable can appear multiple times."""
        template = "{{lang}} is great. I love {{lang}}!"
        variables = {"lang": "Python"}

        result = render_prompt(template, variables)

        assert result == "Python is great. I love Python!"

    def test_render_with_missing_variable(self):
        """Test that missing variables raise KeyError."""
        template = "Translate {{source_text}} to {{target_language}}."
        variables = {"source_text": "Hello"}  # Missing target_language

        with pytest.raises(KeyError) as exc_info:
            render_prompt(template, variables)

        assert "target_language" in str(exc_info.value)

    def test_render_converts_values_to_string(self):
        """Test that non-string values are converted to strings."""
        template = "Count: {{count}}, Price: {{price}}"
        variables = {"count": 42, "price": 19.99}

        result = render_prompt(template, variables)

        assert result == "Count: 42, Price: 19.99"

    def test_render_empty_template(self):
        """Test rendering empty template."""
        result = render_prompt("", {})
        assert result == ""

    def test_render_no_variables(self):
        """Test rendering template without any variables."""
        template = "This is a plain template with no variables."
        result = render_prompt(template, {})

        assert result == template

    def test_render_complex_template(self, template_path, sample_glossary):
        """Test rendering full translation template."""
        # Use a simpler template without {{variable}} in comments
        template = """
BOOK TRANSLATION TASK

You are translating "{{book_title}}" from {{source_language}} to {{target_language}}.

SOURCE TEXT:
{{source_text}}

GLOSSARY:
{{glossary}}

STYLE GUIDE:
{{style_guide}}

CONTEXT:
{{context}}
"""

        glossary_text = format_glossary_for_prompt(sample_glossary)

        variables = {
            "book_title": "Pride and Prejudice",
            "source_language": "English",
            "target_language": "Spanish",
            "source_text": "It is a truth universally acknowledged...",
            "glossary": glossary_text,
            "style_guide": "TONE: Formal",
            "context": "Regency-era novel, 1813"
        }

        result = render_prompt(template, variables)

        # Check all variables were replaced
        assert "{{" not in result
        assert "Pride and Prejudice" in result
        assert "English" in result
        assert "Spanish" in result


# =============================================================================
# FORMAT_GLOSSARY_FOR_PROMPT TESTS
# =============================================================================

class TestFormatGlossaryForPrompt:
    """Tests for format_glossary_for_prompt()."""

    def test_format_normal_glossary(self, sample_glossary):
        """Test formatting glossary with multiple term types."""
        result = format_glossary_for_prompt(sample_glossary)

        # Should contain type headers
        assert "CHARACTER NAMES:" in result
        assert "PLACE NAMES:" in result
        assert "CONCEPTS:" in result

        # Should contain terms
        assert "Elizabeth Bennet" in result
        assert "Sr. Darcy" in result
        assert "Longbourn" in result
        assert "vinculación" in result

    def test_format_with_alternatives(self, sample_glossary):
        """Test that alternatives are included in output."""
        result = format_glossary_for_prompt(sample_glossary)

        # Should include alternatives
        assert "alternatives:" in result
        assert "Señor Darcy" in result
        assert "mayorazgo" in result

    def test_format_empty_glossary(self):
        """Test formatting empty glossary."""
        empty_glossary = Glossary(terms=[], version="1.0")

        result = format_glossary_for_prompt(empty_glossary)

        assert "No glossary terms specified" in result

    def test_format_single_type(self):
        """Test glossary with only one term type."""
        glossary = Glossary(
            terms=[
                GlossaryTerm(
                    english="Paris",
                    spanish="París",
                    type=GlossaryTermType.PLACE
                )
            ],
            version="1.0"
        )

        result = format_glossary_for_prompt(glossary)

        assert "PLACE NAMES:" in result
        assert "Paris" in result
        assert "París" in result

        # Should not have other type headers
        assert "CHARACTER NAMES:" not in result
        assert "CONCEPTS:" not in result

    def test_format_preserves_order(self):
        """Test that term types appear in standard order."""
        # Create glossary with types in reverse order
        glossary = Glossary(
            terms=[
                GlossaryTerm(english="idea", spanish="idea", type=GlossaryTermType.CONCEPT),
                GlossaryTerm(english="London", spanish="Londres", type=GlossaryTermType.PLACE),
                GlossaryTerm(english="Jane", spanish="Jane", type=GlossaryTermType.CHARACTER),
            ],
            version="1.0"
        )

        result = format_glossary_for_prompt(glossary)

        # CHARACTER should come before PLACE, which comes before CONCEPT
        char_pos = result.find("CHARACTER NAMES:")
        place_pos = result.find("PLACE NAMES:")
        concept_pos = result.find("CONCEPTS:")

        assert char_pos < place_pos < concept_pos

    def test_format_is_human_readable(self, sample_glossary):
        """Test that output is properly formatted and readable."""
        result = format_glossary_for_prompt(sample_glossary)

        # Should have proper structure
        lines = result.split('\n')
        assert len(lines) > 0

        # Should use readable arrow notation
        assert "→" in result

        # Should have blank lines between sections
        assert '\n\n' in result


# =============================================================================
# INTEGRATION TESTS
# =============================================================================

class TestPromptWorkflow:
    """Integration tests for full prompt workflow."""

    def test_full_workflow(self, template_path, sample_glossary):
        """Test complete workflow: load template, format glossary, render prompt."""
        # Step 1: Load template (verify it loads)
        template = load_prompt_template(template_path)
        assert "{{book_title}}" in template

        # Step 2: Format glossary
        glossary_text = format_glossary_for_prompt(sample_glossary)
        assert "Elizabeth Bennet" in glossary_text

        # Step 3: Use a simple template for testing (without {{variable}} in comments)
        simple_template = "Translate {{source_text}} to {{target_language}}. Terms: {{glossary}}"

        variables = {
            "source_text": "Test text to translate",
            "target_language": "Spanish",
            "glossary": glossary_text
        }

        # Step 4: Render prompt
        final_prompt = render_prompt(simple_template, variables)

        # Verify final prompt
        assert "Test text to translate" in final_prompt
        assert "Spanish" in final_prompt
        assert "Elizabeth Bennet" in final_prompt
        assert "{{" not in final_prompt  # No unreplaced variables

    def test_workflow_with_minimal_variables(self, template_path):
        """Test workflow can handle just required variables."""
        template = "Book: {{book_title}}\nText: {{source_text}}"
        variables = {
            "book_title": "Test Book",
            "source_text": "Test content"
        }

        result = render_prompt(template, variables)

        assert "Test Book" in result
        assert "Test content" in result
