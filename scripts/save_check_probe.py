"""Show which model the save check would ask right now, and ask it.

The model layer of the save-time check (``src/save_check_model.py``) uses
whichever model the inference server has loaded, if that model has a profile.
This prints what ``app_config.json`` configures, what the server reports as
loaded, the profile that matches, and the verdict on two canned edits: one that
leaves an adjective disagreeing with its noun, and one clean rewording.

Usage:
    python scripts/save_check_probe.py
    python scripts/save_check_probe.py --explain
    python scripts/save_check_probe.py --url http://192.168.1.22:8080
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src import save_check, save_check_model  # noqa: E402
from src.app_config import get_save_check_config  # noqa: E402

EDITS = (
    ("slip", "The old houses were empty.", "Los caserones viejos estaban vacíos.",
     "Las casas viejos estaban vacías."),
    ("clean", "He walked slowly toward the river.", "Caminó despacio hacia el río.",
     "Se acercó al río sin prisa."),
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--url", help="ask this server instead of the configured one")
    parser.add_argument("--explain", action="store_true", help="also ask for the reason on the slip")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    section = get_save_check_config()
    enabled, disabled = save_check.read_config(section)
    model_section = dict(section.get("model") or {}) if isinstance(section.get("model"), dict) else {}
    if args.url:
        model_section.update({"backend": model_section.get("backend") or "llama-server", "url": args.url})
    config = save_check_model.read_model_config(model_section)
    print(f"save_check.enabled: {enabled}" + ("   (model is in disabled_rules)" if "model" in disabled else ""))
    print(f"backend: {config.backend or '(none: the model layer is off)'}   url: {config.url or '-'}")
    backend = save_check_model.backend_for(config)
    if backend is None:
        print("No backend. Set save_check.model.backend and url in app_config.json, "
              f"and {save_check_model.KEY_ENV} in .env.")
        return 1

    try:
        loaded = backend.loaded()
    except Exception as e:
        print(f"The server did not answer ({e}). Saves get the rules only.")
        return 1
    print(f"loaded: {', '.join(loaded) or '(nothing)'}")
    routed = backend.route()
    if routed is None:
        known = ", ".join(p.name for p in config.profiles)
        print(f"No profile for what is loaded (profiles: {known}). Saves get the rules only.")
        return 1
    model_id, profile = routed
    print(f"profile: {profile.name}   prompt {profile.prompt}   warns above {profile.threshold}")

    for label, en, before, after in EDITS:
        verdict = backend.judge(en, before, after)
        if verdict is None:
            print(f"{label:6} no answer")
            continue
        print(f"{label:6} score {verdict.score:+8.3f}   {'WARN' if verdict.flagged else 'ok  '}   "
              f"{verdict.seconds:.2f}s   {after}")
    if args.explain:
        _, en, before, after = EDITS[0]
        print(f"reason: {backend.explain(en, before, after)!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
