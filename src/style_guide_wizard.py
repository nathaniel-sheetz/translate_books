"""
Style guide wizard: generates translation style guides from questionnaire answers.

Supports three modes:
1. Fixed-only: answer hardcoded questions, generate style guide from config effects
2. LLM-assisted: fixed questions + LLM-generated questions, LLM generates style guide
3. Manual: export prompts for copy/paste into external LLM
"""

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from src.models import StyleGuide
from src.text_feature_detector import (
    FeatureManifest,
    detect_all_features,
    filter_conditional_questions,
    manifest_summary,
)
from src.utils.file_io import (
    save_style_guide,
    load_style_guide,
    render_prompt,
    load_prompt_template,
)
from src.utils.source_text import load_clean_source_text

_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"

_log = logging.getLogger(__name__)


def _resolve_prompt_path(name: str) -> Path:
    """Return user's copy if it exists, otherwise fall back to .example file."""
    user_path = _PROMPTS_DIR / name
    if user_path.exists():
        return user_path
    example_path = _PROMPTS_DIR / (name.rsplit(".", 1)[0] + ".example." + name.rsplit(".", 1)[1])
    if example_path.exists():
        return example_path
    raise FileNotFoundError(f"Neither {user_path} nor {example_path} found")


def load_question_config(path: Optional[Path] = None) -> dict[str, list[dict]]:
    """Load question config as ``{"fixed": [...], "conditional": [...]}``.

    Accepts both the new dict-shaped config (``{"fixed": [...], "conditional": [...]}``)
    and the legacy flat-list format (treated as all-fixed, no conditional).
    """
    config_path = path or _resolve_prompt_path("style_guide_questions.json")
    with open(config_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return {"fixed": data, "conditional": []}
    if not isinstance(data, dict):
        raise ValueError(
            f"Question config at {config_path} must be a list or dict, got {type(data).__name__}"
        )
    return {
        "fixed": list(data.get("fixed", [])),
        "conditional": list(data.get("conditional", [])),
    }


def load_fixed_questions(path: Optional[Path] = None) -> list[dict]:
    """Load only the fixed questions (back-compat shim)."""
    return load_question_config(path)["fixed"]


def load_conditional_questions(path: Optional[Path] = None) -> list[dict]:
    """Load only the conditional questions."""
    return load_question_config(path)["conditional"]


def _slug(label: str) -> str:
    """Slugify an option label into a stable, terse id (lowercase, ``_``-joined)."""
    s = re.sub(r"[^a-z0-9]+", "_", str(label).strip().lower())
    return s.strip("_")[:48] or "option"


def option_ids(question: dict) -> list[str]:
    """Stable per-option ids for a question, aligned to ``question["options"]``.

    Ids are slugs derived from each option's label (order-independent and
    human-meaningful, unlike positional indices). Collisions within a single
    question are disambiguated with a ``_2``, ``_3`` … suffix.
    """
    ids: list[str] = []
    seen: dict[str, int] = {}
    for opt in question.get("options", []):
        base = _slug(opt.get("label", ""))
        n = seen.get(base, 0) + 1
        seen[base] = n
        ids.append(base if n == 1 else f"{base}_{n}")
    return ids


# Locale tokens (normalized: lowercased, non-alphanumerics stripped) → dialect
# option id. Keyed by the slugs that ``option_ids`` derives from the dialect
# question's labels, so a mapped id is always a real option.
_LOCALE_DIALECT_ALIASES: dict[str, str] = {
    alias: dialect_id
    for dialect_id, aliases in {
        "mexican_spanish": ("mx", "mex", "mexico", "esmx", "mexican"),
        # Bare ``es`` (region-less Spanish) deliberately maps to generic Latin
        # America, not Castilian: require an explicit Spain token (``es-ES`` →
        # ``eses``, ``esp``, ``spain``, ``castilian``…) to assert Castilian.
        "castilian_spanish": (
            "esp", "spain", "espana", "eses", "castilian", "iberian",
        ),
        "rioplatense_spanish": (
            "ar", "arg", "argentina", "esar", "uy", "uruguay", "esuy", "rioplatense",
        ),
        "colombian_spanish": ("co", "col", "colombia", "esco", "colombian"),
        "generic_latin_america": (
            "es", "latam", "la", "419", "es419", "generic", "latinamerica",
        ),
    }.items()
    for alias in aliases
}


def _normalize_locale(locale: str) -> str:
    """Lowercase a locale and strip everything but ``a-z0-9`` (``es-MX`` → ``esmx``)."""
    return re.sub(r"[^a-z0-9]+", "", str(locale).lower())


def dialect_id_from_locale(locale: str, dialect_question: dict) -> Optional[str]:
    """Map a setup ``locale`` (e.g. ``"mx"``, ``"es-MX"``, ``"Mexico"``) to a
    ``dialect`` option id, or ``None`` when it doesn't resolve.

    The returned id is validated against ``dialect_question``'s actual options
    (via :func:`option_ids`), so it always names a selectable option and stays
    correct if the labels are reworded. Used to pre-answer the redundant dialect
    question from the locale already chosen at ``setup``.
    """
    if not locale:
        return None
    mapped = _LOCALE_DIALECT_ALIASES.get(_normalize_locale(locale))
    if mapped and mapped in option_ids(dialect_question):
        return mapped
    return None


def resolve_answer(question: dict, answer: int | str) -> tuple[str, str, bool]:
    """Resolve a raw answer to ``(label, style_guide_effect, matched_option)``.

    Accepts, in priority order: a 0-based ``int`` (or numeric ``str``) index
    into the question's options (back-compat), an option **id** (slug) or exact
    option **label** (case/space-insensitive), or — failing all of those — free
    text, which is returned verbatim as a custom answer with ``matched=False``.
    """
    options = question.get("options", [])

    # int / numeric-string index — back-compat with the old positional contract.
    idx: int | None = None
    if isinstance(answer, bool):
        idx = None
    elif isinstance(answer, int):
        idx = answer
    elif isinstance(answer, str) and answer.strip().lstrip("-").isdigit():
        idx = int(answer.strip())
    if idx is not None and 0 <= idx < len(options):
        opt = options[idx]
        return opt.get("label", ""), opt.get("style_guide_effect", ""), True

    # id / label match.
    if isinstance(answer, str):
        # Collapse internal whitespace too, so a label matches by typing regardless
        # of irregular spacing — symmetric with option_ids' slug normalization.
        norm = " ".join(answer.split()).casefold()
        ids = option_ids(question)
        for i, opt in enumerate(options):
            label_norm = " ".join(str(opt.get("label", "")).split()).casefold()
            if norm == ids[i].casefold() or norm == label_norm:
                return opt.get("label", ""), opt.get("style_guide_effect", ""), True
        return answer.strip(), "", False

    # anything else (out-of-range int, etc.) — treat as custom text.
    return str(answer), "", False


def format_answered_questions(
    questions: list[dict],
    answers: dict[str, int | str],
    include_effects: bool = False,
) -> str:
    """Format questions and answers as readable text for prompt inclusion.

    Args:
        questions: list of question dicts (fixed or LLM-generated)
        answers: map of question id -> option index (int), option id/label, or
            custom text — see ``resolve_answer``
        include_effects: if True, append style_guide_effect text after each answer label
    """
    lines = []
    for q in questions:
        qid = q["id"]
        if qid not in answers:
            continue
        label, effect, _matched = resolve_answer(q, answers[qid])
        lines.append(f"- {q['question']} -> {label}")
        if include_effects and effect:
            lines.append(f"  {effect}")
    return "\n".join(lines)


def build_question_prompt(
    source_text: str,
    target_lang: str,
    locale: str,
    fixed_questions: list[dict],
    fixed_answers: dict[str, int | str],
    manifest: Optional[FeatureManifest] = None,
) -> str:
    """Build the prompt for LLM to generate additional questions.

    Note: we intentionally do NOT include the heuristic feature manifest in
    this prompt. The LLM should base additional question suggestions on the
    user-answered questions plus the source text sample.
    """
    template = _resolve_prompt_path("style_guide_questions.txt").read_text(encoding="utf-8")
    answered = format_answered_questions(fixed_questions, fixed_answers)
    variables = {
        "target_language": target_lang,
        "locale": locale,
        "answered_questions": answered,
        "source_text": source_text[:15000],  # Cap at ~15K chars
    }
    return render_prompt(template, variables)


def get_active_questions(
    project_dir: Optional[Path],
    *,
    config_path: Optional[Path] = None,
    manifest: Optional[FeatureManifest] = None,
    force: bool = False,
) -> tuple[list[dict], list[dict], FeatureManifest]:
    """Return (fixed_questions, active_conditional_questions, manifest).

    Loads the question config, runs / loads the feature manifest, then filters
    the conditional questions against it.
    """
    config = load_question_config(config_path)
    if manifest is None:
        if project_dir is None:
            manifest = FeatureManifest(features={}, generated_at="")
        else:
            manifest = detect_all_features(Path(project_dir), force=force)
    active_conditional = filter_conditional_questions(config["conditional"], manifest)
    return config["fixed"], active_conditional, manifest


def parse_llm_questions(response: str) -> list[dict]:
    """Parse LLM response into question dicts.

    Expects a JSON array. Handles responses wrapped in markdown code fences.
    """
    text = response.strip()
    # Strip markdown code fences
    match = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
    if match:
        text = match.group(1).strip()
    questions = json.loads(text)
    if not isinstance(questions, list):
        raise ValueError("Expected a JSON array of questions")
    # Validate structure
    for q in questions:
        if "id" not in q or "question" not in q or "options" not in q:
            raise ValueError(f"Question missing required fields: {q}")
    return questions


def build_style_guide_prompt(
    questions: list[dict],
    answers: dict[str, int | str],
    source_text: str,
    target_lang: str,
    locale: str,
    address_summary: str = "",
) -> str:
    """Build the prompt for LLM to generate a style guide from Q&A.

    ``address_summary`` is the approved address map's ``style_guide_summary`` when
    the address-map beat ran first. It is already written for a chunk-local reader,
    so the template reproduces it as the FORMS OF ADDRESS section rather than
    re-deriving that section from the single ``forms_of_address`` answer.
    """
    template = _resolve_prompt_path("style_guide_generate.txt").read_text(encoding="utf-8")
    qa_text = format_answered_questions(questions, answers, include_effects=True)
    variables = {
        "target_language": target_lang,
        "locale": locale,
        "questions_and_answers": qa_text,
        "source_text": source_text[:10000],
        "address_summary": address_summary.strip() or (
            "(no address map for this book — derive FORMS OF ADDRESS from the "
            "questionnaire answer above)"
        ),
    }
    return render_prompt(template, variables)


def parse_style_guide_response(response: str) -> str:
    """Extract style guide text from LLM response.

    Strips markdown fences if present, returns clean text.
    """
    text = response.strip()
    match = re.search(r"```(?:markdown|text)?\s*\n?(.*?)```", text, re.DOTALL)
    if match:
        text = match.group(1).strip()
    return text


def answers_to_style_guide_fallback(
    questions: list[dict],
    answers: dict[str, int | str],
) -> str:
    """Generate a style guide from answers WITHOUT LLM.

    Concatenates the style_guide_effect text of each selected option.
    For custom text answers, uses the custom text directly under the question's section.
    """
    sections = []
    for q in questions:
        qid = q["id"]
        if qid not in answers:
            continue
        label, effect, matched = resolve_answer(q, answers[qid])
        if matched:
            if effect:
                sections.append(effect)
        elif label.strip():
            # Custom text answer — use the question id as section header
            header = qid.upper().replace("_", " ")
            sections.append(f"{header}\n{label.strip()}")
    return "\n\n".join(sections)


def load_source_sample(project_dir: Path, max_words: int = 10000) -> str:
    """Load a source text sample from a project directory.

    Returns the first ``max_words`` words of the cleanest available source
    text. See ``src.utils.source_text.load_clean_source_text`` for the
    priority order (chapters → chunks → source.txt).
    """
    text, _, _ = load_clean_source_text(project_dir)
    if not text:
        return ""
    words = text.split()
    return " ".join(words[:max_words])


# ── light style guide ──────────────────────────────────────────────────────
#
# ``light_content`` is the <=2-sentence condensation the reader's single-sentence
# retranslate prompt uses in place of the full guide (see src/retranslator.py).
# The harness normally commits an agent-written one; the derivation below is the
# floor that keeps the field populated when nobody wrote one.

_LIGHT_MAX_CHARS = 400

# The input is 1-3 short declarative English sentences per section (the drafting
# prompt mandates it), so a narrow lookahead beats a general segmenter here: the
# repo's pysbd-backed split_sentences() pulls numpy in on Flask request paths and
# post-splits anything over 50 words, which would hand back a fragment.
_SENTENCE_SPLIT_RE = re.compile(r"""(?<=[.!?])\s+(?=[A-Z¿¡"'“‘])""")

# Sections are ALL-CAPS-headed blocks. The dialect section supplies sentence one,
# a voice/tone section sentence two.
_DIALECT_HEADING_RE = re.compile(r"\b(DIALECT|REGISTER|VARIETY|LANGUAGE)\b")
# No trailing \b — it would stop NARRAT from matching NARRATOR / NARRATIVE, which
# silently sends most books to the fallback. RHYTHM is deliberately absent:
# "SENTENCE RHYTHM" sections are syntax rules ("keep clauses long"), and matching
# them displaces the real tone sentence.
_TONE_HEADING_RE = re.compile(r"\b(VOICE|TONE|NARRAT)")
# "DIALOGUE AND VOICE" / "CHARACTER VOICE" are speech-rule sections, never narrator tone.
_NOT_TONE_HEADING_RE = re.compile(r"^(DIALOGUE|CHARACTER)\b")

_MAX_HEADING_CHARS = 60


def _is_heading_line(line: str) -> bool:
    return (
        any(ch.isalpha() for ch in line)
        and line == line.upper()
        and len(line) < _MAX_HEADING_CHARS
        and not line.endswith((".", "!", "?"))
    )


def _split_style_guide_sections(content: str) -> list[tuple[str, str]]:
    """Split a style guide into ``(heading, body)`` blank-line-separated blocks.

    ``heading`` is "" for a block that does not open with an ALL-CAPS label.
    A lone ALL-CAPS line is a heading for the next block, not a body — so a
    markdown-style blank line after the label does not emit the heading as
    the dialect sentence.
    """
    raw: list[list[str]] = []
    for block in re.split(r"\n\s*\n", content.strip()):
        lines = [line.strip() for line in block.strip().split("\n") if line.strip()]
        if lines:
            raw.append(lines)

    merged: list[list[str]] = []
    i = 0
    while i < len(raw):
        if len(raw[i]) == 1 and _is_heading_line(raw[i][0]) and i + 1 < len(raw):
            merged.append([raw[i][0], *raw[i + 1]])
            i += 2
        else:
            merged.append(raw[i])
            i += 1

    sections: list[tuple[str, str]] = []
    for lines in merged:
        head = lines[0]
        if _is_heading_line(head):
            sections.append((head, " ".join(lines[1:])))
        else:
            sections.append(("", " ".join(lines)))
    return sections


def _first_sentences(text: str, n: int) -> list[str]:
    """Return up to ``n`` sentences from ``text``."""
    parts = [s.strip() for s in _SENTENCE_SPLIT_RE.split(text.strip()) if s.strip()]
    return parts[:n]


def clamp_light_style_guide(text: str) -> str:
    """Keep at most two sentences; over the cap, drop the second rather than cut."""
    sents = _first_sentences(text, 2)
    if not sents:
        return ""
    light = f"{sents[0]} {sents[1]}".strip() if len(sents) > 1 else sents[0]
    if len(light) > _LIGHT_MAX_CHARS:
        light = sents[0]
    return light.strip()


def derive_light_style_guide(content: str) -> str:
    """Distill a <=2-sentence light style guide out of a full one.

    Sentence one states the dialect, from the guide's DIALECT/REGISTER section —
    every guide the pipeline writes opens with one, so this half is reliable.
    Sentence two states the high-level tone, from the first VOICE/TONE/NARRATOR
    section, falling back to the dialect section's own second sentence when the
    guide has no such section.

    Returns "" when nothing can be derived.
    """
    sections = _split_style_guide_sections(content)
    if not sections:
        return ""

    dialect_idx = next(
        (i for i, (head, _) in enumerate(sections) if _DIALECT_HEADING_RE.search(head)),
        0,
    )
    dialect_sents = _first_sentences(sections[dialect_idx][1], 2)
    if not dialect_sents:
        return ""

    tone = ""
    for i, (head, body) in enumerate(sections):
        if i == dialect_idx or not head:
            continue
        if _NOT_TONE_HEADING_RE.match(head) or not _TONE_HEADING_RE.search(head):
            continue
        found = _first_sentences(body, 1)
        if found:
            tone = found[0]
            break
    if not tone and len(dialect_sents) > 1:
        tone = dialect_sents[1]

    light = f"{dialect_sents[0]} {tone}".strip() if tone else dialect_sents[0]
    return clamp_light_style_guide(light)


def save_style_guide_json(
    content: str,
    output_path: Path,
    *,
    light_content: str | None = None,
) -> None:
    """Save a style guide to JSON, keeping the fields this call does not own.

    ``light_content`` resolves in three steps: the explicit argument, else a
    non-blank one already on disk, else one derived from ``content``. Rebuilding
    the model from scratch used to blank the field — and reset ``created_at``
    and ``version`` — on every write, silently discarding a light guide saved
    from the dashboard.

    The derivation lives here rather than in ``save_style_guide`` or on the model
    so it stays opt-in: the dashboard's own main-guide save builds its StyleGuide
    inline and must NOT invent a light guide behind the user's back.
    """
    now = datetime.now()
    existing_light: Optional[str] = None
    created_at = None
    version = None
    if output_path.exists():
        try:
            existing = load_style_guide(output_path)
            existing_light = existing.light_content
            created_at = existing.created_at
            version = existing.version
        except (OSError, ValueError) as exc:
            # A corrupt style.json must not block writing a good one over it.
            # UnicodeDecodeError is a ValueError, so a non-UTF-8 file is covered.
            _log.warning("Could not read existing style guide at %s: %s", output_path, exc)

    resolved = (light_content or "").strip() or (existing_light or "").strip()
    if not resolved:
        resolved = derive_light_style_guide(content)

    guide = StyleGuide(
        content=content,
        light_content=resolved or None,
        version=version or "1.0",
        created_at=created_at or now,
        updated_at=now,
    )
    save_style_guide(guide, output_path)
