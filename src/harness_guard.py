"""Validation guards for the translate-harness skill.

Harness mode lets an agent *be* the thinking-mode LLM: it drafts the glossary
proposals and the style guide in-conversation instead of an API call. Agent-authored
JSON is less reliable than a code path, so every artifact the agent produces is run
through a guard before the pipeline consumes it. A malformed draft fails loudly with a
re-draft-friendly message instead of poisoning the run (a KeyError 500 deep in the
pipeline, or a half-written glossary the translator silently trusts).

DRY note: this module does NOT re-define the schema. The contract lives in
``src/models.py`` (Pydantic ``Glossary`` / ``StyleGuide`` / ``Chunk``) and the existing
loaders in ``src/utils/file_io.py`` already raise on invalid data. The guards wrap those
loaders and the one parse boundary the loaders don't cover.

    AGENT DRAFT (untrusted JSON)                    GUARD                         PIPELINE
    ────────────────────────────                    ─────                         ────────
    glossary proposals  ─────────►  guard_glossary_proposals()  ──ok──►  glossary_terms_from_proposals
        [{english, translation,         │ missing "english"?                  (no KeyError)
          type, ...}, ...]              │ both translation+spanish empty?
                                        ▼ raise HarnessValidationError ──► agent re-drafts
    style.json / glossary.json  ─►  validate_*_file()  ──ok──►  pipeline reads file
        (written to disk)               │ Pydantic ValidationError /
                                        ▼ ValueError ──► HarnessValidationError ──► re-draft
"""

from __future__ import annotations

from pathlib import Path

from src.evaluators.completeness_eval import CompletenessEvaluator
from src.evaluators.length_eval import LengthEvaluator
from src.models import Chunk, IssueLevel
from src.utils.file_io import load_address_map, load_chunk, load_glossary, load_style_guide
from src.utils.text_utils import (
    footnote_token_counts,
    image_filename_counts,
)


class HarnessValidationError(Exception):
    """Raised when an agent-produced artifact fails validation.

    The message is written for the agent to read and act on: it names what is wrong
    so the skill's approval loop can re-draft instead of crashing the pipeline.
    """


# Proposal dicts feed ``glossary_terms_from_proposals`` (src/glossary_bootstrap.py:118),
# which does ``english=p["english"]`` — a hard KeyError if the agent omits the field.
# Guard that boundary before the dicts reach the bootstrap helper.
def guard_glossary_proposals(proposals: list[dict]) -> list[dict]:
    """Validate agent-drafted glossary proposals before model construction.

    Each proposal must be a dict with a non-empty ``english`` key and at least one of
    ``translation`` / ``spanish``. Returns the proposals unchanged when valid.

    Raises:
        HarnessValidationError: with a per-entry account of every problem, so the agent
            can fix all of them in one re-draft rather than one error at a time.
    """
    if not isinstance(proposals, list):
        raise HarnessValidationError(
            f"Glossary proposals must be a JSON array, got {type(proposals).__name__}. "
            "Re-draft as a list of {english, translation, type, context} objects."
        )

    problems: list[str] = []
    for i, p in enumerate(proposals):
        if not isinstance(p, dict):
            problems.append(f"  entry {i}: not an object ({type(p).__name__})")
            continue
        english = p.get("english")
        if not english or not str(english).strip():
            problems.append(f"  entry {i}: missing or empty 'english' (keys present: {sorted(p.keys())})")
            continue
        translation = str(p.get("translation") or p.get("spanish") or "").strip()
        if not translation:
            problems.append(f"  entry {i} ({english!r}): missing both 'translation' and 'spanish'")

    if problems:
        raise HarnessValidationError(
            "Glossary proposals are invalid — fix every entry below and re-draft:\n"
            + "\n".join(problems)
        )
    return proposals


# Languages whose orthography routinely carries diacritics. Keyed by the config's
# ``language_code`` (src/harness/state.py default "es") — stable and short, unlike the
# free-text ``target_language`` name. A code NOT in this set (e.g. "en") never warns.
DIACRITIC_LANGUAGE_CODES: frozenset[str] = frozenset({
    "es", "fr", "pt", "it", "de", "ca", "pl", "cs", "ro", "tr",
    "vi", "hu", "nl", "sv", "no", "da", "fi", "is", "sk", "sl", "hr",
})

# Below this many terms a glossary can legitimately be all proper names (ASCII), so the
# zero-diacritic signal is too noisy to act on. Above it, an accent-using language almost
# always carries at least one ñ/accented vowel — zero is the accent-stripping tell.
MIN_TERMS_FOR_DIACRITIC_CHECK = 8


def diacritic_warning(proposals: list[dict], language_code: str | None) -> str | None:
    """Soft, advisory smell-check for an accent-stripped glossary draft.

    The agent drafts the glossary in-conversation; "playing it safe" by writing pure ASCII
    (``Tia`` for ``Tía``, ``senor`` for ``señor``) silently ships the wrong canonical forms to
    every translator worker — and the structural guard (:func:`guard_glossary_proposals`) passes
    them clean. This catches that *one* failure mode and returns a human-readable warning the
    commit path surfaces to the agent and the human approval gate.

    Advisory by design: it NEVER raises and NEVER blocks. Untranslated proper names are
    legitimately ASCII, so a high ASCII fraction is normal; the actionable tell is **zero**
    non-ASCII across a non-trivial glossary in a diacritic-using target language.

    Returns the warning string, or ``None`` when nothing looks off (wrong/ASCII language,
    too few terms, or at least one term already carries a diacritic).
    """
    code = (language_code or "").strip().lower()
    if code not in DIACRITIC_LANGUAGE_CODES:
        return None
    if not isinstance(proposals, list):
        return None

    translations = [
        str((p.get("translation") or p.get("spanish") or "")).strip()
        for p in proposals
        if isinstance(p, dict)
    ]
    translations = [t for t in translations if t]
    if len(translations) < MIN_TERMS_FOR_DIACRITIC_CHECK:
        return None

    if any(any(ord(ch) > 127 for ch in t) for t in translations):
        return None

    return (
        f"Possible accent-stripping: all {len(translations)} glossary translations are pure ASCII, "
        f"but the target language ({code}) normally carries diacritics. The Write tool is UTF-8 — "
        "do not ASCII-fold. Re-read glossary.json and restore accents (e.g. Tía/señor/Día) before "
        "approving; these forms are fed verbatim to every translator worker."
    )


# Spanish title words that precede a personal name ("el tío Antony", "la señora Banks").
# Used to tell a *bare* personal name (one canonical form, no alternatives) apart from a
# title+name (narration form with article primary, bare vocative as the alternative).
SPANISH_TITLE_WORDS: frozenset[str] = frozenset({
    "tío", "tía", "señor", "señora", "señorita", "don", "doña",
    "doctor", "doctora", "profesor", "profesora", "padre", "madre",
    "hermano", "hermana", "capitán", "sargento", "coronel", "general",
    "reverendo", "primo", "prima", "abuelo", "abuela", "maestro", "maestra",
})

# Definite articles that mark the narration form of a title+name.
def _first_word(text: str) -> str:
    return text.split()[0].strip(".,;:").lower() if text.split() else ""


def glossary_convention_warnings(proposals: list[dict]) -> list[str]:
    """Advisory checks on the glossary's ``alternatives`` conventions.

    Alternatives are the one glossary field that lets a translator worker pick a
    *different* rendering per chunk — ``prompts/translation.txt`` tells it only
    "you may use them when appropriate". That freedom is right for a term genuinely
    rendered differently by context and wrong for a name, where it silently
    licenses book-wide inconsistency. The conventions checked here:

    * a **place** has one name — no alternatives (the rare exception being two
      interchangeable full/short forms of the *same* name, e.g. "Atlántico" /
      "océano Atlántico", which is why this warns rather than raises);
    * a **bare personal name** has one form — no alternatives;
    * a **title + personal name** leads with the narration form *including* the
      article ("el tío Antony") and offers the bare vocative ("tío Antony") as its
      single alternative, matching the style guide's narration-vs-address rule.
      Checked for ``character`` terms only — a *concept* that happens to open with
      a title word ("la madre superiora") is not a person being addressed.

    Advisory by design, like :func:`diacritic_warning`: it NEVER raises and NEVER
    blocks. Every message is prefixed ``REVIEW:`` so the approval gate can present
    these as human judgement calls, distinct from draft bugs. Returns [] when the
    draft is clean or unparseable (the structural guard owns that failure).
    """
    if not isinstance(proposals, list):
        return []

    from src.harness.address_rename import strip_article

    warnings: list[str] = []
    for p in proposals:
        if not isinstance(p, dict):
            continue
        english = str(p.get("english") or "").strip()
        translation = str(p.get("translation") or p.get("spanish") or "").strip()
        if not english or not translation:
            continue  # guard_glossary_proposals owns this failure
        term_type = str(p.get("type") or "other").strip().lower()
        raw_alts = p.get("alternatives") or []
        alts = [str(a).strip() for a in raw_alts if str(a).strip()] if isinstance(raw_alts, list) else []

        article, without_article = strip_article(translation)
        has_title = _first_word(without_article) in SPANISH_TITLE_WORDS

        if term_type == "place" and alts:
            warnings.append(
                f"REVIEW: '{english}' is a place with alternatives {alts}. Place names should "
                "have none — confirm these are interchangeable full/short forms of the same "
                "name (e.g. 'Atlántico' / 'océano Atlántico'), not variants, and drop them if not."
            )
            continue

        # Characters only: a `concept` rendered "la madre superiora" or "el padre
        # nuestro" opens with a title word without being a title + personal name,
        # and narration-vs-address does not apply to it. This matches the
        # bare-name check below, which has always been character-only.
        if term_type == "character" and has_title:
            if not article:
                warnings.append(
                    f"REVIEW: '{english}' is a title + name whose primary is '{translation}'. "
                    f"The narration form with the article ('el/la {translation}') should be the "
                    f"primary and the bare vocative ('{translation}') the alternative."
                )
            elif not alts:
                warnings.append(
                    f"REVIEW: '{english}' has the narration form '{translation}' but no vocative "
                    f"alternative. Add '{without_article}' for direct address."
                )
            continue

        if term_type == "character" and alts and not has_title:
            warnings.append(
                f"REVIEW: '{english}' is a bare personal name with alternatives {alts}. "
                "One person, one name — drop the alternatives unless there is a stated reason "
                "in 'context' for a human to review."
            )

    return warnings


def address_map_name_warnings(glossary, address_map) -> list[str]:
    """Warn when an address map's cast predates the approved glossary.

    The address map is drafted before the glossary exists (it is the first beat),
    so its pairs carry the **English** source names. Once the glossary fixes the
    target-language forms, the map should be updated to match — otherwise the
    ``address`` judge reads "Aunt Polly" while the translation says "la tía Polly"
    and has to bridge the gap on every call.

    Advisory: never raises, and silently returns [] once the map has been
    reconciled (or when either artifact is missing).

    **Everything it knows, it reads from the rename.** The surfaces come from
    ``address_rename.iter_surfaces`` and the sites from
    ``address_rename.classify_occurrences``, over the rules ``rename_rules``
    derives — so the check and the fix cannot disagree about which fields count,
    which *forms* count (article-stripped variants included), or which sites a
    machine may touch. Re-deriving any of that here is what produced warnings
    whose prescribed fix was a no-op (``Thor`` inside *authority*) and silences
    where a rename would have acted (a bare ``screech-owl``).

    Two warnings, because there are two different fixes:

    * **Stale** — sites ``address-map rename`` will rewrite. Reported per field,
      because a reconcile that updated only the pair names left ``content``
      saying "Aunt Polly" while the pairs said "la tía Polly"; a whole-map
      haystack saw the approved form *somewhere* and fell silent, so the judge
      went on reading English with nothing reporting it (real case:
      ``Redhead's wife`` in ``projects/the-house-on-the-cliff``).
    * **Hand-edit** — sites the rename deliberately leaves in English: edged by a
      hyphen or apostrophe (``a 1920s boys' adventure``, the quoted vocative
      ``'I'm sorry, Uncle Dock'``), or sitting inside another term's approved
      form. Re-running the rename cannot clear these, so the message says so and
      quotes the text instead of prescribing a command that would do nothing.

    A form that already reads the target is never reported: an approved form may
    contain its own English source (``Detective Smuff`` → ``el detective Smuff``),
    and flagging those books would be unclearable.
    """
    if glossary is None or address_map is None:
        return []

    from src.harness.address_rename import (
        MANUAL_KINDS,
        REWRITTEN_KINDS,
        classify_occurrences,
        context_snippet,
        iter_surfaces,
        rename_rules,
    )

    rules = rename_rules(glossary)
    if not rules:
        return []
    surfaces = [s for s in iter_surfaces(address_map) if s.text.strip()]
    if not surfaces:
        return []

    # source_english -> {target, paths, contexts}; insertion order == order of
    # first appearance in the map, so the message is stable across runs.
    stale: dict[str, dict] = {}
    manual: dict[str, dict] = {}
    for surface in surfaces:
        for occ in classify_occurrences(surface.text, rules):
            if occ.kind in REWRITTEN_KINDS:
                bucket = stale
            elif occ.kind in MANUAL_KINDS:
                bucket = manual
            else:
                continue  # already the approved form
            rule = rules[occ.rule_index]
            entry = bucket.setdefault(
                rule.source_english, {"target": rule.target, "paths": [], "contexts": []}
            )
            if surface.path not in entry["paths"]:
                entry["paths"].append(surface.path)
            entry["contexts"].append(
                (surface.path, context_snippet(surface.text, occ.start, occ.end))
            )

    def _sites(paths: list[str]) -> str:
        shown = ", ".join(paths[:3])
        return f"{shown}, +{len(paths) - 3} more" if len(paths) > 3 else shown

    warnings: list[str] = []
    if stale:
        listed = "; ".join(
            f"'{en}' → '{e['target']}' ({_sites(e['paths'])})"
            for en, e in list(stale.items())[:8]
        )
        more = f" (+{len(stale) - 8} more)" if len(stale) > 8 else ""
        warnings.append(
            f"REVIEW: address_map.json still uses English cast names for {len(stale)} approved "
            f"character term(s): {listed}{more}. The map was drafted before the glossary. Run "
            "`address-map rename` to apply the approved forms across the map (it writes a draft "
            "and reports every substitution), review it, then `address-map commit` — so the "
            "address judge reads the names that appear in the prose."
        )
    if manual:
        sites = sum(len(e["contexts"]) for e in manual.values())
        listed = "; ".join(
            f"'{en}' → '{e['target']}' at {e['contexts'][0][0]}: \"{e['contexts'][0][1]}\""
            for en, e in list(manual.items())[:5]
        )
        more = f" (+{len(manual) - 5} more)" if len(manual) > 5 else ""
        warnings.append(
            f"REVIEW: {sites} site(s) across {len(manual)} approved character term(s) still read "
            "English where `address-map rename` will FLAG but deliberately NOT rewrite them — the "
            "match is edged by a hyphen or apostrophe (part of a longer name, a possessive "
            "adjective, or a quoted vocative wanting the glossary's bare-vocative alternative), "
            "or it sits inside another term's approved form. Re-running the rename will not clear "
            f"these; edit the map text by hand or accept them as written: {listed}{more}."
        )
    return warnings


def guard_translation_draft(chunk: Chunk, prose: str) -> list[str]:
    """Validate a worker-produced translation draft before it is stamped.

    Harness Phase B lets a spawned subagent translate a chunk and write the
    prose to a file. That prose is untrusted (cheaper worker models wander), so
    the commit path runs it through this guard; a non-empty return flags the
    chunk for re-spawn instead of poisoning combine/align/epub with a bad chunk.

    Returns a list of human-readable problems (empty list == OK). Checks (eng
    review A4, 2026-06-10):

      - non-empty / not whitespace-only
      - NOT a verbatim echo of the English source (worker didn't translate)
      - image-token FILENAME parity vs source: ``[IMAGE:file]`` tokens are
        PRESERVED through translation (only the description is translated), so a
        dropped or hallucinated filename fails — the token's presence does not.
      - footnote-token NUMBER parity vs source: ``[FOOTNOTE:N]`` tokens are
        preserved exactly (same multiset of N); a dropped or hallucinated note
        fails the same way images do.
      - ``completeness_eval`` + ``length_eval`` ERROR-severity issues
        (placeholder text, empty, wildly off length); WARNINGS do not block.

        AGENT PROSE (untrusted)            guard_translation_draft           COMMIT
        ──────────────────────            ───────────────────────           ──────
        worker draft  ──────────►  empty? echo? image/footnote  ──ok──►  apply_translation
                                   parity? completeness/length            + save_chunk
                                   ERROR?
                                        │ problems
                                        ▼ return [..]  ──► re-spawn (cap 3) ─► manual
    """
    text = (prose or "").strip()
    if not text:
        return ["empty or whitespace-only translation"]

    problems: list[str] = []

    # Echo guard: exact match and near-verbatim overlap both catch untranslated prose.
    source_text = (chunk.source_text or "").strip()
    if text == source_text:
        problems.append("translation is a verbatim copy of the English source (not translated)")
    elif source_text:
        src_tokens = set(source_text.lower().split())
        out_tokens = set(text.lower().split())
        jaccard = len(src_tokens & out_tokens) / len(src_tokens | out_tokens) if (src_tokens | out_tokens) else 0.0
        if jaccard >= 0.85:
            problems.append(
                f"near-verbatim echo: token overlap with source is {jaccard:.0%} (≥85% threshold)"
            )

    # Image-token filename parity. Compare counts (Counter) not sets — a worker
    # that duplicates an image token would pass a set equality check.
    src_files = image_filename_counts(chunk.source_text)
    out_files = image_filename_counts(prose)
    if src_files != out_files:
        detail = []
        missing = sorted(src_files - out_files)
        extra = sorted(out_files - src_files)
        if missing:
            detail.append(f"dropped {missing}")
        if extra:
            detail.append(f"hallucinated {extra}")
        problems.append("image-token filename mismatch vs source: " + "; ".join(detail))

    # Footnote-token number parity (same Counter pattern as images).
    src_fns = footnote_token_counts(chunk.source_text or "")
    out_fns = footnote_token_counts(prose or "")
    if src_fns != out_fns:
        detail = []
        missing = sorted(src_fns - out_fns)
        extra = sorted(out_fns - src_fns)
        if missing:
            detail.append(f"dropped {missing}")
        if extra:
            detail.append(f"hallucinated {extra}")
        problems.append("footnote-token-parity: " + "; ".join(detail))

    # Reuse the existing evaluators; only ERROR-severity issues block the commit.
    eval_chunk = chunk.model_copy(update={"translated_text": text})
    for evaluator in (CompletenessEvaluator(), LengthEvaluator()):
        try:
            result = evaluator.evaluate(eval_chunk, {})
        except Exception as e:  # defensive: a broken evaluator shouldn't crash commit
            problems.append(f"{evaluator.name} evaluator failed: {e}")
            continue
        for issue in result.issues:
            if issue.severity == IssueLevel.ERROR:
                problems.append(f"{evaluator.name}: {issue.message}")

    return problems


def _wrap_loader(path: Path, loader, label: str):
    """Run an existing Pydantic loader, re-raising failures as HarnessValidationError."""
    try:
        return loader(Path(path))
    except FileNotFoundError as e:
        raise HarnessValidationError(f"{label} not found at {path}: {e}") from e
    except Exception as e:  # pydantic ValidationError, json.JSONDecodeError, ValueError
        raise HarnessValidationError(
            f"{label} at {path} failed validation: {e}\n"
            f"Re-draft so it matches the {label} schema in src/models.py."
        ) from e


def validate_glossary_file(path: Path):
    """Validate a written glossary.json against the Glossary model. Returns the Glossary."""
    return _wrap_loader(path, load_glossary, "Glossary")


def validate_style_guide_file(path: Path):
    """Validate a written style.json against the StyleGuide model. Returns the StyleGuide."""
    return _wrap_loader(path, load_style_guide, "Style guide")


def validate_chunk_file(path: Path):
    """Validate a chunk JSON file against the Chunk model. Returns the Chunk."""
    return _wrap_loader(path, load_chunk, "Chunk")


def validate_address_map_file(path: Path):
    """Validate a written address_map.json against the AddressMap model.

    The AddressMap validators already reject unknown forms/directions and a
    non-empty direction that lacks a ``when="default"`` fallback; this wraps that
    load so a bad agent draft fails with a re-draft-friendly message. Returns the
    AddressMap.
    """
    return _wrap_loader(path, load_address_map, "Address map")
