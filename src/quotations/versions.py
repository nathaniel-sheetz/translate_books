"""
The one Bible version the pass knows: the NBLA, hardcoded.

No per-book setting and no version framework: running the pass is the opt-in.
The notice is the Spanish wording already on short-sermons' copyright page
(``edition_notice.json``). The limits are Lockman's permission-to-quote terms,
read 2026-10-28: up to 500 verses, never a complete book of the Bible, and under
25% of the work. Past them, commercial print needs HarperCollins' written
approval. This pass only records how a book stands against them; kdp-publish
decides what to do about it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class BibleVersion:
    id: str
    name: str
    notice: str
    limits: dict[str, Any] = field(default_factory=dict)


NBLA = BibleVersion(
    id="nbla",
    name="Nueva Biblia de las Américas",
    notice=(
        "Escrituras tomadas de la Nueva Biblia de las Américas (NBLA), "
        "Copyright © 2005 por The Lockman Foundation. Usadas con permiso. "
        "www.NuevaBiblia.com"
    ),
    limits={"verses": 500, "share": 0.25, "whole_book": False},
)

VERSIONS = {NBLA.id: NBLA}
