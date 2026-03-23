"""
Manual validation script for report generation.

This script generates all three report formats (text, JSON, HTML) from
the test fixtures and saves them to a temp directory for manual inspection.

Run this to verify that:
1. Text reports are readable with proper formatting
2. JSON reports are valid and well-structured
3. HTML reports display correctly in a browser
"""

import tempfile
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.utils.file_io import load_chunk, load_glossary, save_text_report, save_json_report, save_html_report
from src.evaluators import run_all_evaluators, aggregate_results
from src.evaluators.reporting import generate_text_report, generate_json_report, generate_html_report
from src.config import create_default_config


def main():
    print("=" * 80)
    print("MANUAL REPORT VALIDATION")
    print("=" * 80)
    print()

    # Setup paths
    fixtures_dir = Path("tests/fixtures")
    chunk_good_path = fixtures_dir / "chunk_translated_good.json"
    chunk_errors_path = fixtures_dir / "chunk_translated_errors.json"
    glossary_path = fixtures_dir / "glossary_sample.json"

    # Load fixtures
    print("Loading fixtures...")
    chunk_good = load_chunk(chunk_good_path)
    chunk_errors = load_chunk(chunk_errors_path)
    glossary = load_glossary(glossary_path)
    print(f"  [OK] Loaded good chunk: {chunk_good.id}")
    print(f"  [OK] Loaded error chunk: {chunk_errors.id}")
    print(f"  [OK] Loaded glossary: {len(glossary.terms)} terms")
    print()

    # Create config
    config = create_default_config("manual_test")
    config.evaluation.enabled_evals = ["length", "paragraph"]  # Use simple evaluators

    # Create output directory
    output_dir = Path(tempfile.gettempdir()) / "book_translation_reports"
    output_dir.mkdir(exist_ok=True)
    print(f"Output directory: {output_dir}")
    print()

    # ========================================================================
    # Generate reports for GOOD chunk
    # ========================================================================
    print("-" * 80)
    print("GOOD TRANSLATION CHUNK (should mostly pass)")
    print("-" * 80)

    print("Running evaluators on good chunk...")
    results_good = run_all_evaluators(chunk_good, config.evaluation, glossary)
    aggregated_good = aggregate_results(results_good)

    print(f"  [OK] {aggregated_good['total_evaluators']} evaluators run")
    print(f"  [OK] {aggregated_good['passed_evaluators']} passed, {aggregated_good['failed_evaluators']} failed")
    print(f"  [OK] {aggregated_good['total_issues']} total issues")
    print()

    # Generate and save all formats
    print("Generating reports...")

    text_report_good = generate_text_report(results_good, aggregated_good, chunk_good)
    json_report_good = generate_json_report(results_good, aggregated_good, chunk_good)
    html_report_good = generate_html_report(results_good, aggregated_good, chunk_good)

    text_path_good = save_text_report(text_report_good, output_dir, f"{chunk_good.id}_good")
    json_path_good = save_json_report(json_report_good, output_dir, f"{chunk_good.id}_good")
    html_path_good = save_html_report(html_report_good, output_dir, f"{chunk_good.id}_good")

    print(f"  [OK] Text report: {text_path_good.name}")
    print(f"  [OK] JSON report: {json_path_good.name}")
    print(f"  [OK] HTML report: {html_path_good.name}")
    print()

    # ========================================================================
    # Generate reports for ERROR chunk
    # ========================================================================
    print("-" * 80)
    print("ERROR TRANSLATION CHUNK (should have multiple failures)")
    print("-" * 80)

    print("Running evaluators on error chunk...")
    results_errors = run_all_evaluators(chunk_errors, config.evaluation, glossary)
    aggregated_errors = aggregate_results(results_errors)

    print(f"  [OK] {aggregated_errors['total_evaluators']} evaluators run")
    print(f"  [OK] {aggregated_errors['passed_evaluators']} passed, {aggregated_errors['failed_evaluators']} failed")
    print(f"  [OK] {aggregated_errors['total_issues']} total issues")
    print()

    # Generate and save all formats
    print("Generating reports...")

    text_report_errors = generate_text_report(results_errors, aggregated_errors, chunk_errors)
    json_report_errors = generate_json_report(results_errors, aggregated_errors, chunk_errors)
    html_report_errors = generate_html_report(results_errors, aggregated_errors, chunk_errors)

    text_path_errors = save_text_report(text_report_errors, output_dir, f"{chunk_errors.id}_errors")
    json_path_errors = save_json_report(json_report_errors, output_dir, f"{chunk_errors.id}_errors")
    html_path_errors = save_html_report(html_report_errors, output_dir, f"{chunk_errors.id}_errors")

    print(f"  [OK] Text report: {text_path_errors.name}")
    print(f"  [OK] JSON report: {json_path_errors.name}")
    print(f"  [OK] HTML report: {html_path_errors.name}")
    print()

    # ========================================================================
    # Summary
    # ========================================================================
    print("=" * 80)
    print("MANUAL VALIDATION CHECKLIST")
    print("=" * 80)
    print()
    print("Please manually verify the following:")
    print()
    print("1. TEXT REPORTS (open in text editor):")
    print(f"   - Good: {text_path_good}")
    print(f"   - Errors: {text_path_errors}")
    print("   [ ] Check formatting is readable")
    print("   [ ] Check issue details are clear")
    print("   [ ] Check color codes display properly (if viewing in terminal)")
    print()
    print("2. JSON REPORTS (open in JSON viewer or editor):")
    print(f"   - Good: {json_path_good}")
    print(f"   - Errors: {json_path_errors}")
    print("   [ ] Verify valid JSON structure")
    print("   [ ] Check all fields are present")
    print("   [ ] Verify Unicode characters preserved")
    print()
    print("3. HTML REPORTS (open in web browser):")
    print(f"   - Good: file:///{html_path_good.as_posix()}")
    print(f"   - Errors: file:///{html_path_errors.as_posix()}")
    print("   [ ] Check styling displays correctly")
    print("   [ ] Check color coding (red/yellow/blue for severities)")
    print("   [ ] Check tables are formatted properly")
    print("   [ ] Verify special characters display correctly")
    print()
    print("=" * 80)
    print("NOTE:")
    print("  The text reports contain Rich formatting with Unicode box-drawing")
    print("  characters that may not display correctly in this console.")
    print("  Please open the saved report files directly to view them properly.")
    print("=" * 80)
    print()
    print("All reports have been successfully generated!")
    print()


if __name__ == "__main__":
    main()
