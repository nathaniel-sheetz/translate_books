"""
EPUB builder module for packaging translated chapters with images.

Reads per-chapter .txt files (containing [IMAGE:...] placeholders),
resolves images from the project images/ directory, and produces
a valid EPUB 3 file.
"""

import json
import logging
import mimetypes
import re
from html import escape
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ebooklib import epub

logger = logging.getLogger(__name__)

_IMAGE_RE = re.compile(r'\[IMAGE:(images/[^:\]]+)(?::([^\]]*))?\]')
_CHAPTER_NUM_RE = re.compile(r'chapter_(\d+)\.txt$', re.IGNORECASE)
_HEADING_RE = re.compile(
    r'^(?:CHAPTER\s+)?([IVXLCDM\d]+)\s*$', re.IGNORECASE
)
_HR_RE = re.compile(r'^-{3,}$')

# Default configuration for synthesizing a chapter heading when the chapter
# text does not begin with a recognizable numeral line. Override per project
# via the "chapter_heading" key in project.json, e.g.:
#   "chapter_heading": {"label": "Capítulo", "numeral_style": "arabic"}
# Set "label" to "" (empty string) to emit just the numeral with no word.
_DEFAULT_HEADING_CONFIG: Dict[str, Any] = {
    "label": "Chapter",
    "numeral_style": "arabic",  # "arabic" or "roman"
}

_DEFAULT_TRANSLATOR_HEADING = "Note from the Translator"


def _int_to_roman(n: int) -> str:
    """Convert a positive integer to its Roman numeral representation."""
    if n <= 0:
        return str(n)
    pairs = [
        (1000, 'M'), (900, 'CM'), (500, 'D'), (400, 'CD'),
        (100, 'C'), (90, 'XC'), (50, 'L'), (40, 'XL'),
        (10, 'X'), (9, 'IX'), (5, 'V'), (4, 'IV'), (1, 'I'),
    ]
    out = []
    for value, sym in pairs:
        while n >= value:
            out.append(sym)
            n -= value
    return ''.join(out)


def _resolve_heading_config(
    config: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Merge a user-supplied heading config with defaults."""
    merged = dict(_DEFAULT_HEADING_CONFIG)
    if config:
        if 'label' in config and config['label'] is not None:
            merged['label'] = str(config['label'])
        if config.get('numeral_style') in ('arabic', 'roman'):
            merged['numeral_style'] = config['numeral_style']
    return merged


def synthesize_chapter_heading(
    chapter_number: int,
    config: Optional[Dict[str, Any]] = None,
) -> str:
    """Build a chapter heading string like 'Capítulo 1' or 'III'.

    Args:
        chapter_number: 1-based chapter index.
        config: Optional override of {"label": str, "numeral_style": str}.
    """
    cfg = _resolve_heading_config(config)
    label = cfg['label'].strip()
    if cfg['numeral_style'] == 'roman':
        numeral = _int_to_roman(chapter_number)
    else:
        numeral = str(chapter_number)
    if label:
        return f'{label} {numeral}'
    return numeral

_DEFAULT_CSS = """\
img { max-width: 100%; height: auto; }
div.image { text-align: center; margin: 1em 0; }
h1, h2 { text-align: center; }
p { text-indent: 1.5em; margin-top: 0.25em; margin-bottom: 0.25em; }
hr { margin: 1.5em auto; width: 40%; }
"""


def parse_image_placeholders(text: str) -> List[Tuple[str, str, str]]:
    """
    Find all [IMAGE:...] placeholders in text.

    Returns:
        List of (full_match, relative_path, alt_text) tuples.
        alt_text is '' when not provided.
    """
    results = []
    for m in _IMAGE_RE.finditer(text):
        full_match = m.group(0)
        rel_path = m.group(1)
        alt_text = m.group(2) or ''
        results.append((full_match, rel_path, alt_text))
    return results


def detect_chapter_heading(
    text: str,
    *,
    chapter_number: Optional[int] = None,
    heading_config: Optional[Dict[str, Any]] = None,
) -> Tuple[str, str, str]:
    """
    Parse chapter heading and subtitle from text.

    Expected patterns:
        CHAPTER I\\n\\nThe Title\\n\\nbody...
        I\\n\\nUNA AND THE LION\\n\\nbody...

    When the first line is not a recognizable numeral and ``chapter_number``
    is provided, a heading is synthesized from ``heading_config`` (e.g.
    "Capítulo 1") and the existing first non-blank line becomes the subtitle.

    Returns:
        (heading, subtitle, body) where heading/subtitle may be ''
        if no heading was detected and synthesis was not requested.
    """
    lines = text.split('\n')
    if not lines:
        return ('', '', text)

    first_line = lines[0].strip()
    heading_match = _HEADING_RE.match(first_line)

    if heading_match:
        heading = first_line
        idx = 1
    elif chapter_number is not None:
        # Synthesize a heading from the chapter number; the original first
        # non-blank line will be promoted to the subtitle below.
        heading = synthesize_chapter_heading(chapter_number, heading_config)
        idx = 0
    else:
        return ('', '', text)

    # Look for subtitle: skip blank lines, take next non-blank line.
    while idx < len(lines) and not lines[idx].strip():
        idx += 1

    subtitle = ''
    if idx < len(lines):
        candidate = lines[idx].strip()
        # Subtitle should be a short text line, not a paragraph or image
        if candidate and not _IMAGE_RE.match(candidate) and len(candidate) < 200:
            subtitle = candidate
            idx += 1

    # Skip blank lines after subtitle to find body start
    while idx < len(lines) and not lines[idx].strip():
        idx += 1

    body = '\n'.join(lines[idx:])
    return (heading, subtitle, body)


def _render_body_blocks(body: str) -> List[str]:
    """
    Render a plain-text body into a list of XHTML block strings.

    Handles:
        - Paragraphs (blank-line separated) -> <p>
        - [IMAGE:...] placeholders (sole-block) -> <img> inside <div class="image">
        - --- lines -> <hr />
    """
    out: List[str] = []
    blocks = re.split(r'\n{2,}', body)

    for block in blocks:
        block = block.strip()
        if not block:
            continue

        # Check if entire block is an image placeholder
        img_match = _IMAGE_RE.fullmatch(block)
        if img_match:
            rel_path = img_match.group(1)
            alt_text = img_match.group(2) or ''
            # Use just the filename for the src (images are stored flat in EPUB)
            filename = Path(rel_path).name
            out.append(
                f'<div class="image">'
                f'<img src="images/{escape(filename)}" alt="{escape(alt_text)}"/>'
                f'</div>'
            )
            continue

        # Check for horizontal rule
        if _HR_RE.match(block):
            out.append('<hr/>')
            continue

        # Regular paragraph -- escape HTML entities
        out.append(f'<p>{escape(block)}</p>')

    return out


def chapter_text_to_xhtml(
    text: str,
    chapter_number: int,
    *,
    heading_config: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Convert a plain-text chapter to XHTML suitable for EPUB embedding.

    Handles:
        - Chapter headings -> <h1>/<h2>
        - Paragraphs (blank-line separated) -> <p>
        - [IMAGE:...] placeholders -> <img> inside <div class="image">
        - --- lines -> <hr />
    """
    heading, subtitle, body = detect_chapter_heading(
        text,
        chapter_number=chapter_number,
        heading_config=heading_config,
    )

    parts = []
    parts.append('<?xml version="1.0" encoding="utf-8"?>')
    parts.append('<!DOCTYPE html>')
    parts.append('<html xmlns="http://www.w3.org/1999/xhtml">')
    parts.append('<head><title>{}</title>'
                 '<link rel="stylesheet" type="text/css" href="style.css"/>'
                 '</head>'.format(escape(heading or f'Chapter {chapter_number}')))
    parts.append('<body>')

    if heading:
        parts.append(f'<h1>{escape(heading)}</h1>')
    if subtitle:
        parts.append(f'<h2>{escape(subtitle)}</h2>')

    parts.extend(_render_body_blocks(body))

    parts.append('</body>')
    parts.append('</html>')
    return '\n'.join(parts)


def _strip_image_blocks(body: str) -> Tuple[str, int]:
    """
    Strip ALL [IMAGE:...] substrings from body (per ENG REVIEW decision 2A).

    Translator notes are short prose; image references would not be embedded
    (collect_referenced_images only scans chapters_dir), producing a broken
    EPUB. We strip them entirely, including inline occurrences.

    Returns:
        (stripped_body, n_stripped) — n_stripped is the number of image
        placeholders removed.
    """
    matches = _IMAGE_RE.findall(body)
    n_stripped = len(matches)
    if n_stripped == 0:
        return body, 0
    return _IMAGE_RE.sub('', body), n_stripped


def note_text_to_xhtml(heading: str, body: str) -> str:
    """
    Convert a translator note to XHTML. Same scaffolding as
    chapter_text_to_xhtml but takes an explicit heading (no regex
    detection, no subtitle) and strips [IMAGE:...] placeholders from body.
    """
    eff_heading = heading.strip() or _DEFAULT_TRANSLATOR_HEADING
    stripped_body, n_stripped = _strip_image_blocks(body)
    if n_stripped > 0:
        logger.warning(
            "Stripped %d image placeholder(s) from translator note",
            n_stripped,
        )

    parts = []
    parts.append('<?xml version="1.0" encoding="utf-8"?>')
    parts.append('<!DOCTYPE html>')
    parts.append('<html xmlns="http://www.w3.org/1999/xhtml">')
    parts.append(
        '<head><title>{}</title>'
        '<link rel="stylesheet" type="text/css" href="style.css"/>'
        '</head>'.format(escape(eff_heading))
    )
    parts.append('<body>')
    parts.append(f'<h1>{escape(eff_heading)}</h1>')
    parts.extend(_render_body_blocks(stripped_body))
    parts.append('</body>')
    parts.append('</html>')
    return '\n'.join(parts)


def collect_referenced_images(chapters_dir: Path) -> set:
    """Scan all chapter .txt files for [IMAGE:...] placeholders.

    Returns set of relative image paths (e.g. 'images/i010.jpg').
    """
    refs = set()
    for txt_file in sorted(chapters_dir.glob('chapter_*.txt')):
        text = txt_file.read_text(encoding='utf-8')
        for _, rel_path, _ in parse_image_placeholders(text):
            refs.add(rel_path)
    return refs


def _image_media_type(filename: str) -> str:
    """Determine MIME type from image filename."""
    mt, _ = mimetypes.guess_type(filename)
    if mt:
        return mt
    ext = Path(filename).suffix.lower()
    return {
        '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg',
        '.png': 'image/png',
        '.gif': 'image/gif',
        '.svg': 'image/svg+xml',
    }.get(ext, 'application/octet-stream')


def _sort_chapter_files(files: List[Path]) -> List[Path]:
    """Sort chapter files by their numeric portion."""
    def sort_key(p: Path) -> int:
        m = _CHAPTER_NUM_RE.search(p.name)
        return int(m.group(1)) if m else 0
    return sorted(files, key=sort_key)


def _load_chapter_heading_config(project_path: Path) -> Optional[Dict[str, Any]]:
    """Read the optional 'chapter_heading' block from project.json."""
    project_json = project_path / 'project.json'
    if not project_json.exists():
        return None
    try:
        data = json.loads(project_json.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError):
        return None
    cfg = data.get('chapter_heading')
    return cfg if isinstance(cfg, dict) else None


def build_epub(
    project_path: Path,
    title: str,
    author: str,
    language: str = 'es',
    cover_image: Optional[Path] = None,
    output_path: Optional[Path] = None,
    chapters_dir: Optional[Path] = None,
    chapter_heading_config: Optional[Dict[str, Any]] = None,
    translator_note_heading: Optional[str] = None,
    translator_note_body: Optional[str] = None,
) -> Path:
    """
    Build an EPUB from translated chapter files and project images.

    Args:
        project_path: Root project directory (contains images/).
        title: Book title for EPUB metadata.
        author: Author name for EPUB metadata.
        language: EPUB language code.
        cover_image: Path to cover image (absolute or relative to project_path).
                     Auto-detects images/cover.jpg or .png if not provided.
        output_path: Where to write the EPUB. Defaults to project_path/{name}.epub.
        chapters_dir: Directory containing chapter_*.txt files.
                      Defaults to project_path/chapters/.
        chapter_heading_config: Optional override for synthesized chapter
                      headings (used when a chapter does not begin with a
                      numeral line). Shape: {"label": str, "numeral_style":
                      "arabic"|"roman"}. If omitted, falls back to
                      project.json's "chapter_heading" key, then defaults.
        translator_note_heading: Heading for the optional "Note from the
                     Translator" final chapter. Falls back to a default constant
                     if blank/whitespace.
        translator_note_body: Body text for the translator note. If empty (or
                     becomes empty after stripping [IMAGE:...] placeholders),
                     no extra chapter is appended.

    Returns:
        Path to the written EPUB file.
    """
    project_path = Path(project_path)
    chapters_dir = Path(chapters_dir) if chapters_dir else project_path / 'chapters'
    images_dir = project_path / 'images'

    if chapter_heading_config is None:
        chapter_heading_config = _load_chapter_heading_config(project_path)

    # Discover chapter files
    chapter_files = list(chapters_dir.glob('chapter_*.txt'))
    if not chapter_files:
        raise FileNotFoundError(
            f"No chapter_*.txt files found in {chapters_dir}"
        )
    chapter_files = _sort_chapter_files(chapter_files)
    logger.info(f"Found {len(chapter_files)} chapter files")

    # Create EPUB book
    book = epub.EpubBook()
    book.set_identifier(f'translate-books-{project_path.name}')
    book.set_title(title)
    book.set_language(language)
    book.add_author(author)

    # Add CSS
    css_item = epub.EpubItem(
        uid='style',
        file_name='style.css',
        media_type='text/css',
        content=_DEFAULT_CSS.encode('utf-8'),
    )
    book.add_item(css_item)

    # Handle cover image
    cover_path = _resolve_cover(project_path, cover_image)
    if cover_path:
        cover_data = cover_path.read_bytes()
        book.set_cover(
            f'images/{cover_path.name}',
            cover_data,
        )
        logger.info(f"Cover image: {cover_path.name}")

    # Collect all referenced images and embed them
    referenced = collect_referenced_images(chapters_dir)
    embedded_images = set()
    missing_images = []

    for rel_path in sorted(referenced):
        img_file = project_path / rel_path
        filename = Path(rel_path).name

        if filename in embedded_images:
            continue

        if not img_file.exists():
            missing_images.append(rel_path)
            logger.warning(f"Image not found: {img_file}")
            continue

        img_item = epub.EpubItem(
            uid=f'img-{filename}',
            file_name=f'images/{filename}',
            media_type=_image_media_type(filename),
            content=img_file.read_bytes(),
        )
        book.add_item(img_item)
        embedded_images.add(filename)

    logger.info(f"Embedded {len(embedded_images)} images")
    if missing_images:
        logger.warning(f"Missing images: {missing_images}")

    # Convert chapters to XHTML and add to book
    spine = ['nav']
    toc = []

    for i, chapter_file in enumerate(chapter_files, 1):
        text = chapter_file.read_text(encoding='utf-8')
        xhtml_content = chapter_text_to_xhtml(
            text, i, heading_config=chapter_heading_config,
        )

        heading, subtitle, _ = detect_chapter_heading(
            text, chapter_number=i, heading_config=chapter_heading_config,
        )
        toc_label = heading or f'Chapter {i}'
        if subtitle:
            toc_label = f'{toc_label}: {subtitle}'

        chapter_item = epub.EpubHtml(
            title=toc_label,
            file_name=f'chapter_{i:02d}.xhtml',
            lang=language,
        )
        chapter_item.set_content(xhtml_content.encode('utf-8'))
        chapter_item.add_item(css_item)
        book.add_item(chapter_item)

        spine.append(chapter_item)
        toc.append(chapter_item)

    # Append optional "Note from the Translator" as the final chapter.
    if translator_note_body is not None:
        body_text = str(translator_note_body)
        stripped_body, _ = _strip_image_blocks(body_text)
        if stripped_body.strip():
            heading_text = (translator_note_heading or "").strip() \
                or _DEFAULT_TRANSLATOR_HEADING
            note_xhtml = note_text_to_xhtml(heading_text, body_text)
            note_item = epub.EpubHtml(
                uid='translator_note',
                title=heading_text,
                file_name='translator_note.xhtml',
                lang=language,
            )
            note_item.set_content(note_xhtml.encode('utf-8'))
            note_item.add_item(css_item)
            book.add_item(note_item)
            spine.append(note_item)
            toc.append(note_item)
            logger.info("Translator note appended")
        else:
            logger.info("Translator note skipped (empty body)")

    book.toc = toc
    book.spine = spine

    # Add navigation files
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    # Write EPUB
    if output_path is None:
        output_path = project_path / f'{project_path.name}.epub'
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    epub.write_epub(str(output_path), book)
    logger.info(f"EPUB written to {output_path}")
    return output_path


def _resolve_cover(
    project_path: Path, cover_image: Optional[Path]
) -> Optional[Path]:
    """Resolve cover image path, with auto-detection fallback."""
    if cover_image is not None:
        p = Path(cover_image)
        if not p.is_absolute():
            p = project_path / p
        if p.exists():
            return p
        logger.warning(f"Specified cover not found: {p}")
        return None

    # Auto-detect
    for name in ('cover.jpg', 'cover.jpeg', 'cover.png'):
        candidate = project_path / 'images' / name
        if candidate.exists():
            return candidate
    return None
