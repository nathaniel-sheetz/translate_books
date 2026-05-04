#!/usr/bin/env python3
"""
Fetch images for an already-ingested Gutenberg project.

Reads [IMAGE:images/<file>:<alt>] placeholders out of source.txt, joins each
filename against the project's `gutenberg_url` (from project.json), and
downloads any that are missing into <project>/images/.

Useful when ingest_gutenberg.py was run with --no-images, or when image
downloads failed mid-run.

Usage:
    python scripts/fetch_missing_images.py projects/mybook/
    python scripts/fetch_missing_images.py projects/mybook/ --base-url https://...
    python scripts/fetch_missing_images.py projects/mybook/ --force
"""

import argparse
import json
import re
import sys
import urllib.parse
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("requests is required: pip install requests")


PLACEHOLDER_RE = re.compile(r"\[IMAGE:(images/[^\]:]+)(?::[^\]]*)?\]")
USER_AGENT = (
    "Mozilla/5.0 (compatible; book-translation-tool/1.0; "
    "+https://github.com/example/translate-books)"
)


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("project_dir", help="Path to the project directory (contains source.txt and project.json)")
    p.add_argument("--base-url", help="Override base URL (defaults to project.json gutenberg_url)")
    p.add_argument("--force", action="store_true", help="Re-download even if the file already exists")
    return p.parse_args()


def load_base_url(project_dir: Path, override: str | None) -> str:
    if override:
        url = override
    else:
        pj = project_dir / "project.json"
        if not pj.exists():
            sys.exit(f"project.json not found at {pj}; pass --base-url instead")
        data = json.loads(pj.read_text(encoding="utf-8"))
        url = data.get("gutenberg_url")
        if not url:
            sys.exit("project.json has no 'gutenberg_url'; pass --base-url instead")
    parsed = urllib.parse.urlparse(url)
    clean = urllib.parse.urlunparse(parsed._replace(fragment=""))
    return clean.rsplit("/", 1)[0] + "/"


def extract_placeholders(source_path: Path) -> list[str]:
    text = source_path.read_text(encoding="utf-8")
    seen: set[str] = set()
    out: list[str] = []
    for m in PLACEHOLDER_RE.finditer(text):
        rel = m.group(1)
        if rel not in seen:
            seen.add(rel)
            out.append(rel)
    return out


def main():
    args = parse_args()
    project_dir = Path(args.project_dir)
    source_path = project_dir / "source.txt"
    if not source_path.exists():
        sys.exit(f"source.txt not found at {source_path}")

    base_url = load_base_url(project_dir, args.base_url)
    images_dir = project_dir / "images"
    images_dir.mkdir(exist_ok=True)

    placeholders = extract_placeholders(source_path)
    if not placeholders:
        print("No image placeholders found in source.txt.")
        return

    print(f"Found {len(placeholders)} unique image references in source.txt")
    print(f"Base URL: {base_url}")
    print(f"Output  : {images_dir}/")
    print()

    downloaded = 0
    skipped_existing = 0
    failed: list[tuple[str, str]] = []

    for rel in placeholders:
        filename = Path(rel).name
        dest = images_dir / filename
        if dest.exists() and not args.force:
            skipped_existing += 1
            continue

        abs_url = urllib.parse.urljoin(base_url, rel)
        try:
            r = requests.get(abs_url, headers={"User-Agent": USER_AGENT}, timeout=20)
            r.raise_for_status()
            dest.write_bytes(r.content)
            downloaded += 1
            print(f"  ok   {filename}")
        except Exception as exc:
            failed.append((abs_url, str(exc)))
            print(f"  FAIL {filename}: {exc}", file=sys.stderr)

    print()
    print(f"Downloaded     : {downloaded}")
    print(f"Already present: {skipped_existing}")
    print(f"Failed         : {len(failed)}")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
