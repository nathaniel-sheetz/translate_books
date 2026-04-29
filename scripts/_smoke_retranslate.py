"""Smoke test for src.retranslator.retranslate_sentence.

Lets us iterate on the prompt + model behavior without booting the web UI.

Usage:
    python scripts/_smoke_retranslate.py \
        --project fabre2 \
        --source "The cake was burnt and the king was scolded." \
        --model claude-haiku-4-5-20251001

Defaults to the Anthropic default model from llm_config.json. Reads style.json
from the named project if present (passes empty style guide otherwise).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.models import Glossary  # noqa: E402
from src.retranslator import retranslate_sentence  # noqa: E402


def _load_glossary(project_dir: Path) -> Glossary | None:
    path = project_dir / "glossary.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return Glossary.model_validate(data)
    except Exception as exc:
        print(f"[warn] could not load glossary at {path}: {exc}")
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip())
    parser.add_argument("--project", required=True, help="Project id under projects/")
    parser.add_argument("--source", required=True, help="Source text to retranslate")
    parser.add_argument("--model", default=None, help="Model id (default: from llm_config.json)")
    parser.add_argument("--provider", default=None, help="Provider id (auto-resolved if omitted)")
    parser.add_argument("--source-language", default="English")
    parser.add_argument("--target-language", default="Spanish")
    parser.add_argument(
        "--context",
        default=None,
        help="Surrounding text passed as <context> to the LLM (read-only).",
    )
    args = parser.parse_args()

    project_dir = REPO_ROOT / "projects" / args.project
    if not project_dir.exists():
        print(f"[err] project not found: {project_dir}")
        return 2

    style_path = project_dir / "style.json"
    glossary = _load_glossary(project_dir)

    print(f"project        : {args.project}")
    print(f"model          : {args.model or '(default)'}")
    print(f"style.json     : {'found' if style_path.exists() else 'not found'}")
    print(f"glossary       : {'loaded ' + str(len(glossary.terms)) + ' terms' if glossary else 'none'}")
    print(f"source         : {args.source}")
    if args.context:
        ctx_preview = args.context if len(args.context) <= 120 else args.context[:120] + "…"
        print(f"context        : {ctx_preview}")
    print()

    result = retranslate_sentence(
        args.source,
        style_json_path=style_path if style_path.exists() else None,
        glossary=glossary,
        model=args.model,
        provider=args.provider,
        source_language=args.source_language,
        target_language=args.target_language,
        context_text=args.context,
    )

    print(f"new translation: {result.new_translation}")
    print()
    print(f"model used     : {result.model} ({result.provider})")
    print(f"tokens         : {result.prompt_tokens} in / {result.completion_tokens} out")
    print(f"cost           : ${result.cost_usd:.6f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
