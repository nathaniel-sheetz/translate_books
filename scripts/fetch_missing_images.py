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
            raise FileNotFoundError(f"project.json not found at {pj}; pass base_url instead")
        data = json.loads(pj.read_text(encoding="utf-8"))
        url = data.get("gutenberg_url")
        if not url:
            raise ValueError("project.json has no 'gutenberg_url'; pass base_url instead")
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


def list_missing_images(project_dir: Path) -> list[str]:
    """Return the list of image filenames referenced by source.txt that are not present on disk.

    Returns an empty list if there is no source.txt or no image references.
    """
    source_path = project_dir / "source.txt"
    if not source_path.exists():
        return []
    images_dir = project_dir / "images"
    placeholders = extract_placeholders(source_path)
    missing: list[str] = []
    for rel in placeholders:
        filename = Path(rel).name
        if not (images_dir / filename).exists():
            missing.append(filename)
    return missing


def fetch_missing_images(
    project_dir: Path,
    base_url: str | None = None,
    force: bool = False,
    log: callable = None,
) -> dict:
    """Download any missing images for an ingested Gutenberg project.

    Returns a dict: {downloaded, skipped_existing, failed: [(url, err), ...],
                     placeholders: int}.
    Raises if there is no source.txt or no resolvable base URL.
    """
    source_path = project_dir / "source.txt"
    if not source_path.exists():
        raise FileNotFoundError(f"source.txt not found at {source_path}")

    resolved_base = load_base_url(project_dir, base_url)
    images_dir = project_dir / "images"
    images_dir.mkdir(exist_ok=True)

    placeholders = extract_placeholders(source_path)
    downloaded = 0
    skipped_existing = 0
    failed: list[tuple[str, str]] = []

    for rel in placeholders:
        filename = Path(rel).name
        dest = images_dir / filename
        if dest.exists() and not force:
            skipped_existing += 1
            continue

        abs_url = urllib.parse.urljoin(resolved_base, rel)
        try:
            r = requests.get(abs_url, headers={"User-Agent": USER_AGENT}, timeout=20)
            r.raise_for_status()
            dest.write_bytes(r.content)
            downloaded += 1
            if log:
                log(f"  ok   {filename}")
        except Exception as exc:
            failed.append((abs_url, str(exc)))
            if log:
                log(f"  FAIL {filename}: {exc}")

    return {
        "downloaded": downloaded,
        "skipped_existing": skipped_existing,
        "failed": failed,
        "placeholders": len(placeholders),
        "base_url": resolved_base,
    }


def main():
    args = parse_args()
    project_dir = Path(args.project_dir)
    source_path = project_dir / "source.txt"
    if not source_path.exists():
        sys.exit(f"source.txt not found at {source_path}")

    try:
        result = fetch_missing_images(
            project_dir,
            base_url=args.base_url,
            force=args.force,
            log=lambda msg: print(msg) if not msg.startswith("  FAIL") else print(msg, file=sys.stderr),
        )
    except (FileNotFoundError, ValueError) as exc:
        sys.exit(str(exc))

    if result["placeholders"] == 0:
        print("No image placeholders found in source.txt.")
        return

    print(f"Found {result['placeholders']} unique image references in source.txt")
    print(f"Base URL: {result['base_url']}")
    print(f"Output  : {project_dir / 'images'}/")
    print()
    print(f"Downloaded     : {result['downloaded']}")
    print(f"Already present: {result['skipped_existing']}")
    print(f"Failed         : {len(result['failed'])}")
    if result["failed"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
