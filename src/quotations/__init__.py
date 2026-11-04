"""
The quotation pass: the NBLA in a finished translation.

An optional post-translation pass for books that quote Scripture. A headless
wave finds the quotations in the aligned chapters (:mod:`.scan`), code looks the
verses up in a per-user Bible index (:mod:`.index`, :mod:`.refs`), a second wave
picks the part of each verse the author quotes and the nearby prose that should
echo it (:mod:`.map`), code applies the NBLA's conventions (:mod:`.conventions`),
and the user approves every change on a board before it goes through the
judge-apply path. The exact verse and word counts go to
``projects/<slug>/quotations.json``, which kdp-publish reads; the copyright
decision is kdp-publish's, not this pass's.

The Bible text never enters the repo. The index lives under ``prompts/bible/``
(gitignored) and has no tracked fallback: a missing index, or one built from an
EPUB that has since changed, is an error, so the pass can never splice some
other version under the NBLA's name.
"""

import json
import os
from pathlib import Path
from typing import Any

#: Name of this pass's working directory under ``<project>/.harness/``.
DIRNAME = "quotations"

#: Where per-user Bible indexes live: ``prompts/bible/<version>.json``. Gitignored.
INDEX_DIR = Path(__file__).resolve().parents[2] / "prompts" / "bible"


def quotations_dir(project_dir: Path) -> Path:
    return Path(project_dir) / ".harness" / DIRNAME


def write_json(path: Path, doc: Any) -> None:
    """Whole or not at all: a kill mid-write never leaves half a state file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
