"""
EPUB builder module for packaging translated chapters with images.

Reads per-chapter .txt files (containing [IMAGE:...] placeholders),
resolves images from the project images/ directory, and produces
a valid EPUB 3 file.
"""

import logging
import mimetypes
import re
from html import escape
from pathlib import Path
from typing import List, Optional, Tuple

from ebooklib import epub

logger = logging.getLogger(__name__)

_IMAGE_RE = re.compile(r'\[IMAGE:(images/[^:\]]+)(?::([^\]]*))?\]')
_CHAPTER_NUM_RE = re.compile(r'chapter_(\d+)\.txt$', re.IGNORECASE)
_HEADING_RE = re.compile(
    r'^(?:CHAPTER\s+)?([IVXLCDM\d]+)\s*$', re.IGNORECASE
)
_HR_RE = re.compile(r'^-{3,}$')

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


def detect_chapter_heading(text: str) -> Tuple[str, str, str]:
    """
    Parse chapter heading and subtitle from text.

    Expected patterns:
        CHAPTER I\\n\\nThe Title\\n\\nbody...
        I\\n\\nUNA AND THE LION\\n\\nbody...

    Returns:
        (heading, subtitle, body) where heading/subtitle may be ''
        if the pattern is not detected.
    """
    lines = text.split('\n')
    if not lines:
        return ('', '', text)

    first_line = lines[0].strip()
    heading_match = _HEADING_RE.match(first_line)

    if not heading_match:
        return ('', '', text)

    heading = first_line

    # Look for subtitle: skip blank lines after heading, take next non-blank line
    idx = 1
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


def chapter_text_to_xhtml(text: str, chapter_number: int) -> str:
    """
    Convert a plain-text chapter to XHTML suitable for EPUB embedding.

    Handles:
        - Chapter headings -> <h1>/<h2>
        - Paragraphs (blank-line separated) -> <p>
        - [IMAGE:...] placeholders -> <img> inside <div class="image">
        - --- lines -> <hr />
    """
    heading, subtitle, body = detect_chapter_heading(text)

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

    # Split body into blocks separated by blank lines
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
            parts.append(
                f'<div class="image">'
                f'<img src="images/{escape(filename)}" alt="{escape(alt_text)}"/>'
                f'</div>'
            )
            continue

        # Check for horizontal rule
        if _HR_RE.match(block):
            parts.append('<hr/>')
            continue

        # Regular paragraph -- escape HTML entities
        parts.append(f'<p>{escape(block)}</p>')

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


def build_epub(
    project_path: Path,
    title: str,
    author: str,
    language: str = 'es',
    cover_image: Optional[Path] = None,
    output_path: Optional[Path] = None,
    chapters_dir: Optional[Path] = None,
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

    Returns:
        Path to the written EPUB file.
    """
    project_path = Path(project_path)
    chapters_dir = Path(chapters_dir) if chapters_dir else project_path / 'chapters'
    images_dir = project_path / 'images'

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
        xhtml_content = chapter_text_to_xhtml(text, i)

        heading, subtitle, _ = detect_chapter_heading(text)
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
