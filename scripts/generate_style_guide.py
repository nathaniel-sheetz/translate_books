#!/usr/bin/env python3
"""
Generate a translation style guide via interactive questionnaire.

Modes:
  --fixed-only     Use only the hardcoded questions (no LLM needed)
  --non-interactive Use default answers for all questions
  (default)        Interactive CLI + LLM-generated questions

Examples:
  # Fixed questions only, interactive
  python scripts/generate_style_guide.py --project-dir projects/fabre2 \\
      --target-lang Spanish --locale mx --fixed-only

  # Full wizard with LLM questions
  python scripts/generate_style_guide.py --project-dir projects/fabre2 \\
      --target-lang Spanish --locale mx

  # Non-interactive with defaults
  python scripts/generate_style_guide.py --project-dir projects/fabre2 \\
      --target-lang Spanish --locale mx --non-interactive --fixed-only
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.style_guide_wizard import (
    load_fixed_questions,
    build_question_prompt,
    parse_llm_questions,
    build_style_guide_prompt,
    parse_style_guide_response,
    answers_to_style_guide_fallback,
    load_source_sample,
    save_style_guide_json,
)


def ask_question_interactive(question: dict) -> int | str:
    """Present a question interactively and return the answer."""
    print(f"\n  {question['question']}")
    if question.get("context"):
        print(f"  ({question['context']})")
    for i, opt in enumerate(question["options"]):
        default_marker = " (default)" if i == question.get("default", -1) else ""
        print(f"    {i + 1}) {opt['label']}{default_marker}")
    if question.get("allow_custom"):
        print(f"    {len(question['options']) + 1}) Custom")

    default_idx = question.get("default", 0)
    while True:
        choice = input(f"  Choice [{default_idx + 1}]: ").strip()
        if not choice:
            return default_idx

        try:
            n = int(choice) - 1
            if 0 <= n < len(question["options"]):
                return n
            if question.get("allow_custom") and n == len(question["options"]):
                prompt_text = question.get("custom_prompt", "Enter custom value:")
                custom = input(f"  {prompt_text} ").strip()
                return custom if custom else default_idx
        except ValueError:
            pass
        print("  Invalid choice, try again.")


def main():
    parser = argparse.ArgumentParser(
        description="Generate a translation style guide via questionnaire",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--project-dir", required=True, help="Project directory")
    parser.add_argument("--target-lang", default="Spanish", help="Target language (default: Spanish)")
    parser.add_argument("--locale", default="mx", help="Target locale code (default: mx)")
    parser.add_argument("--output", help="Output path (default: <project-dir>/style.json)")
    parser.add_argument("--fixed-only", action="store_true", help="Use only fixed questions (no LLM)")
    parser.add_argument("--non-interactive", action="store_true", help="Use default answers")
    parser.add_argument("--provider", default="anthropic", choices=["anthropic", "openai"])
    parser.add_argument("--model", default="claude-sonnet-4-20250514")
    parser.add_argument("--questions-config", help="Path to custom questions JSON")

    args = parser.parse_args()
    project_dir = Path(args.project_dir)
    output_path = Path(args.output) if args.output else project_dir / "style.json"

    # Load source text sample
    print("Loading source text...")
    source_text = load_source_sample(project_dir)
    if not source_text:
        print("Error: No source text found (source.txt or chunks/)")
        return 1
    word_count = len(source_text.split())
    print(f"  Loaded {word_count:,} words")

    # Load and ask fixed questions
    questions_path = Path(args.questions_config) if args.questions_config else None
    fixed_questions = load_fixed_questions(questions_path)
    all_questions = list(fixed_questions)
    answers: dict[str, int | str] = {}

    print("\n--- Style Guide Questionnaire ---")
    for q in fixed_questions:
        if args.non_interactive:
            answers[q["id"]] = q.get("default", 0)
            opt = q["options"][answers[q["id"]]]
            print(f"  {q['question']} -> {opt['label']} (default)")
        else:
            answers[q["id"]] = ask_question_interactive(q)

    # Optionally get LLM-generated questions
    if not args.fixed_only:
        print("\nGenerating additional questions from text analysis...")
        try:
            from src.api_translator import call_llm
            prompt = build_question_prompt(source_text, args.target_lang, args.locale, fixed_questions, answers)
            response = call_llm(prompt, provider=args.provider, model=args.model, call_type="style_questions")
            llm_questions = parse_llm_questions(response)
            all_questions.extend(llm_questions)

            print(f"  Generated {len(llm_questions)} additional question(s)\n")
            for q in llm_questions:
                if args.non_interactive:
                    answers[q["id"]] = q.get("default", 0)
                    opt = q["options"][answers[q["id"]]]
                    print(f"  {q['question']} -> {opt['label']} (default)")
                else:
                    answers[q["id"]] = ask_question_interactive(q)

        except Exception as e:
            print(f"  Warning: Could not generate LLM questions: {e}")
            print("  Continuing with fixed questions only.")

    # Generate style guide
    if args.fixed_only:
        print("\nGenerating style guide from answers...")
        content = answers_to_style_guide_fallback(all_questions, answers)
    else:
        print("\nGenerating style guide via LLM...")
        try:
            from src.api_translator import call_llm
            prompt = build_style_guide_prompt(all_questions, answers, source_text, args.target_lang, args.locale)
            response = call_llm(prompt, provider=args.provider, model=args.model, max_tokens=2048, call_type="style_guide_generate")
            content = parse_style_guide_response(response)
        except Exception as e:
            print(f"  Warning: LLM generation failed: {e}")
            print("  Falling back to template-based generation.")
            content = answers_to_style_guide_fallback(all_questions, answers)

    # Save
    print(f"\n--- Generated Style Guide ---\n")
    print(content)
    print(f"\n--- End ---\n")

    save_style_guide_json(content, output_path)
    print(f"Saved to: {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
