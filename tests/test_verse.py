"""Tests for the block-level verse detector."""

from src.utils.verse import is_verse_block


class TestIsVerseBlock:
    def test_pure_stanza(self):
        """A four-line stanza with non-terminal-punctuated lines."""
        block = (
            "Drops of rain and bits of sunshine\n"
            "Falling here and gleaming there,\n"
            "Tiny blades of grass appearing.\n"
            "Tell of springtime bright and fair."
        )
        assert is_verse_block(block) is True

    def test_mixed_chapter_stanza(self):
        """Stanza embedded inside a prose-and-verse chapter."""
        block = (
            "In the snowing and the blowing,\n"
            "In the cruel sleet,\n"
            "Little flowers begin their growing\n"
            "Far beneath our feet."
        )
        assert is_verse_block(block) is True

    def test_translated_stanza(self):
        """The same stanza translated to Spanish should still be verse."""
        block = (
            "Gotas de lluvia y destellos de sol\n"
            "cayendo aquí y brillando allá,\n"
            "pequeñas briznas de hierba apareciendo,\n"
            "hablan de una primavera brillante y bella."
        )
        assert is_verse_block(block) is True

    def test_prose_paragraph(self):
        """A normal prose paragraph -- long lines, terminal punctuation."""
        block = (
            "There are many beautiful flowers in the world. "
            "Some grow in the fields, others by the brook, "
            "and still others on the highest mountains. "
            "Each has its season, and each its place."
        )
        assert is_verse_block(block) is False

    def test_single_line(self):
        """Single-line blocks (headings, attribution) are not verse."""
        assert is_verse_block("---Lillian Cox.") is False
        assert is_verse_block("SPRING.") is False
        assert is_verse_block("") is False

    def test_prose_with_internal_newlines(self):
        """Long lines separated by newlines are still prose, not verse."""
        block = (
            "The morning rose, and a soft golden glow spread across the meadow as far as the eye could see.\n"
            "Birds called from every tree, and the brook bubbled quietly among the stones."
        )
        assert is_verse_block(block) is False

    def test_two_line_stanza(self):
        """Couplets count as verse."""
        block = (
            "Watch the pretty snowflakes fall,\n"
            "Some are large and some are small;"
        )
        assert is_verse_block(block) is True

    def test_all_terminal_punctuation(self):
        """Short lines that all end in '.' are still line-break-preserving
        content (lists, captions, aphoristic verse). The renderer should
        keep them on separate lines."""
        block = (
            "First line.\n"
            "Second line.\n"
            "Third line.\n"
            "Fourth line."
        )
        assert is_verse_block(block) is True

    def test_long_lines_with_terminal_punct(self):
        """Long lines (typical prose hard-wrap) are not verse even when split."""
        block = (
            "The cat sat down on the mat in the hot afternoon sun, watching dust motes.\n"
            "Outside the window, a bird sang from the very highest branch of the oak tree."
        )
        assert is_verse_block(block) is False
