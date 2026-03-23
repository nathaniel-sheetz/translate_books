"""Tests for core data models."""

import pytest
from src.models import Glossary, GlossaryTerm, GlossaryTermType


class TestGlossaryFindTermBySpanish:
    """Tests for Glossary.find_term_by_spanish() method."""

    def test_find_by_primary_spanish(self):
        """Test finding a term by its primary Spanish translation."""
        glossary = Glossary(terms=[
            GlossaryTerm(english="magic", spanish="magia", type=GlossaryTermType.CONCEPT),
            GlossaryTerm(english="wizard", spanish="mago", type=GlossaryTermType.CHARACTER)
        ])

        result = glossary.find_term_by_spanish("magia")
        assert result is not None
        assert result.english == "magic"
        assert result.spanish == "magia"

    def test_find_by_spanish_case_insensitive(self):
        """Test that search is case-insensitive."""
        glossary = Glossary(terms=[
            GlossaryTerm(english="magic", spanish="magia", type=GlossaryTermType.CONCEPT)
        ])

        # Test uppercase
        result = glossary.find_term_by_spanish("MAGIA")
        assert result is not None
        assert result.english == "magic"

        # Test mixed case
        result = glossary.find_term_by_spanish("MaGiA")
        assert result is not None
        assert result.english == "magic"

    def test_find_by_alternative_translation(self):
        """Test finding a term by its alternative Spanish translation."""
        glossary = Glossary(terms=[
            GlossaryTerm(
                english="magic",
                spanish="magia",
                alternatives=["hechicería", "brujería"],
                type=GlossaryTermType.CONCEPT
            )
        ])

        # Find by first alternative
        result = glossary.find_term_by_spanish("hechicería")
        assert result is not None
        assert result.english == "magic"
        assert result.spanish == "magia"

        # Find by second alternative
        result = glossary.find_term_by_spanish("brujería")
        assert result is not None
        assert result.english == "magic"

    def test_find_by_alternative_case_insensitive(self):
        """Test that alternative search is also case-insensitive."""
        glossary = Glossary(terms=[
            GlossaryTerm(
                english="magic",
                spanish="magia",
                alternatives=["hechicería"],
                type=GlossaryTermType.CONCEPT
            )
        ])

        result = glossary.find_term_by_spanish("HECHICERÍA")
        assert result is not None
        assert result.english == "magic"

    def test_not_found_returns_none(self):
        """Test that searching for non-existent term returns None."""
        glossary = Glossary(terms=[
            GlossaryTerm(english="magic", spanish="magia", type=GlossaryTermType.CONCEPT)
        ])

        result = glossary.find_term_by_spanish("abracadabra")
        assert result is None

    def test_empty_glossary_returns_none(self):
        """Test searching in empty glossary returns None."""
        glossary = Glossary(terms=[])

        result = glossary.find_term_by_spanish("magia")
        assert result is None

    def test_multiple_terms_returns_first_match(self):
        """Test that when multiple terms could match, first one is returned."""
        glossary = Glossary(terms=[
            GlossaryTerm(english="magic", spanish="magia", type=GlossaryTermType.CONCEPT),
            GlossaryTerm(english="spell", spanish="hechizo", alternatives=["magia"], type=GlossaryTermType.CONCEPT)
        ])

        # Should find first term by primary translation
        result = glossary.find_term_by_spanish("magia")
        assert result is not None
        assert result.english == "magic"  # First term, not second

    def test_find_proper_noun(self):
        """Test finding proper nouns that are same in English and Spanish."""
        glossary = Glossary(terms=[
            GlossaryTerm(english="Darcy", spanish="Darcy", type=GlossaryTermType.CHARACTER),
            GlossaryTerm(english="Hogwarts", spanish="Hogwarts", type=GlossaryTermType.PLACE)
        ])

        result = glossary.find_term_by_spanish("Darcy")
        assert result is not None
        assert result.english == "Darcy"
        assert result.spanish == "Darcy"


class TestGlossaryFindTerm:
    """Tests for original Glossary.find_term() method to ensure it still works."""

    def test_find_by_english(self):
        """Test finding a term by English value."""
        glossary = Glossary(terms=[
            GlossaryTerm(english="magic", spanish="magia", type=GlossaryTermType.CONCEPT)
        ])

        result = glossary.find_term("magic")
        assert result is not None
        assert result.english == "magic"
        assert result.spanish == "magia"

    def test_find_by_english_case_insensitive(self):
        """Test that English search is case-insensitive."""
        glossary = Glossary(terms=[
            GlossaryTerm(english="magic", spanish="magia", type=GlossaryTermType.CONCEPT)
        ])

        result = glossary.find_term("MAGIC")
        assert result is not None
        assert result.english == "magic"
