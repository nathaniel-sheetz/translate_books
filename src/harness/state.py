"""Per-project temp/config state for the translate-harness CLI.

Harness mode used to scatter intermediate files across a repo-global ``.tmp/``
directory and hand-pass them between inline-Python snippets in SKILL.md. That
collided across books (one ``.tmp/`` shared by every project) and put untested
orchestration in markdown. This module gives every harness command one place to:

  * resolve a project directory from an id or a path, and
  * resolve per-project ``projects/<slug>/.harness/`` working paths, and
  * load/save a small persisted ``config.json`` (target language, locale,
    provider, model, title, author) so commands stop hardcoding ``"Spanish"`` /
    ``"mx"`` / a default model the way the old heredocs did.
"""

from __future__ import annotations

import json
import logging
import re
import shlex
import shutil
from datetime import datetime
from pathlib import Path
from typing import Mapping, Sequence

_log = logging.getLogger(__name__)

# src/harness/state.py -> parents[0]=harness, [1]=src, [2]=repo root.
REPO_ROOT = Path(__file__).resolve().parents[2]

# Defaults applied when config.json is absent or a key is missing. These replace
# the values the old SKILL.md heredocs hardcoded inline.
DEFAULTS: dict[str, object] = {
    "target_language": "Spanish",
    "locale": "mx",
    "provider": "anthropic",
    "model": "claude-sonnet-5",
    "language_code": "es",
    "title": "",
    "author": "",
    # When true, the DIALOGUE FORMATTING block is placed on every chunk's prompt
    # (not only dialogue-bearing chunks) so it sits in the byte-identical, cacheable
    # fixed prefix. See build_translation_prompt / prompts/translation.txt.
    #
    # Tri-state, like its image sibling below. None / absent means auto: on when
    # any chunk in the book has dialogue and the target is Spanish. Auto is the
    # right default because it only fires where it pays — a book whose chunks ALL
    # have dialogue (or none do) already renders a stable prefix, and forcing the
    # block on there would be pure token overhead for no cache gain. It is the
    # MIXED book whose prefix diverges chunk to chunk and runs uncached.
    # Explicitly persisted True/False (from `setup --always-dialogue` or
    # `config-set`) always wins, so a book mid-translation never changes rendering
    # underneath itself.
    "always_include_dialogue": None,
    # When true, the constant image-placeholder bullet is placed on every chunk.
    # None / absent means auto (on when any chunk has [IMAGE:...] placeholders).
    # Stored as bool when the user sets --always-images / --no-always-images at
    # setup, or `config-set --key always_include_image_instructions`.
    "always_include_image_instructions": None,
    # Which CLI family the headless backend drives (``claude -p`` vs ``cursor-agent``).
    # Backend stays ``headless``; this only selects the launcher profile.
    #
    # ``auto`` (the default) means "follow whichever agent is driving" — see
    # :func:`src.harness.profile.resolve_profile`, which maps a detected Claude
    # Code host to ``claude`` and a Cursor host to ``cursor``. It is the default
    # because ``claude`` could not be one: ``save_config`` merges DEFAULTS into
    # every write, so a literal ``"claude"`` on disk was indistinguishable from a
    # book that had never chosen — and a Cursor operator therefore got a
    # Claude-shaped worker, baseline and effort with no way for the harness to
    # know better (2026-08-11 friction logs). Books that DO carry a literal
    # ``claude``/``cursor`` keep it and outrank detection.
    #
    # Never read this raw when you need a launcher profile — ``auto`` is not one.
    # Use :func:`resolved_headless_cli`, which never returns it.
    "headless_cli": "auto",
    # Extra argv appended to every ``claude -p`` job in a headless wave, to trim
    # what the child loads into its system prompt (``--strict-mcp-config``,
    # ``--setting-sources ""``, ``--safe-mode`` …). A list of strings. Ignored on
    # the Cursor profile. Each wave records the flags it ran under in
    # ``.harness/*/usage.jsonl``, so this doubles as the A/B knob: change the
    # list, run a wave, compare ``overhead_ratio`` in the log.
    #
    # NEVER put ``--bare`` here. Its auth is strictly ANTHROPIC_API_KEY or an
    # apiKeyHelper — "OAuth and keychain are never read" — which is exactly what
    # ``subscription_auth_error`` exists to guarantee against.
    "headless_extra_flags": [],
    # ``headless_effort_<type>`` (one key per wave type) is appended below, once
    # COMMAND_EFFORT_DEFAULTS exists — see effort_config_key.
    # Prompt-cache TTL for headless Claude waves. ``auto`` (default) picks
    # ``5m`` / ``1h`` / ``off`` from job shapes and prior wall times; see
    # :func:`~src.harness.headless.resolve_cache_mode`. Claude-only — Cursor
    # ignores it.
    "headless_prompt_cache": "auto",
}

# Headless launcher families, plus the ``auto`` sentinel that defers to the
# detected host. ``auto`` is a *config* value only — it is never a launcher
# profile, so it must not reach ``headless._normalize_cli``.
HEADLESS_CLIS = ("claude", "cursor")
CLI_VALUES = frozenset(HEADLESS_CLIS) | {"auto"}

# Claude ``--effort`` levels the CLI accepts, plus the two harness sentinels.
EFFORT_LEVELS = ("low", "medium", "high", "xhigh")
# "auto" -> per-command table below; "default" -> pass no --effort at all.
EFFORT_VALUES = frozenset(EFFORT_LEVELS) | {"auto", "default"}

# Claude prompt-cache TTL modes (see ``src.harness.headless``).
CACHE_MODES = ("auto", "5m", "1h", "off")
CACHE_VALUES = frozenset(CACHE_MODES)

# Effort each wave type runs at when its config key is "auto". medium is measured
# only on judge waves (2026-07-31: output -66%, wall -66%, cost -55%, no quality
# loss on hand-verified findings). Translate/footnotes are long literary prose and
# reduced effort there is unmeasured, so they name the band the CLI was already
# using — explicit, so it shows up in argv, usage rows and ``status`` instead of
# being an invisible property of whichever CLI build is installed.
# ``None`` is legal and means "emit no ``--effort``" (whatever the CLI does).
COMMAND_EFFORT_DEFAULTS: dict[str, str | None] = {
    "judges": "medium",
    "annotations": "medium",
    "translate": "high",
    "footnotes": "high",
}


# Per-CLI view of the table above. The rows are *measurements*, not preferences:
# they move when someone runs a probe and writes a friction log, i.e. in a code
# change with a comment — which is why this lives here and not in config.json.
# The per-book override stays in config, where it already is
# (``headless_effort_<type>``).
#
# The cursor row is identical to the claude row today, on purpose: no Cursor
# effort sweep has been run, so inventing different numbers would be fiction.
# It exists as the seam a future Cursor measurement lands in without having to
# re-thread a CLI argument through every caller first.
CLI_COMMAND_EFFORT_DEFAULTS: dict[str, dict[str, str | None]] = {
    "claude": COMMAND_EFFORT_DEFAULTS,
    "cursor": dict(COMMAND_EFFORT_DEFAULTS),
}


def command_effort_default(command: str, *, cli: str | None = None) -> str | None:
    """Effort this wave type runs at when nothing else says, for this CLI family.

    ``cli=None`` (or an unknown family) reads the claude row, which is the
    historical answer — so an un-threaded caller keeps today's behaviour.
    """
    table = CLI_COMMAND_EFFORT_DEFAULTS.get(
        (cli or "").strip().lower(), COMMAND_EFFORT_DEFAULTS
    )
    return table.get(command)


def resolved_headless_cli(
    cfg: Mapping[str, object] | None = None, override: str | None = None
) -> str:
    """The launcher family to actually drive: ``claude`` or ``cursor``, never ``auto``.

    ``override`` (a per-run ``--cli``) wins, then ``cfg["headless_cli"]`` when it
    names a real family, else ``claude``.

    This is the back-compat shim for every caller that only needs *a* profile and
    has no host to consult: ``auto`` and any mistyped value both land on
    ``claude``, which is what those call sites did before ``auto`` existed.
    Callers that want detection to have a say go through
    :func:`src.harness.profile.resolve_profile` instead — it reports where the
    answer came from, which this cannot.
    """
    candidate = (override or "").strip().lower()
    if candidate in HEADLESS_CLIS:
        return candidate
    raw = (cfg or {}).get("headless_cli")
    candidate = str(raw or "").strip().lower()
    if candidate in HEADLESS_CLIS:
        return candidate
    return "claude"


def effort_config_key(command: str) -> str:
    """Per-book config key holding the effort level for one wave type.

    One key per wave type (``headless_effort_judges``, ``…_translate``, …) so
    tuning judge waves can never move book prose. Derived from ``command`` — the
    same string ``resolve_headless_argv`` and ``COMMAND_EFFORT_DEFAULTS`` use —
    so the key name has exactly one definition.
    """
    return f"headless_effort_{command}"


# Each defaults to "auto" (use the table above); "default" means emit no --effort.
DEFAULTS.update({effort_config_key(cmd): "auto" for cmd in COMMAND_EFFORT_DEFAULTS})

# Config keys a command may override (CLI flag -> config key); used by setup.
CONFIG_KEYS = tuple(DEFAULTS.keys())


# ── machine-readable result sentinel (streaming wrapped scripts) ──────────────
#
# The streaming harness commands (chunk/cost/translate/epub) wrap a subprocess
# whose human-facing progress goes to stdout. To ALSO give the agent a fresh,
# structured last_output.json (friction-log #18 — those commands used to leave the
# previous command's result in place), the wrapped script prints exactly one line
# ``HARNESS_RESULT: {...json...}``; ``flow._run_script`` tees the child's stdout,
# strips that one line, and returns the parsed dict as the command's result.
HARNESS_RESULT_PREFIX = "HARNESS_RESULT:"


def emit_harness_result(data: dict) -> None:
    """Print the structured-result sentinel a streaming harness wrapper exposes.

    One line, machine-only. ``flow._run_script`` captures it (and keeps it out of
    the human stream) so the streaming command can mirror a fresh structured result
    to ``last_output.json`` instead of leaving a stale one behind (friction-log #18).
    """
    print(f"{HARNESS_RESULT_PREFIX} {json.dumps(data, ensure_ascii=False)}", flush=True)


def _iter_nested_match(root: Path, project_id: str, _depth: int = 0):
    """Yield project dirs whose leaf name equals project_id, sorted alphabetically.

    Uses an explicit walk so it never follows symlinks, never expands glob
    metacharacters, and never descends into a project directory — consistent
    with _iter_project_dirs in web_ui/app.py.
    """
    if _depth > 20:
        return
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.is_symlink() or not entry.is_dir():
            continue
        is_proj = (entry / "chunks").exists() or (entry / "source.txt").exists()
        if entry.name == project_id and is_proj:
            yield entry
        elif not is_proj:
            yield from _iter_nested_match(entry, project_id, _depth + 1)


def resolve_project_dir(project: str, *, must_exist: bool = True) -> Path:
    """Accept either a project id (a folder under ``projects/``) or a path.

    A bare id (a single path component that is not an existing file) resolves to
    ``projects/<id>``; anything absolute or with a separator is treated as a direct
    path. ``must_exist=False`` (used by ``setup``) allows a not-yet-created target.
    """
    p = Path(project)
    if p.is_absolute() or len(p.parts) > 1 or p.exists():
        if must_exist and not p.exists():
            raise FileNotFoundError(f"project path not found: {project!r}")
        return p
    candidate = REPO_ROOT / "projects" / project
    if candidate.exists():
        return candidate
    # bare id not at the flat root: search grouping subfolders for a project dir
    # of that name (a project dir has chunks/ or source.txt).
    projects_root = REPO_ROOT / "projects"
    if projects_root.exists():
        _found = None
        for entry in _iter_nested_match(projects_root, project):
            if _found is None:
                _found = entry
            else:
                _log.warning(
                    "Duplicate project id %r found at %s and %s; using %s",
                    project, _found, entry, _found,
                )
                break
        if _found is not None:
            return _found
    if not must_exist:
        return candidate
    raise FileNotFoundError(
        f"project not found: {project!r} (looked for a path and projects/{project})"
    )


def slugify(text: str) -> str:
    """Turn a book title into a filesystem-safe project slug.

    Mirrors the web UI's create-project slug (``web_ui/app.py``) so both entry
    points name projects identically: lowercase, runs of non-alphanumerics
    collapse to a single ``-``, trimmed; empty/symbol-only input -> ``"project"``.
    """
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "project"


def available_project_dir(slug: str) -> Path:
    """Return a not-yet-existing ``projects/<slug>`` dir, suffixing on collision.

    ``projects/<slug>`` if free, else the first free ``projects/<slug>-N`` with
    ``N`` starting at 2 (so a second *Understood Betsy* becomes
    ``understood-betsy-2``). Matches the web UI's collision loop. Used by
    ``setup`` when the slug is auto-derived from the title; an explicit
    ``--project`` is honored verbatim instead and may reuse an existing dir.
    """
    projects_root = REPO_ROOT / "projects"
    candidate = slug
    suffix = 2
    while (projects_root / candidate).exists():
        candidate = f"{slug}-{suffix}"
        suffix += 1
    return projects_root / candidate


def harness_dir(project_dir: Path) -> Path:
    """The per-project working directory, ``projects/<slug>/.harness/``."""
    return project_dir / ".harness"


def ensure_harness_dir(project_dir: Path, *, clean: bool = False) -> Path:
    """Create (optionally wiping first) the per-project ``.harness/`` directory."""
    d = harness_dir(project_dir)
    if clean and d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_path(project_dir: Path) -> Path:
    return harness_dir(project_dir) / "config.json"


def load_config(project_dir: Path) -> dict:
    """Load ``.harness/config.json`` merged over DEFAULTS (missing file is fine)."""
    cfg = dict(DEFAULTS)
    path = config_path(project_dir)
    if path.exists():
        cfg.update(json.loads(path.read_text(encoding="utf-8")))
    return cfg


# A flag token safe to append to a child argv: one or two leading dashes, then
# word characters, dots, dashes, and an optional ``=value``.
_SAFE_FLAG_RE = re.compile(r"^--?[A-Za-z0-9][A-Za-z0-9._=-]*$")

# Characters ``cmd.exe`` re-parses: command chaining, redirection, its escape
# character, and ``%VAR%`` expansion. On Windows ``shutil.which("claude")``
# resolves to a ``claude.CMD`` shim, and subprocess runs a .CMD through
# ``cmd.exe /c`` — which re-parses these *even though we pass shell=False and a
# list argv*. A free-text config value is therefore a command-execution surface,
# not merely an argv-shape question.
_SHELL_METACHARS = frozenset('&|<>^%"`\n\r\t')


def unsafe_extra_flag_tokens(tokens: Sequence[str]) -> list[str]:
    """The tokens in ``tokens`` that must never reach a child argv.

    A token is rejected when it carries a character ``cmd.exe`` would re-parse
    (see :data:`_SHELL_METACHARS`), or when it looks like a flag but is not
    shaped like one. Plain values (a model id, a path) pass as long as they are
    metacharacter-free; the empty string passes, since ``--setting-sources ""``
    is a documented use.
    """
    bad: list[str] = []
    for token in tokens:
        text = str(token)
        if set(text) & _SHELL_METACHARS:
            bad.append(text)
        elif text.startswith("-") and not _SAFE_FLAG_RE.match(text):
            bad.append(text)
    return bad


def split_extra_flags(raw: str) -> list[str]:
    """Tokenize a free-text ``headless_extra_flags`` value into argv tokens.

    Quote-aware, so the documented ``--setting-sources ""`` reaches the child as
    an empty string rather than two literal quote characters. Splits in non-POSIX
    mode and strips matched surrounding quotes afterwards, because POSIX mode eats
    the backslashes in a Windows path (``C:\\x\\y`` -> ``C:xy``).

    Raises ``ValueError`` on unbalanced quotes; callers that must not fail decide
    what to do about it.
    """
    tokens = shlex.split(raw, posix=False)
    return [
        token[1:-1]
        if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'"
        else token
        for token in tokens
    ]


def headless_extra_flags(cfg: Mapping[str, object]) -> list[str]:
    """``headless_extra_flags`` as a clean argv list (never raises on bad config).

    Accepts a list (``["--effort", "low"]``) or a quote-aware string
    (``"--effort low"``), because ``config-set --value`` can only deliver a
    string and a key that silently did nothing when set the documented way would
    be worse than no key at all.

    A mistyped value must not take a wave down, so an unparseable value comes back
    empty and the wave runs on today's argv. Tokens that would be re-parsed by the
    Windows ``.CMD`` shim are dropped rather than raising, for the same reason —
    ``flow.config_set`` rejects them loudly at set time, and this is the
    belt-and-braces for a hand-edited ``config.json`` that bypassed that gate.
    """
    raw = cfg.get("headless_extra_flags")
    if isinstance(raw, str):
        try:
            tokens = split_extra_flags(raw)
        except ValueError:
            _log.warning(
                "headless_extra_flags is not parseable (unbalanced quotes); "
                "ignoring it for this wave"
            )
            return []
    elif isinstance(raw, list):
        tokens = [str(flag) for flag in raw if isinstance(flag, (str, int, float))]
    else:
        return []

    unsafe = unsafe_extra_flag_tokens(tokens)
    if unsafe:
        # Drop the whole value, not just the offending token: keeping the
        # remainder would run the wave on a mangled argv nobody asked for
        # ("--safe-mode & echo hi" minus "&" is not a flag list). The wave runs
        # on today's argv instead, which is the same fail-closed behaviour a
        # mistyped value gets.
        _log.warning(
            "ignoring headless_extra_flags: unsafe token(s) %s",
            ", ".join(sorted(repr(t) for t in unsafe)),
        )
        return []
    return tokens


def _split_effort_from_flags(flags: list[str]) -> tuple[str | None, list[str]]:
    """Pull an ``--effort`` / ``--effort=`` pair out of ``flags``.

    Returns ``(effort_value_or_None, residual_flags)``. When several effort pairs
    appear, the last one wins (matching typical CLI last-wins semantics) and all
    of them are stripped so the resolver never emits a duplicate.

    Effort does not belong in ``headless_extra_flags`` — ``flow.config_set``
    rejects it there and points at ``headless_effort_<type>``. This strip is the
    belt-and-braces for a hand-edited ``config.json`` that bypassed that gate: the
    value is discarded, not honored, so argv can never carry two ``--effort``
    pairs and the per-type key stays the single source of truth.
    """
    residual: list[str] = []
    effort: str | None = None
    i = 0
    while i < len(flags):
        flag = flags[i]
        if flag == "--effort":
            # A trailing bare --effort has no value to capture, but it must still
            # be dropped: passing it through would leave argv ending in a flag the
            # CLI rejects ("expected one argument"), taking down every job.
            if i + 1 < len(flags):
                effort = str(flags[i + 1])
                i += 2
            else:
                i += 1
            continue
        if isinstance(flag, str) and flag.startswith("--effort="):
            effort = flag.split("=", 1)[1]
            i += 1
            continue
        residual.append(flag)
        i += 1
    return effort, residual


def compose_headless_argv(
    cfg: Mapping[str, object], effort: str | None
) -> list[str]:
    """Claude argv for an **already-resolved** effort level.

    Strips any ``--effort`` out of ``headless_extra_flags`` first (see
    :func:`_split_effort_from_flags`) and prepends the resolved pair, so argv can
    never carry a duplicate. ``effort=None`` emits no ``--effort`` at all.

    Split out of :func:`resolve_headless_argv` so a caller that already knows the
    level — because :func:`src.harness.profile.resolve_profile` resolved it across
    both CLI channels — can compose the argv without re-running a ladder that
    would answer a slightly different question.
    """
    _stray_effort, residual_flags = _split_effort_from_flags(
        headless_extra_flags(cfg)
    )
    if effort:
        return ["--effort", effort, *residual_flags]
    return list(residual_flags)


def resolve_headless_argv(
    cfg: Mapping[str, object],
    *,
    command: str,
    effort_override: str | None = None,
    cli: str | None = None,
) -> tuple[list[str], str | None, str]:
    """Compose headless argv with a resolved ``--effort`` for one wave type.

    Returns ``(argv, resolved_effort_or_None, provenance)``.

    Precedence, highest first:
      1. ``effort_override`` (per-run ``--effort``) → ``"cli"``
      2. ``cfg[effort_config_key(command)]`` when not ``"auto"`` → ``"config"``
      3. ``command_effort_default(command, cli=cli)`` → ``"default:<command>"``

    ``cli`` selects the tier-3 row. **The composed argv is Claude argv**: a Cursor
    wave drops ``--effort`` entirely (``headless._build_cmd``) and takes its
    effort from the model bracket instead, so on that path use the *level* this
    returns and ignore the argv — or, better, go through
    :func:`src.harness.profile.resolve_profile`, which picks the channel for you.

    Each wave type reads its own key, so pinning judges never moves translate.
    ``"auto"`` defers to the table; ``"default"`` and ``None`` both mean *emit no
    flag*, with provenance keeping them distinguishable (``"config"`` / ``"cli"``
    for an explicit none vs ``"default:<command>"`` for a type whose table entry
    is ``None``). Never raises on a mistyped value — falls through to the next
    tier, because a typo in config.json must not take a wave down.

    Composition strips any ``--effort`` out of ``headless_extra_flags`` first (see
    :func:`_split_effort_from_flags`) and then prepends the resolved pair, so argv
    can never carry a duplicate.
    """
    def _compose(level: str | None) -> list[str]:
        return compose_headless_argv(cfg, level)

    # 1. Per-run CLI override.
    if effort_override is not None:
        if effort_override == "default":
            return _compose(None), None, "cli"
        if effort_override in EFFORT_LEVELS:
            return _compose(effort_override), effort_override, "cli"
        # Mistyped override (argparse should have caught it): fall through.

    # 2. This wave type's config key (when not "auto").
    raw_cfg = cfg.get(effort_config_key(command), "auto")
    if isinstance(raw_cfg, str) and raw_cfg != "auto":
        if raw_cfg == "default":
            return _compose(None), None, "config"
        if raw_cfg in EFFORT_LEVELS:
            return _compose(raw_cfg), raw_cfg, "config"
        # Mistyped config value: fall through to the per-command table.

    # 3. Per-command auto default (per CLI family).
    level = command_effort_default(command, cli=cli)
    return _compose(level), level, f"default:{command}"


def resolve_prompt_cache(
    cfg: Mapping[str, object],
    *,
    cache_override: str | None = None,
) -> str:
    """Resolve the requested prompt-cache mode (``auto`` / ``5m`` / ``1h`` / ``off``).

    Precedence: per-run ``cache_override`` > ``cfg["headless_prompt_cache"]`` >
    ``"auto"``. Does **not** expand ``auto`` into a concrete TTL — that needs
    job shapes and lives in :func:`~src.harness.headless.resolve_cache_mode`
    / :func:`~src.harness.headless.run_headless_wave`. Never raises on a
    mistyped config value — falls through to ``auto``.
    """
    if cache_override is not None:
        value = str(cache_override).strip().lower()
        if value in CACHE_VALUES:
            return value
    raw = cfg.get("headless_prompt_cache", "auto")
    if isinstance(raw, str):
        value = raw.strip().lower()
        if value in CACHE_VALUES:
            return value
    return "auto"


def save_config(project_dir: Path, cfg: dict) -> None:
    """Write ``.harness/config.json`` (merged over DEFAULTS so it is complete).

    Keys not in DEFAULTS (e.g. ``run_id``, the persisted spawn knobs) are kept
    as-is, so callers can stash extra state in the config without registering it
    as a CLI-overridable key.
    """
    merged = dict(DEFAULTS)
    merged.update({k: v for k, v in cfg.items() if v is not None})
    ensure_harness_dir(project_dir)
    config_path(project_dir).write_text(
        json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8"
    )


# ── run id (one per pass through the harness) ───────────────────────────────
#
# A "run" is one trip through the pipeline, bounded by ``setup`` (which wipes
# ``.harness/`` for a clean run). The id lives in config.json so every later
# command can stamp the same run, and it is deliberately NOT in DEFAULTS /
# CONFIG_KEYS so it is never exposed as a CLI override.

_RUN_ID_TS_FMT = "%Y%m%d_%H%M%S_%f"  # microseconds break ties on fast re-runs


def new_run_id(project_dir: Path) -> str:
    """Mint a fresh run id, ``<slug>_<YYYYMMDD_HHMMSS_ffffff>`` (not persisted here)."""
    return f"{project_dir.name}_{datetime.now():{_RUN_ID_TS_FMT}}"


def ensure_run_id(project_dir: Path) -> str:
    """Return the project's current run id, minting + persisting one if absent.

    ``setup`` mints a fresh id each run; this is the read path every other
    command uses (and the back-fill for projects created before run-logging
    existed). Best-effort persistence: if the config can't be written, the
    minted id is still returned so the event can be stamped.
    """
    cfg = load_config(project_dir)
    rid = cfg.get("run_id")
    if rid:
        return rid
    rid = new_run_id(project_dir)
    cfg["run_id"] = rid
    try:
        save_config(project_dir, cfg)
    except OSError:
        _log.warning("Could not persist run_id for %s", project_dir, exc_info=True)
    return rid
