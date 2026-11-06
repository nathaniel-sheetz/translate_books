"""
Collections: one published volume bound from several independent works.

Each work is an ordinary project; a collection is only a thin layer above them,
so translation, judges, book-review and the passes all stay per member and
unchanged. A collection folder lives anywhere under ``projects/`` and holds:

- ``collection.json`` — membership only::

      {"version": 1, "group": "plumer",
       "members": [{"slug": "short-sermons-to-little-children",
                    "part_title": null, "part_page": "auto"},
                   {"slug": "plain-thoughts"}]}

  ``part_title`` defaults to the member's ``spanish_title``; ``part_page`` is
  ``"auto"`` (a part page only for a multi-chapter member), ``true`` or ``false``.
- ``project.json`` — the *volume's* metadata, the same keys a book carries, so
  :mod:`src.book_metadata` and ``kdp_publish.py apply`` work on it unchanged.
- the volume's own sidecars (``edition_notice.json``, ``translator_note.json``,
  ``kdp_listing.json``, ``cover/``, ``quotations.json``), named as for a book.

A collection dir has no ``chunks/`` or ``source.txt``, so it is never a project
(:func:`src.harness.state.is_project_dir`), and the project walker still
descends into it: a member may live inside the collection folder or anywhere
else, referenced by its unique slug as ``book_groups.json`` already does.

The members form a book group (:mod:`src.house.groups`), so story 2+ get their
style answers and glossary pre-filled from the earlier stories and then own
their files. Nothing is layered: no member reads another's glossary at
translate time.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional, Union

from src.harness import state

logger = logging.getLogger(__name__)

COLLECTION_FILE = "collection.json"
PROJECT_FILE = "project.json"
SCHEMA_VERSION = 1

PART_PAGE_AUTO = "auto"

PartPage = Union[str, bool]


@dataclass
class Member:
    """One work in a collection, as listed and as resolved on disk."""

    slug: str
    part_title: Optional[str] = None
    part_page: PartPage = PART_PAGE_AUTO
    #: The member's project dir, or ``None`` when the slug no longer resolves.
    path: Optional[Path] = None

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"slug": self.slug}
        if self.part_title:
            out["part_title"] = self.part_title
        if self.part_page != PART_PAGE_AUTO:
            out["part_page"] = self.part_page
        return out


@dataclass
class Collection:
    slug: str
    path: Path
    group: Optional[str] = None
    members: list[Member] = field(default_factory=list)

    @property
    def member_slugs(self) -> list[str]:
        return [m.slug for m in self.members]

    def member(self, slug: str) -> Optional[Member]:
        return next((m for m in self.members if m.slug == slug), None)

    def to_json(self) -> dict[str, Any]:
        doc: dict[str, Any] = {"version": SCHEMA_VERSION}
        if self.group:
            doc["group"] = self.group
        doc["members"] = [m.to_json() for m in self.members]
        return doc


# ---------------------------------------------------------------------------
# finding collections
# ---------------------------------------------------------------------------

def is_collection_dir(p: Path) -> bool:
    """A collection dir has ``collection.json`` and is not itself a project."""
    p = Path(p)
    return p.is_dir() and (p / COLLECTION_FILE).is_file() and not state.is_project_dir(p)


def iter_collection_dirs(root: Optional[Path] = None, _depth: int = 0) -> Iterator[Path]:
    """Yield collection dirs under ``root`` (default ``projects/``), sorted.

    Walks the way :func:`src.harness.state.iter_project_dirs` does: symlinks
    skipped, never into a project, depth-capped. It does descend into a
    collection, but collections inside collections are not supported, so what
    it finds there is only members.
    """
    root = Path(root) if root is not None else state.projects_root()
    if not root.exists() or _depth > state.PROJECT_WALK_MAX_DEPTH:
        return
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.is_symlink() or not entry.is_dir() or state.is_project_dir(entry):
            continue
        if is_collection_dir(entry):
            yield entry
        else:
            yield from iter_collection_dirs(entry, _depth + 1)


def resolve_collection_dir(collection: str) -> Path:
    """A collection slug (searched under ``projects/``) or a path to one.

    Raises ``FileNotFoundError`` when nothing matches, and ``ValueError`` when
    the path exists but is not a collection.
    """
    p = Path(collection)
    if p.is_absolute() or len(p.parts) > 1:
        if not p.exists():
            raise FileNotFoundError(f"collection path not found: {collection!r}")
        if not is_collection_dir(p):
            raise ValueError(f"{collection!r} is not a collection (no {COLLECTION_FILE})")
        return p
    found = [d for d in iter_collection_dirs() if d.name == collection]
    if not found:
        raise FileNotFoundError(f"collection not found: {collection!r}")
    if len(found) > 1:
        logger.warning("Duplicate collection id %r found at %s; using %s",
                       collection, [str(d) for d in found], found[0])
    return found[0]


def _project_slug_exists(slug: str) -> bool:
    try:
        return state.is_project_dir(state.resolve_project_dir(slug))
    except FileNotFoundError:
        return False


# ---------------------------------------------------------------------------
# reading and writing
# ---------------------------------------------------------------------------

def _read_json(path: Path) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_json(path: Path, doc: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _resolve_member_dir(slug: str) -> Optional[Path]:
    try:
        path = state.resolve_project_dir(slug)
    except FileNotFoundError:
        return None
    return path if state.is_project_dir(path) else None


def _parse_part_page(value: Any) -> PartPage:
    if isinstance(value, bool):
        return value
    return PART_PAGE_AUTO


def load_collection(collection_dir: Path) -> Collection:
    """Read ``collection.json`` and resolve each member's project dir.

    A member whose slug no longer resolves keeps ``path=None``; callers decide
    whether that is fatal (a build) or a row to report (``collection-status``).
    """
    collection_dir = Path(collection_dir)
    doc = _read_json(collection_dir / COLLECTION_FILE)
    if not isinstance(doc, dict):
        raise ValueError(f"{collection_dir / COLLECTION_FILE} is missing or not a JSON object")
    members: list[Member] = []
    for raw in doc.get("members") or []:
        if isinstance(raw, str):
            raw = {"slug": raw}
        if not isinstance(raw, dict) or not str(raw.get("slug") or "").strip():
            continue
        slug = str(raw["slug"]).strip()
        members.append(Member(
            slug=slug,
            part_title=(str(raw["part_title"]).strip() or None) if raw.get("part_title") else None,
            part_page=_parse_part_page(raw.get("part_page")),
            path=_resolve_member_dir(slug),
        ))
    group = str(doc.get("group") or "").strip() or None
    return Collection(slug=collection_dir.name, path=collection_dir, group=group, members=members)


def save_collection(coll: Collection) -> Path:
    path = coll.path / COLLECTION_FILE
    _write_json(path, coll.to_json())
    return path


def collections_of(project_slug: str, root: Optional[Path] = None) -> list[Collection]:
    """Every collection that lists ``project_slug`` as a member."""
    out = []
    for cdir in iter_collection_dirs(root):
        try:
            coll = load_collection(cdir)
        except ValueError:
            continue
        if project_slug in coll.member_slugs:
            out.append(coll)
    return out


def volume_metadata(coll: Collection) -> dict[str, Any]:
    """The collection's own ``project.json`` (``{}`` when absent)."""
    data = _read_json(coll.path / PROJECT_FILE)
    return data if isinstance(data, dict) else {}


def member_metadata(member: Member) -> dict[str, Any]:
    """A member's ``project.json`` plus its resolved title/author (``{}`` when unresolved)."""
    if member.path is None:
        return {}
    from src.book_metadata import load_book_metadata

    project = _read_json(member.path / PROJECT_FILE)
    project = project if isinstance(project, dict) else {}
    meta = load_book_metadata(member.path)
    return {**project, "resolved_title": meta["title"], "resolved_author": meta["author"]}


def part_title(member: Member) -> str:
    """``part_title`` -> the member's Spanish title -> its slug."""
    if member.part_title:
        return member.part_title
    meta = member_metadata(member)
    return str(meta.get("spanish_title") or meta.get("resolved_title") or member.slug).strip()


def member_authors(coll: Collection) -> list[str]:
    """Distinct member authors, in member order (blank authors skipped)."""
    seen: list[str] = []
    for m in coll.members:
        author = str(member_metadata(m).get("resolved_author") or "").strip()
        if author and author not in seen:
            seen.append(author)
    return seen


# ---------------------------------------------------------------------------
# membership changes
# ---------------------------------------------------------------------------

def _group_kind(kind: Optional[str]) -> str:
    from src.house.groups import DOMAIN, KINDS

    if kind is None:
        return DOMAIN
    if kind not in KINDS:
        raise ValueError(f"unknown group kind {kind!r} (expected one of {', '.join(KINDS)})")
    return kind


def _join(coll: Collection, slug: str, kind: Optional[str]) -> dict[str, Any]:
    from src.house.groups import join_group, load_groups

    existing = load_groups().get(coll.group or "")
    joined = join_group(slug, coll.group, kind=_group_kind(kind))
    if existing is not None and kind and existing["kind"] != kind:
        joined = {**joined, "warning": (
            f"group {coll.group!r} already exists as a {existing['kind']}; "
            f"--group-kind {kind} was not applied")}
    return joined


def _require_members(slugs: list[str]) -> list[str]:
    """Resolve each slug to an existing project; return the leaf slugs."""
    out: list[str] = []
    for raw in slugs:
        path = _resolve_member_dir(raw)
        if path is None:
            raise FileNotFoundError(f"no project {raw!r}: add each work with `setup` first")
        if path.name in out:
            raise ValueError(f"{path.name!r} is listed twice")
        out.append(path.name)
    return out


def stale_precedent_warnings(coll: Collection) -> list[dict[str, Any]]:
    """Members whose precedent snapshot predates some sibling in the collection.

    The snapshot is taken when drafting starts and is never rewritten behind the
    book's back, so a member set up before it joined still has a snapshot that
    does not know its new siblings. The next step for it is ``precedent``.
    """
    from src.house.precedent import cached_precedent

    out = []
    for m in coll.members:
        if m.path is None:
            continue
        doc = cached_precedent(m.path)
        if doc is None:
            continue
        known = {s.get("slug") for s in doc.get("siblings") or [] if isinstance(s, dict)}
        missing = [s for s in coll.member_slugs if s != m.slug and s not in known]
        if missing:
            out.append({
                "member": m.slug,
                "built_at": doc.get("built_at"),
                "missing_siblings": missing,
                "next": f"precedent --project {m.slug}",
            })
    return out


def create(
    slug: str,
    *,
    members: list[str],
    title: Optional[str] = None,
    group: Optional[str] = None,
    group_kind: Optional[str] = None,
    parent: Optional[str] = None,
) -> tuple[Collection, dict[str, Any]]:
    """Make a collection from existing projects and join them to its group.

    ``group`` defaults to the collection slug (created with ``group_kind``,
    ``domain`` unless told ``series``); naming an existing group reuses it.
    ``parent`` is a folder under ``projects/`` to create it in (``.macdonald``).

    Returns ``(collection, report)`` where the report carries each join and the
    stale-precedent warnings.
    """
    slug = state.slugify(slug)
    if _project_slug_exists(slug):
        raise ValueError(f"{slug!r} is already a project slug; pick another name for the collection")
    if any(d.name == slug for d in iter_collection_dirs()):
        raise ValueError(f"collection {slug!r} already exists")
    if not members:
        raise ValueError("a collection needs at least one member")
    member_slugs = _require_members(members)
    _group_kind(group_kind)  # validate before anything is written

    base = state.projects_root()
    if parent:
        parent_path = Path(parent)
        if parent_path.is_absolute() or ".." in parent_path.parts:
            raise ValueError("--parent is a folder under projects/, not a path out of it")
        base = base / parent_path
    cdir = base / slug
    # A folder that already holds the members is fine to adopt; one with a
    # project.json of its own belongs to something else.
    if (cdir / PROJECT_FILE).exists():
        raise ValueError(f"{cdir} already has a project.json; refusing to adopt it")

    coll = Collection(slug=slug, path=cdir, group=(group or slug).strip(),
                      members=[Member(slug=s, path=_resolve_member_dir(s)) for s in member_slugs])
    cdir.mkdir(parents=True, exist_ok=True)
    project: dict[str, Any] = {}
    if title:
        project["spanish_title"] = title
    _write_json(cdir / PROJECT_FILE, project)
    save_collection(coll)

    joins = [_join(coll, s, group_kind) for s in member_slugs]
    return coll, {"joined": joins, "stale_precedent": stale_precedent_warnings(coll)}


def add_member(coll: Collection, slug: str, *, position: Optional[int] = None,
               group_kind: Optional[str] = None) -> dict[str, Any]:
    """Append (or insert at 1-based ``position``) an existing project."""
    (member_slug,) = _require_members([slug])
    if member_slug in coll.member_slugs:
        raise ValueError(f"{member_slug!r} is already in {coll.slug!r}")
    if member_slug == coll.slug:
        raise ValueError("a collection cannot contain itself")
    member = Member(slug=member_slug, path=_resolve_member_dir(member_slug))
    if position is None:
        coll.members.append(member)
    else:
        coll.members.insert(max(0, min(len(coll.members), position - 1)), member)
    save_collection(coll)
    joined = _join(coll, member_slug, group_kind) if coll.group else None
    return {"joined": joined, "stale_precedent": stale_precedent_warnings(coll)}


def remove_member(coll: Collection, slug: str, *, keep_group: bool = False) -> dict[str, Any]:
    """Drop a member; it leaves the collection's group unless ``keep_group`` or
    another collection sharing that group still lists it."""
    from src.house.groups import leave_group

    if slug not in coll.member_slugs:
        raise ValueError(f"{slug!r} is not in {coll.slug!r}")
    coll.members = [m for m in coll.members if m.slug != slug]
    save_collection(coll)
    left = None
    kept_reason = None
    if coll.group and not keep_group:
        others = [c.slug for c in collections_of(slug)
                  if c.slug != coll.slug and c.group == coll.group]
        if others:
            kept_reason = f"still in group {coll.group!r} through {', '.join(others)}"
        else:
            try:
                left = leave_group(slug, coll.group)
            except ValueError as exc:
                kept_reason = str(exc)
    elif keep_group:
        kept_reason = "--keep-group"
    return {"left_group": left, "kept_group": kept_reason}


def reorder(coll: Collection, slugs: list[str]) -> None:
    """Put the members in exactly the order given (a permutation of them)."""
    if sorted(slugs) != sorted(coll.member_slugs) or len(set(slugs)) != len(slugs):
        raise ValueError(
            f"reorder needs every member exactly once: {', '.join(coll.member_slugs)}")
    by_slug = {m.slug: m for m in coll.members}
    coll.members = [by_slug[s] for s in slugs]
    save_collection(coll)
