"""
Save-time check, model layer: ask a language model whether a saved sentence
holds a slip the rules cannot see -- agreement left half-changed, tú and usted
mixed, a real word in place of the intended one.

The question is one letter, A (no slip) or B (slip). On ``llama-server`` the
answer is read from the first token's log-probabilities as log-odds and
compared with a threshold, so nothing is generated
(``docs/design/local-inference-save-check.md``).

The check never starts or swaps a model. It asks the server what is loaded and
uses that model if it has a :class:`Profile`; log-odds are on a different scale
for each model, so one without a calibrated profile is left alone and the rules
stand by themselves. The same holds when the server does not answer.

:class:`Backend` is the seam for other ways of asking. A headless or API
backend has no log-odds: it returns a :class:`Verdict` with ``score`` of
``None`` and ``flagged`` read from the letter. Nothing above the backend looks
at the score except to log it.
"""

from __future__ import annotations

import collections
import difflib
import json
import logging
import math
import os
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional, Protocol

from src import save_check
from src.models import IgnoredTerms

logger = logging.getLogger(__name__)

RULE = "model"
READOUT_LOG_NAME = "save_check_readouts.jsonl"
KEY_ENV = "LOCAL_LLM_KEY"

# An unreachable server must cost nothing a translator notices; a slot shared
# with another job answers late, and pays for the whole prompt again.
CONNECT_TIMEOUT_S = 2
READ_TIMEOUT_S = 15
# Short, because a server with one model answers to any name: a model swapped
# in behind a stale listing would be scored on the old model's threshold.
LOADED_TTL_S = 5
RETRY_AFTER_S = 60
RETRY_AFTER_MAX_S = 600


# --- prompts ----------------------------------------------------------------

SYSTEM_V1 = """\
You check one edit that a translator has just saved in the Spanish translation of an English book. The translator works quickly and sometimes leaves a slip behind. Decide whether the sentence as saved contains a slip the translator would want to fix at once.

A slip is a mechanical mistake, not a choice:
- a misspelt or non-existent word, or a missing or misplaced accent (tambien, nesecario)
- a real word that a mistyped key or autocorrect put in place of the intended one, so the sentence no longer makes sense (cabello where caballo was meant)
- agreement left half-changed: an article, adjective, pronoun or verb that no longer matches a word the edit changed (la casa viejo; cuando llegan, saludas)
- tú forms and usted forms mixed for the same listener inside the sentence, because only some of them were changed
- a word left over, repeated or missing, so the sentence no longer parses
- broken punctuation: a doubled or misplaced mark, or an opening and closing mark in the same sentence that do not match

These are not slips:
- any change of wording, word order, register or style that leaves a grammatical sentence, even one you would have translated differently
- a consistent switch between tú and usted, or between singular and plural
- capitalisation, italics markers (_like this_) and spelling conventions for names and titles
- dialect and deliberately non-standard speech in dialogue
- names and foreign words
- a quotation mark or raya whose partner may sit in a neighbouring sentence

Answer with one letter: A if there is no slip, B if there is a slip."""

#: v2 names the agreement check first and adds worked examples. It made the
#: strongest model worse and a weaker one better, so the prompt is per profile.
SYSTEM_V2 = """\
You check one edit that a translator has just saved in the Spanish translation of an English book. The translator works quickly and sometimes leaves a slip behind. Decide whether the sentence as saved contains a slip the translator would want to fix at once.

Look first at the words listed under Changed, then at every word that has to agree with them: the article, adjectives, pronouns and verb forms that refer to the same thing or speak to the same listener. If the edit changed gender, number, or tú/usted in one place and a matching word elsewhere in the sentence still has the old form, that is a slip.

A slip is a mechanical mistake, not a choice:
- a misspelt or non-existent word, or a missing or misplaced accent (tambien, nesecario)
- a real word that a mistyped key or autocorrect put in place of the intended one, so the sentence no longer makes sense
- agreement left half-changed, as described above
- tú forms and usted forms mixed for the same listener inside the sentence
- a word left over, repeated or missing, so the sentence no longer parses
- broken punctuation: a doubled or misplaced mark, or an opening and closing mark in the same sentence that do not match

These are not slips:
- any change of wording, word order, register or style that leaves a grammatical sentence, even one you would have translated differently
- a change of a noun, or of a character's or animal's sex, when the words around it were changed to match
- a consistent switch between tú and usted, or between singular and plural
- rio, guio, vio, dio, fue, crie and similar forms written without an accent: that is the current spelling
- capitalisation, italics markers (_like this_) and spelling conventions for names and titles
- dialect, regional vocabulary, adapted loanwords and deliberately non-standard speech
- names and foreign words
- a quotation mark or raya whose partner may sit in a neighbouring sentence

Examples

Before: El perro viejo dormía junto a la puerta.
After: La perra viejo dormía junto a la puerta.
Changed: "El perro" -> "La perra"
Answer: B

Before: —¿Quieres que te ayude? —preguntó.
After: —¿Quiere que lo ayude? —preguntó.
Changed: "Quieres" -> "Quiere"; "te" -> "lo"
Answer: A

Before: —Siéntese y cuénteme qué vio.
After: —Siéntate y cuénteme qué vio.
Changed: "Siéntese" -> "Siéntate"
Answer: B

Before: Caminó despacio hacia el río.
After: Se acercó al río sin prisa.
Changed: "Caminó despacio hacia el" -> "Se acercó al"; added "sin prisa"
Answer: A

Before: El niño acarició el lomo del caballo.
After: El niño acarició el lomo del cabello.
Changed: "caballo" -> "cabello"
Answer: B

Before: —Claro —rió ella.
After: —Claro —rio ella.
Changed: "rió" -> "rio"
Answer: A

Answer with one letter: A if there is no slip, B if there is a slip."""

SYSTEMS = {"v1": SYSTEM_V1, "v2": SYSTEM_V2}

EXPLAIN_SYSTEM = """A translator has just saved an edit to one sentence of the Spanish translation of an English book, and a check flagged the saved sentence as containing a slip: a typo, a wrong word, agreement or tú/usted left half-changed, a leftover or missing word, or broken punctuation.

Say what the slip is in one short English sentence, quoting the Spanish words involved. If you can find no slip, answer exactly: none."""

_TOKEN = re.compile(r"\s+|\w+|[^\w\s]")


def diff_segments(before: str, after: str) -> list[list]:
    """The edit as ``[op, text]`` runs: 0 kept, -1 removed, 1 added."""
    a, b = _TOKEN.findall(before), _TOKEN.findall(after)
    out = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            out.append([0, "".join(a[i1:i2])])
            continue
        if i2 > i1:
            out.append([-1, "".join(a[i1:i2])])
        if j2 > j1:
            out.append([1, "".join(b[j1:j2])])
    return out


def changed_spans(before: str, after: str) -> str:
    """The edit in words: ``"old" -> "new"``, ``added "x"``, ``removed "y"``."""
    parts, pending = [], None
    for op, text in diff_segments(before, after) + [[0, ""]]:
        if op == -1:
            pending = text
            continue
        if op == 1:
            if pending is not None:
                parts.append(f'"{pending.strip()}" -> "{text.strip()}"')
                pending = None
            else:
                parts.append(f'added "{text.strip()}"')
            continue
        if pending is not None:
            parts.append(f'removed "{pending.strip()}"')
            pending = None
    return "; ".join(parts)


def punctuation_only(before: str, after: str) -> bool:
    """True when the edit touched no letter or digit.

    Such an edit is not shown to the model: it flagged them for the dialogue
    convention (a paragraph that opens with a lone ») and the rules already
    cover the punctuation slips.
    """
    return not any(ch.isalnum() for op, text in diff_segments(before, after) if op for ch in text)


def render_user(en: str, before: str, after: str, with_english: bool = True) -> str:
    lines = []
    if with_english and en:
        lines.append(f"English: {en}")
    lines.append(f"Before: {before}")
    lines.append(f"After: {after}")
    lines.append(f"Changed: {changed_spans(before, after)}")
    lines.append("Answer:")
    return "\n".join(lines)


def render_explain_user(en: str, before: str, after: str) -> str:
    return render_user(en, before, after).rsplit("\n", 1)[0] + "\nSlip:"


def hit_text(before: str, after: str, limit: int = 60) -> str:
    """What a model hit shows: the words the edit typed, or took out."""
    runs = diff_segments(before, after)
    words = [text.strip() for op, text in runs if op == 1 and text.strip()]
    if not words:
        words = [text.strip() for op, text in runs if op == -1 and text.strip()]
    text = " … ".join(words)
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def read_letters(reply: Mapping) -> tuple[float, float, float]:
    """``(p_slip, mass, logit)`` from the first answer token's top log-probabilities."""
    positions = reply["choices"][0]["logprobs"]["content"]
    # A model with a thinking phase closes it first even when thinking is off,
    # so the answer is the first token that is neither a think tag nor blank.
    first = next(
        (p for p in positions
         if (p.get("token") or "").strip() and "think" not in (p.get("token") or "")),
        None,
    )
    if first is None:
        raise ValueError("no answer token in %r" % [p.get("token") for p in positions])
    weight = {"A": 0.0, "B": 0.0}
    for candidate in first.get("top_logprobs") or []:
        letter = (candidate.get("token") or "").strip()
        if letter in weight:
            weight[letter] += math.exp(candidate["logprob"])
    mass = weight["A"] + weight["B"]
    if mass <= 0:
        raise ValueError("neither letter is among the top tokens: %r" % first.get("token"))
    # The log-odds keep the resolution a rounded probability loses when a model
    # saturates; a letter that fell out of the top tokens is capped at 30 nats.
    floor = 1e-13
    logit = math.log(max(weight["B"], floor)) - math.log(max(weight["A"], floor))
    return weight["B"] / mass, mass, logit


# --- profiles ---------------------------------------------------------------

@dataclass(frozen=True)
class Profile:
    """How one model is asked and where it warns.

    ``aliases`` are the names the model is served under (``--alias``), matched
    whole and without regard to case. ``threshold`` is the lowest log-odds that
    kept rules and model together within 2.5% of clean development saves.
    """

    name: str
    aliases: tuple[str, ...]
    prompt: str
    threshold: float

    def serves(self, model_id: str) -> bool:
        return model_id.lower() in {self.name.lower(), *(a.lower() for a in self.aliases)}


#: In order of preference, for a server with more than one loaded.
PROFILES: tuple[Profile, ...] = (
    Profile("gemma-4-31b", ("gemma31",), "v1", 7.5),
    Profile("qwen3.8-27b", ("qwen27",), "v2", 0.9999),
    Profile("gemma-4-26b", ("gemma26",), "v1", 0.5006),
    Profile("qwen3.6-35b", ("qwen35moe",), "v1", 6.1043),
)


@dataclass(frozen=True)
class ModelConfig:
    backend: str = ""
    url: str = ""
    profiles: tuple[Profile, ...] = PROFILES


def read_model_config(config: object) -> ModelConfig:
    """The ``save_check.model`` section of app_config, read as leniently as the rest.

    ``backend`` empty or absent leaves the model layer off. ``profiles`` lists
    ``{name, threshold?, prompt?, aliases?}``: a name already in
    :data:`PROFILES` has the fields given replaced, a new one is added and
    needs a prompt and a threshold.
    """
    if not isinstance(config, Mapping):
        if config is not None:
            save_check._report_once("save_check.model should be an object")
        return ModelConfig()
    backend = config.get("backend") or ""
    if not isinstance(backend, str) or (backend and backend not in BACKENDS):
        save_check._report_once(f"save_check.model.backend: {backend!r} is not one of {sorted(BACKENDS)}")
        backend = ""
    url = config.get("url") or ""
    if not isinstance(url, str) or (url and not url.strip().startswith(("http://", "https://"))):
        save_check._report_once(f"save_check.model.url {url!r} is not an http(s) URL")
        url = ""
    profiles = {p.name.lower(): p for p in PROFILES}
    listed = config.get("profiles") or ()
    if not isinstance(listed, (list, tuple)):
        save_check._report_once("save_check.model.profiles should be a list")
        listed = ()
    for entry in listed:
        profile = _read_profile(entry, profiles)
        if profile is None:
            save_check._report_once(f"save_check.model.profiles: {entry!r} is not a usable profile")
        else:
            profiles[profile.name.lower()] = profile
    return ModelConfig(backend, url.strip().rstrip("/"), tuple(profiles.values()))


def _read_profile(entry: object, known: Mapping[str, Profile]) -> Optional[Profile]:
    if not isinstance(entry, Mapping) or not isinstance(entry.get("name"), str) or not entry["name"].strip():
        return None
    name = entry["name"].strip()
    base = known.get(name.lower())
    prompt = entry.get("prompt", base.prompt if base else None)
    threshold = entry.get("threshold", base.threshold if base else None)
    aliases = entry.get("aliases", base.aliases if base else ())
    if not isinstance(prompt, str) or prompt not in SYSTEMS:
        return None
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
        return None
    if isinstance(aliases, str):
        aliases = (aliases,)
    if not isinstance(aliases, (list, tuple)) or not all(isinstance(a, str) for a in aliases):
        return None
    return Profile(base.name if base else name, tuple(aliases), prompt, float(threshold))


# --- backends ---------------------------------------------------------------

@dataclass(frozen=True)
class Verdict:
    """One answer. ``score`` and ``threshold`` are ``None`` where a backend has no log-odds."""

    flagged: bool
    model: str
    profile: str
    prompt: str
    backend: str
    seconds: float
    score: Optional[float] = None
    threshold: Optional[float] = None


class Backend(Protocol):
    name: str

    def judge(self, en: str, before: str, after: str) -> Optional[Verdict]:
        """The verdict on one saved edit, or ``None`` when it cannot be asked now."""

    def explain(self, en: str, before: str, after: str) -> Optional[str]:
        """The slip in one sentence, ``""`` when the model names none, ``None`` when it cannot be asked."""


class LlamaServerBackend:
    """Read the answer's log-odds from whichever profiled model a ``llama-server`` has loaded."""

    name = "llama-server"

    def __init__(self, url: str, key: str, profiles: Iterable[Profile] = PROFILES, session=None):
        self.url = url.rstrip("/")
        self.key = key
        self.profiles = tuple(profiles)
        self._session = session
        self._lock = threading.Lock()
        self._loaded: Optional[list[str]] = None
        self._loaded_at = 0.0
        self._skip_until = 0.0
        self._failures = 0
        self._unprofiled: set[str] = set()

    def _http(self):
        if self._session is None:
            import requests

            self._session = requests.Session()
        return self._session

    def _failed(self, error: Exception) -> None:
        with self._lock:
            wait = min(RETRY_AFTER_S * 2 ** self._failures, RETRY_AFTER_MAX_S)
            self._failures += 1
            self._skip_until = time.monotonic() + wait
            self._loaded = None
        logger.warning("Save-check model server %s unusable (%s); rules only for %ds", self.url, error, wait)

    def loaded(self) -> list[str]:
        """Ids of the models the server has in memory, asked at most every ``LOADED_TTL_S``."""
        with self._lock:
            if self._loaded is not None and time.monotonic() - self._loaded_at < LOADED_TTL_S:
                return self._loaded
        resp = self._http().get(
            self.url + "/v1/models",
            headers={"Authorization": "Bearer " + self.key},
            timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S),
        )
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        ids = []
        for entry in resp.json().get("data") or []:
            # A router-mode server lists every model it could load. Naming one
            # that is not in memory would load it and push out the one that is.
            status = entry.get("status")
            state = status.get("value") if isinstance(status, Mapping) else status
            if state is not None and state != "loaded":
                continue
            if isinstance(entry.get("id"), str):
                ids.append(entry["id"])
        with self._lock:
            self._loaded, self._loaded_at = ids, time.monotonic()
        return ids

    def route(self) -> Optional[tuple[str, Profile]]:
        """``(model id, profile)`` for the best loaded model that has one, else ``None``."""
        with self._lock:
            if time.monotonic() < self._skip_until:
                return None
        try:
            ids = self.loaded()
        except Exception as e:
            self._failed(e)
            return None
        for profile in self.profiles:
            for model_id in ids:
                if profile.serves(model_id):
                    return model_id, profile
        fresh = [m for m in ids if m not in self._unprofiled]
        if fresh:
            self._unprofiled.update(fresh)
            logger.warning("Save check: no profile for the loaded model %s; rules only", ", ".join(fresh))
        return None

    def _chat(self, model_id: str, system: str, user: str, **extra) -> dict:
        resp = self._http().post(
            self.url + "/v1/chat/completions",
            json={
                "model": model_id,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "temperature": 0,
                "cache_prompt": True,
                # Thinking is switched off in the chat template itself; a zero
                # reasoning budget alone leaves the model reasoning in the reply.
                "chat_template_kwargs": {"enable_thinking": False},
                **extra,
            },
            headers={"Authorization": "Bearer " + self.key},
            timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S),
        )
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        with self._lock:
            self._failures = 0
        return resp.json()

    def judge(self, en: str, before: str, after: str) -> Optional[Verdict]:
        routed = self.route()
        if routed is None:
            return None
        model_id, profile = routed
        started = time.monotonic()
        try:
            reply = self._chat(
                model_id, SYSTEMS[profile.prompt], render_user(en, before, after),
                max_tokens=4, logprobs=True, top_logprobs=20,
            )
        except Exception as e:
            self._failed(e)
            return None
        try:
            _, _, logit = read_letters(reply)
        except (KeyError, IndexError, TypeError, ValueError) as e:
            # The server answered; this one reply cannot be read.
            logger.warning("Save check: no readout from %s (%s)", model_id, e)
            return None
        return Verdict(
            flagged=logit > profile.threshold, model=model_id, profile=profile.name,
            prompt=profile.prompt, backend=self.name, seconds=round(time.monotonic() - started, 3),
            score=round(logit, 4), threshold=profile.threshold,
        )

    def explain(self, en: str, before: str, after: str) -> Optional[str]:
        routed = self.route()
        if routed is None:
            return None
        try:
            reply = self._chat(routed[0], EXPLAIN_SYSTEM, render_explain_user(en, before, after), max_tokens=60)
            content = (reply["choices"][0].get("message") or {}).get("content") or ""
        except Exception as e:
            self._failed(e)
            return None
        reason = " ".join(content.split())
        if not reason:
            # No answer is not the answer "none": a model that spent the
            # tokens reasoning is asked again, not kept as having named no slip.
            return None
        return "" if reason.lower().rstrip(".") == "none" else reason


BACKENDS = {LlamaServerBackend.name: LlamaServerBackend}

_backends: dict[ModelConfig, Optional[Backend]] = {}
_backends_lock = threading.Lock()


def backend_for(config: ModelConfig) -> Optional[Backend]:
    """The backend ``config`` names, or ``None`` when the model layer is off.

    One instance per configuration, so that what it has learnt about the server
    (what is loaded, when to try again) outlives a Save.
    """
    if not config.backend:
        return None
    with _backends_lock:
        if config not in _backends:
            _backends[config] = _build(config)
        return _backends[config]


def _build(config: ModelConfig) -> Optional[Backend]:
    if not config.url:
        save_check._report_once("save_check.model.backend is set but url is not; rules only")
        return None
    key = os.environ.get(KEY_ENV)
    if not key:
        from dotenv import load_dotenv

        load_dotenv()
        key = os.environ.get(KEY_ENV)
    if not key:
        save_check._report_once(f"save_check.model is set but {KEY_ENV} is not; rules only")
        return None
    return BACKENDS[config.backend](config.url, key, config.profiles)


# --- the readout log --------------------------------------------------------

def append_readout(project_dir: Path, verdict: Verdict, **fields) -> None:
    """Record one answer, flagged or not, in ``save_check_readouts.jsonl``.

    Kept apart from the warning log, which is read on every chapter load. A
    threshold is refitted from these: a dismissal says only that a warning was
    wrong, and this says how every other save scored.
    """
    record = {"timestamp": datetime.now().isoformat(), **fields, **asdict(verdict)}
    with open(Path(project_dir) / READOUT_LOG_NAME, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# --- one saved edit ---------------------------------------------------------

def _model_hit(warning: Optional[Mapping]) -> Optional[dict]:
    return next((h for h in (warning or {}).get("hits", []) if h.get("rule") == RULE), None)


def _left_alone(hit: Mapping, before: str, after: str) -> bool:
    """Whether an edit left the words a model hit names as they were.

    The hit names words an earlier edit typed, which are in ``before``, or
    words it took out, which are not. An edit that changes either is taken for
    the fix when the model cannot be asked.
    """
    parts = [p.strip() for p in (hit.get("text") or "").rstrip("…").split(" … ")]
    return all(save_check._has_word(p, before) == save_check._has_word(p, after) for p in parts if p)


def check_saved_write(
    backend: Backend,
    project_dir: Path,
    *,
    chapter_id: str,
    es_idx,
    path: str,
    en: str,
    before: str,
    after: str,
    rule_warning_id: Optional[str] = None,
    prior: Optional[Mapping] = None,
    load_rows: Callable[[], list[dict]],
    ignored: Optional[IgnoredTerms] = None,
    disabled_rules: Iterable[str] = (),
) -> Optional[dict]:
    """Ask the model about a write that has landed; ``{"id", "hits"}`` if it warns.

    ``rule_warning_id`` is the warning the rules logged for this Save, whose
    open hits the new warning takes over so the sentence carries one warning.
    ``prior`` is the open warning that stood on ``before`` when it held a model
    hit: if the model cannot be asked, that hit is carried forward so the
    warning does not close unread, unless the edit changed the words it names.
    A warning raised on a sentence that had one continues it. ``load_rows``
    reads the chapter's alignment as it stands once the verdict is in.
    """
    project_dir = Path(project_dir)
    prior_hit = _model_hit(prior)
    verdict = backend.judge(en, before, after)
    carried = verdict is None and prior_hit is not None and _left_alone(prior_hit, before, after)
    if verdict is not None:
        append_readout(project_dir, verdict, chapter_id=chapter_id, es_idx=es_idx, path=path,
                       en=en, es_before=before, es_after=after)
    if not carried and not (verdict is not None and verdict.flagged):
        return None
    # The verdict comes a second or more after the Save; the sentence may have
    # been saved again since, and that Save has a check of its own.
    if save_check.standing_row({"es_after": after, "es_idx": es_idx}, load_rows()) is None:
        return None

    warnings, outcomes, reasons = save_check.load_records(project_dir)
    off = set(disabled_rules)
    hits, lineage = [], {}
    rule_warning = next((w for w in warnings if w.get("id") == rule_warning_id), None)
    if rule_warning is not None:
        lineage["carried_from"] = rule_warning_id
        if rule_warning_id not in outcomes:
            hits = [
                h for h in rule_warning.get("hits", [])
                if h.get("rule") not in off | {RULE}
                and not (h.get("rule") == "spelling" and ignored is not None
                         and ignored.matches("dictionary", h.get("text")))
            ]
    if prior_hit is not None:
        lineage.setdefault("carried_from", prior.get("id"))
    if carried:
        hits.append(dict(prior_hit))
        asked = prior.get("model")
    else:
        text = hit_text(before, after)
        # An edit of marks or spaces alone typed no word to show.
        if prior_hit is not None and not any(ch.isalnum() for ch in text):
            text = prior_hit.get("text") or text
        hits.append({"rule": RULE, "text": text})
        asked = asdict(verdict)
    warning_id = save_check.append_warning(
        project_dir, chapter_id=chapter_id, es_idx=es_idx, path=path,
        en=en, es_before=before, es_after=after, hits=hits, model=asked, **lineage,
    )
    if carried and prior.get("id") in reasons:
        save_check.append_reason(project_dir, warning_id, reasons[prior["id"]])
    return {"id": warning_id, "hits": hits}


def reason_for(backend: Optional[Backend], project_dir: Path, warning_id: str) -> Optional[str]:
    """The model's one-line reason for a warning it raised, asked for once and kept.

    ``None`` when the warning has no model hit; ``""`` when the model named no
    slip, or cannot be asked now (in which case nothing is kept).
    """
    warnings, _, reasons = save_check.load_records(project_dir)
    warning = next((w for w in warnings if w.get("id") == warning_id), None)
    if _model_hit(warning) is None:
        return None
    if warning_id in reasons:
        return reasons[warning_id]
    reason = backend.explain(
        warning.get("en") or "", warning.get("es_before") or "", warning.get("es_after") or "",
    ) if backend is not None else None
    if reason is None:
        return ""
    save_check.append_reason(project_dir, warning_id, reason)
    return reason


# --- running behind the Save ------------------------------------------------

class Jobs:
    """One worker thread and a short queue: the server has one slot to answer on."""

    def __init__(self, max_waiting: int = 20, keep_s: float = 300) -> None:
        self.max_waiting = max_waiting
        self.keep_s = keep_s
        self._waiting: collections.deque = collections.deque()
        self._jobs: dict[str, dict] = {}
        self._cond = threading.Condition()
        self._thread: Optional[threading.Thread] = None

    def submit(self, work: Callable[[], Optional[dict]]) -> str:
        job_id = uuid.uuid4().hex[:12]
        now = time.monotonic()
        with self._cond:
            for old in [k for k, j in self._jobs.items() if j["done"].is_set() and now - j["at"] > self.keep_s]:
                del self._jobs[old]
            # Saves outrunning the model: the oldest one waiting goes unasked.
            while len(self._waiting) >= self.max_waiting:
                self._jobs[self._waiting.popleft()[0]]["done"].set()
            self._jobs[job_id] = {"done": threading.Event(), "result": None, "at": now}
            self._waiting.append((job_id, work))
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="save-check-model", daemon=True)
                self._thread.start()
            self._cond.notify()
        return job_id

    def wait(self, job_id: str, timeout: float) -> tuple[bool, Optional[dict]]:
        """``(done, result)``. A job this process does not know counts as done."""
        with self._cond:
            job = self._jobs.get(job_id)
        if job is None:
            return True, None
        return job["done"].wait(timeout), job["result"]

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._waiting:
                    self._cond.wait()
                job_id, work = self._waiting.popleft()
                job = self._jobs[job_id]
            try:
                job["result"] = work()
            except Exception:
                logger.exception("save check model job failed")
            job["at"] = time.monotonic()
            job["done"].set()


jobs = Jobs()
