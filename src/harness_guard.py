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
from src.utils.file_io import load_chunk, load_glossary, load_style_guide
from src.utils.text_utils import image_filename_counts, image_filenames


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
      - ``completeness_eval`` + ``length_eval`` ERROR-severity issues
        (placeholder text, empty, wildly off length); WARNINGS do not block.

        AGENT PROSE (untrusted)            guard_translation_draft           COMMIT
        ──────────────────────            ───────────────────────           ──────
        worker draft  ──────────►  empty? echo? image parity?  ──ok──►  apply_translation
                                   completeness/length ERROR?            + save_chunk
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
