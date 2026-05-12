"""
Build NBLA verse index from EPUB and add NBLA text column to CSV.
"""
import os
import re
import csv
import glob
import html

OEBPS = r"C:\Users\Nathaniel\Downloads\nbla_extract\OEBPS"
INPUT_CSV = r"C:\Users\Nathaniel\Downloads\short_sermons_quotations.csv"
OUTPUT_CSV = r"C:\Users\Nathaniel\Downloads\short_sermons_quotations_with_nbla.csv"

BOOK_NUMS = {
    "Genesis": 1, "Exodus": 2, "Leviticus": 3, "Numbers": 4, "Deuteronomy": 5,
    "Joshua": 6, "Judges": 7, "Ruth": 8, "1 Samuel": 9, "2 Samuel": 10,
    "1 Kings": 11, "2 Kings": 12, "1 Chronicles": 13, "2 Chronicles": 14,
    "Ezra": 15, "Nehemiah": 16, "Esther": 17, "Job": 18, "Psalm": 19, "Psalms": 19,
    "Proverbs": 20, "Ecclesiastes": 21, "Song of Solomon": 22,
    "Isaiah": 23, "Jeremiah": 24, "Lamentations": 25, "Ezekiel": 26, "Daniel": 27,
    "Hosea": 28, "Joel": 29, "Amos": 30, "Obadiah": 31, "Jonah": 32,
    "Micah": 33, "Nahum": 34, "Habakkuk": 35, "Zephaniah": 36, "Haggai": 37,
    "Zechariah": 38, "Malachi": 39,
    "Matthew": 40, "Mark": 41, "Luke": 42, "John": 43, "Acts": 44,
    "Romans": 45, "1 Corinthians": 46, "2 Corinthians": 47, "Galatians": 48,
    "Ephesians": 49, "Philippians": 50, "Colossians": 51,
    "1 Thessalonians": 52, "2 Thessalonians": 53,
    "1 Timothy": 54, "2 Timothy": 55, "Titus": 56, "Philemon": 57,
    "Hebrews": 58, "James": 59, "1 Peter": 60, "2 Peter": 61,
    "1 John": 62, "2 John": 63, "3 John": 64, "Jude": 65, "Revelation": 66,
}

VER_RE = re.compile(r'<span class="ver[012]?" id="v(\d{2})(\d{3})(\d{3})">[^<]*</span>')
ENREF_RE = re.compile(r'<a class="enref"[^>]*>[^<]*</a>')
SUP_RE = re.compile(r'<sup[^>]*>[^<]*</sup>')
# Section/chapter heading paragraphs to strip from inside verse text
HEADING_RE = re.compile(r'<p class="(?:ahaft2|ahaft|ctfm|bk2|cs|ms|ms1|ms2|s|s1|s2|d|ah|ah1|ah2|ah3)"[^>]*>.*?</p>', re.DOTALL)
# Page break / chapter divs that contain only nested chapter info
CHAP_DIV_RE = re.compile(r'<div class="chapter"[^>]*>')
TAG_RE = re.compile(r'<[^>]+>')
WS_RE = re.compile(r'\s+')

def clean_text(s: str) -> str:
    # Remove section headings between verses
    s = HEADING_RE.sub('', s)
    # Remove footnote/cross-ref anchor blocks entirely
    s = ENREF_RE.sub('', s)
    s = SUP_RE.sub('', s)
    s = TAG_RE.sub('', s)
    s = html.unescape(s)
    s = s.replace('\u00a0', ' ')
    s = WS_RE.sub(' ', s).strip()
    return s

def build_index():
    """Returns dict: (book#, chap#, verse#) -> verse text."""
    index = {}
    files = sorted(glob.glob(os.path.join(OEBPS, "*.xhtml")))
    for fp in files:
        with open(fp, 'r', encoding='utf-8') as f:
            content = f.read()
        # Find all verse markers and their positions
        matches = list(VER_RE.finditer(content))
        for i, m in enumerate(matches):
            bk, ch, vs = int(m.group(1)), int(m.group(2)), int(m.group(3))
            start = m.end()
            end = matches[i+1].start() if i+1 < len(matches) else len(content)
            raw = content[start:end]
            # Cut off at end of paragraph if it occurs
            # But verses can span across paragraphs, so we just clean
            text = clean_text(raw)
            # Verse may continue in subsequent <p> in same file even after a closing </p>;
            # we already include all until next verse marker, which is what we want.
            key = (bk, ch, vs)
            if key not in index:
                index[key] = text
    return index

def parse_ref(ref: str):
    """Parse a reference like 'Genesis 1:3' or 'Psalm 23' or 'Matthew 6:9-13'.
    Returns list of (book#, chap, verse) tuples, or None.
    Handles multiple verses, ranges, and combined refs separated by '+', ',' or '/'.
    Returns list of segments where each segment is a list of (bk,ch,vs)."""
    ref = ref.strip()
    if not ref:
        return None
    # If the entire ref is just "Paraphrase (cf. XXX)" or similar, lift the cf. content out
    m_cf = re.match(r'^Paraphrase\s*\(cf\.\s*(.+?)\s*\)\s*$', ref, flags=re.IGNORECASE)
    if m_cf:
        ref = m_cf.group(1)
    # Strip any parenthetical comment
    ref = re.sub(r'\s*\([^)]*\)', '', ref)
    # Strip leading qualifiers: "Paraphrase of ", "cf. ", etc.
    ref = re.sub(r'^(Paraphrase of\s+|cf\.\s+)', '', ref, flags=re.IGNORECASE)
    # Handle multi-ref separated by " / " - take first occurrence only
    primary = re.split(r'\s*/\s*', ref)[0]
    primary = re.split(r'\s*\+\s*', primary)[0]
    primary = primary.strip()
    # Strip trailing notes
    primary = re.sub(r'\s*\(.*$', '', primary).strip()
    # Match: (Book name) (chap)[:vs[-vs2][, vs3...]]
    m = re.match(r'^((?:[1-3]\s+)?[A-Za-z]+(?:\s+of\s+\w+)?)\s+(\d+)(?::([\d,\s\-]+))?$', primary)
    if not m:
        return None
    book = m.group(1).strip()
    # Normalize book
    book = book.replace("Psalms", "Psalm")
    if book not in BOOK_NUMS:
        return None
    bk_num = BOOK_NUMS[book]
    chap = int(m.group(2))
    verses_part = m.group(3)
    verses = []
    if verses_part is None:
        # Whole chapter
        return ('chapter', bk_num, chap)
    # Split by comma
    for piece in verses_part.split(','):
        piece = piece.strip()
        if '-' in piece:
            a, b = piece.split('-', 1)
            try:
                a, b = int(a), int(b)
                verses.extend(range(a, b+1))
            except ValueError:
                continue
        else:
            try:
                verses.append(int(piece))
            except ValueError:
                continue
    return ('verses', bk_num, chap, verses)

def lookup(ref_str: str, index, chapter_max):
    """Return NBLA text for the reference, or empty string if non-biblical/unparseable."""
    if not ref_str or ref_str.strip() == "":
        return ""
    low = ref_str.lower()
    if 'not biblical' in low:
        return ""
    # Take only the first reference if multiple are listed
    parsed = parse_ref(ref_str)
    # If parsing failed, try without leading qualifiers
    if parsed is None:
        cleaned = re.sub(r'^(Paraphrase of\s+|cf\.\s+|Paraphrase combining\s+)', '', ref_str, flags=re.IGNORECASE)
        if cleaned != ref_str:
            parsed = parse_ref(cleaned)
    if parsed is None:
        return ""
    if parsed[0] == 'chapter':
        _, bk, ch = parsed
        # Get all verses in that chapter
        verses = sorted(v for (b, c, v) in index if b == bk and c == ch)
        if not verses:
            return ""
        parts = [f"{v} {index[(bk, ch, v)]}" for v in verses]
        return ' '.join(parts)
    else:
        _, bk, ch, vlist = parsed
        parts = []
        for v in vlist:
            txt = index.get((bk, ch, v))
            if txt:
                parts.append(f"{v} {txt}")
        return ' '.join(parts)

def main():
    print("Building NBLA index...")
    index = build_index()
    print(f"Indexed {len(index)} verses.")
    # Smoke test
    for k in [(1,1,3),(19,23,1),(43,3,16),(58,11,6)]:
        print(k, "=>", index.get(k, "(missing)")[:120])

    chapter_max = {}  # not strictly needed

    with open(INPUT_CSV, 'r', encoding='utf-8', newline='') as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = reader.fieldnames + ["NBLA text"]

    for row in rows:
        ref = row.get("Actual KJV reference", "")
        nbla = lookup(ref, index, chapter_max)
        row["NBLA text"] = nbla

    with open(OUTPUT_CSV, 'w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {OUTPUT_CSV}")

if __name__ == "__main__":
    main()
