"""
Evaluation report generation in multiple formats.

This module generates human-readable reports from evaluation results,
supporting three output formats:
- Text: Console-friendly Rich-formatted output
- JSON: Machine-readable structured data
- HTML: Web-viewable formatted report with embedded CSS

Typical usage:
    from src.evaluators import run_all_evaluators, aggregate_results
    from src.evaluators.reporting import generate_text_report

    results = run_all_evaluators(chunk, config, glossary)
    aggregated = aggregate_results(results)
    report = generate_text_report(results, aggregated)
    print(report)
"""

import json
import html as html_module
from datetime import datetime
from typing import Any, Optional
from io import StringIO

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich import box

from src.models import Chunk, EvalResult, Issue, IssueLevel


def _format_severity_emoji(level: IssueLevel) -> str:
    """
    Get emoji representation for issue severity.

    Args:
        level: Issue severity level

    Returns:
        Emoji character as string
    """
    if level == IssueLevel.ERROR:
        return "❌"
    elif level == IssueLevel.WARNING:
        return "⚠️"
    else:  # INFO
        return "ℹ️"


def _format_severity_text(level: IssueLevel) -> str:
    """
    Get text representation for issue severity.

    Args:
        level: Issue severity level

    Returns:
        Severity as uppercase string
    """
    return level.value.upper()


def _format_timestamp(dt: datetime) -> str:
    """
    Format datetime as human-readable string.

    Args:
        dt: Datetime to format

    Returns:
        Formatted timestamp string
    """
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _escape_html(text: str) -> str:
    """
    Escape HTML special characters.

    Args:
        text: Text to escape

    Returns:
        HTML-escaped text
    """
    return html_module.escape(text)


def _generate_summary_table(aggregated: dict) -> Table:
    """
    Generate Rich table showing summary of evaluator results.

    Args:
        aggregated: Aggregated results from aggregate_results()

    Returns:
        Rich Table object
    """
    table = Table(title="Evaluator Summary", box=box.ROUNDED, show_header=True)

    table.add_column("Evaluator", style="cyan", no_wrap=True)
    table.add_column("Version", style="dim")
    table.add_column("Status", justify="center")
    table.add_column("Score", justify="right")
    table.add_column("Issues", justify="right")
    table.add_column("Errors", justify="right", style="red")
    table.add_column("Warnings", justify="right", style="yellow")
    table.add_column("Info", justify="right", style="blue")

    for eval_result in aggregated.get("evaluator_results", []):
        status_text = "✅ PASS" if eval_result["passed"] else "❌ FAIL"
        status_style = "green" if eval_result["passed"] else "red"

        score_text = f"{eval_result['score']:.2f}" if eval_result["score"] is not None else "—"

        table.add_row(
            eval_result["name"],
            eval_result["version"],
            Text(status_text, style=status_style),
            score_text,
            str(eval_result["issues"]),
            str(eval_result["errors"]),
            str(eval_result["warnings"]),
            str(eval_result["info"])
        )

    return table


def generate_text_report(
    results: list[EvalResult],
    aggregated: dict,
    chunk: Optional[Chunk] = None
) -> str:
    """
    Generate console-friendly text report with Rich formatting.

    This report uses Rich library to create colorful, well-formatted
    output suitable for terminal display or text file saving.

    Args:
        results: List of EvalResult objects from evaluators
        aggregated: Aggregated statistics from aggregate_results()
        chunk: Optional Chunk object for context

    Returns:
        Formatted text report as string (includes ANSI color codes)

    Example:
        >>> results = run_all_evaluators(chunk, config, glossary)
        >>> aggregated = aggregate_results(results)
        >>> report = generate_text_report(results, aggregated, chunk)
        >>> print(report)
    """
    console = Console(file=StringIO(), width=100, record=True)

    # Header
    console.print()
    overall_status = "✅ PASSED" if aggregated["overall_passed"] else "❌ FAILED"
    status_style = "bold green" if aggregated["overall_passed"] else "bold red"

    if chunk:
        title = f"Evaluation Report: {chunk.id}"
    else:
        title = "Evaluation Report"

    console.print(Panel(
        Text(overall_status, style=status_style, justify="center"),
        title=title,
        border_style=status_style
    ))
    console.print()

    # Overall statistics
    console.print("[bold]Overall Statistics:[/bold]")
    console.print(f"  Evaluators Run: {aggregated['total_evaluators']}")
    console.print(f"  Passed: [green]{aggregated['passed_evaluators']}[/green]")
    console.print(f"  Failed: [red]{aggregated['failed_evaluators']}[/red]")
    console.print(f"  Average Score: {aggregated['average_score']:.2f}" if aggregated['average_score'] is not None else "  Average Score: —")
    console.print()

    console.print("[bold]Issue Counts:[/bold]")
    console.print(f"  Total Issues: {aggregated['total_issues']}")
    console.print(f"  ❌ Errors: [red]{aggregated['issues_by_severity']['error']}[/red]")
    console.print(f"  ⚠️  Warnings: [yellow]{aggregated['issues_by_severity']['warning']}[/yellow]")
    console.print(f"  ℹ️  Info: [blue]{aggregated['issues_by_severity']['info']}[/blue]")
    console.print()

    # Summary table
    table = _generate_summary_table(aggregated)
    console.print(table)
    console.print()

    # Detailed issues by severity
    if aggregated["total_issues"] > 0:
        console.print("[bold]Detailed Issues:[/bold]")
        console.print()

        # Group issues by severity
        for severity in [IssueLevel.ERROR, IssueLevel.WARNING, IssueLevel.INFO]:
            severity_issues = []
            for result in results:
                for issue in result.issues:
                    if issue.severity == severity:
                        severity_issues.append((result.eval_name, issue))

            if severity_issues:
                emoji = _format_severity_emoji(severity)
                severity_text = _format_severity_text(severity)

                if severity == IssueLevel.ERROR:
                    style = "bold red"
                elif severity == IssueLevel.WARNING:
                    style = "bold yellow"
                else:
                    style = "bold blue"

                console.print(f"[{style}]{emoji} {severity_text} ({len(severity_issues)}):[/{style}]")

                for eval_name, issue in severity_issues:
                    console.print(f"  [{eval_name}] {issue.message}")
                    if issue.location:
                        console.print(f"    Location: {issue.location}", style="dim")
                    if issue.suggestion:
                        console.print(f"    💡 Suggestion: {issue.suggestion}", style="italic")

                console.print()
    else:
        console.print("[bold green]✨ No issues found! Translation looks great.[/bold green]")
        console.print()

    # Chunk context
    if chunk:
        console.print("[bold]Chunk Information:[/bold]")
        console.print(f"  ID: {chunk.id}")
        console.print(f"  Chapter: {chunk.chapter_id}")
        console.print(f"  Position: {chunk.position}")
        console.print(f"  Source words: {chunk.word_count}")
        console.print(f"  Translation words: {chunk.translation_word_count}")
        console.print()

    # Footer with timestamp
    timestamp = _format_timestamp(datetime.now())
    console.print(f"[dim]Report generated: {timestamp}[/dim]")
    console.print()

    # Export as string
    return console.export_text()


def generate_json_report(
    results: list[EvalResult],
    aggregated: dict,
    chunk: Optional[Chunk] = None
) -> str:
    """
    Generate machine-readable JSON report.

    This report combines aggregated statistics with full evaluation
    results in a structured JSON format suitable for tooling integration.

    Args:
        results: List of EvalResult objects from evaluators
        aggregated: Aggregated statistics from aggregate_results()
        chunk: Optional Chunk object for context

    Returns:
        JSON string (pretty-printed with indent=2)

    Example:
        >>> results = run_all_evaluators(chunk, config, glossary)
        >>> aggregated = aggregate_results(results)
        >>> json_report = generate_json_report(results, aggregated, chunk)
        >>> with open('report.json', 'w') as f:
        ...     f.write(json_report)
    """
    # Convert datetime objects to ISO format
    def serialize_datetime(obj: Any) -> Any:
        if isinstance(obj, datetime):
            return obj.isoformat()
        raise TypeError(f"Object of type {type(obj)} is not JSON serializable")

    # Build report structure
    report_data = {
        "report_type": "evaluation",
        "generated_at": datetime.now().isoformat(),
        "summary": {
            "overall_passed": aggregated["overall_passed"],
            "total_evaluators": aggregated["total_evaluators"],
            "passed_evaluators": aggregated["passed_evaluators"],
            "failed_evaluators": aggregated["failed_evaluators"],
            "average_score": aggregated["average_score"],
            "total_issues": aggregated["total_issues"],
            "issues_by_severity": aggregated["issues_by_severity"],
            "issues_by_evaluator": aggregated["issues_by_evaluator"]
        },
        "evaluators": aggregated["evaluator_results"],
        "detailed_results": [
            {
                "eval_name": result.eval_name,
                "eval_version": result.eval_version,
                "target_id": result.target_id,
                "target_type": result.target_type,
                "passed": result.passed,
                "score": result.score,
                "executed_at": result.executed_at.isoformat(),
                "issues": [
                    {
                        "severity": issue.severity.value,
                        "message": issue.message,
                        "location": issue.location,
                        "suggestion": issue.suggestion
                    }
                    for issue in result.issues
                ],
                "metadata": result.metadata
            }
            for result in results
        ]
    }

    # Add chunk information if provided
    if chunk:
        report_data["chunk"] = {
            "id": chunk.id,
            "chapter_id": chunk.chapter_id,
            "position": chunk.position,
            "status": chunk.status.value,
            "source_word_count": chunk.word_count,
            "translation_word_count": chunk.translation_word_count,
            "source_preview": chunk.source_text[:100] + "..." if len(chunk.source_text) > 100 else chunk.source_text,
            "translation_preview": (chunk.translated_text[:100] + "..." if chunk.translated_text and len(chunk.translated_text) > 100 else chunk.translated_text) if chunk.translated_text else None
        }

    # Serialize to JSON
    return json.dumps(report_data, indent=2, ensure_ascii=False, default=serialize_datetime)


def generate_html_report(
    results: list[EvalResult],
    aggregated: dict,
    chunk: Optional[Chunk] = None
) -> str:
    """
    Generate web-viewable HTML report with embedded CSS.

    This report creates a self-contained HTML file with styling,
    suitable for viewing in a web browser.

    Args:
        results: List of EvalResult objects from evaluators
        aggregated: Aggregated statistics from aggregate_results()
        chunk: Optional Chunk object for context

    Returns:
        Complete HTML document as string

    Example:
        >>> results = run_all_evaluators(chunk, config, glossary)
        >>> aggregated = aggregate_results(results)
        >>> html_report = generate_html_report(results, aggregated, chunk)
        >>> with open('report.html', 'w', encoding='utf-8') as f:
        ...     f.write(html_report)
    """
    timestamp = _format_timestamp(datetime.now())
    overall_status = "PASSED" if aggregated["overall_passed"] else "FAILED"
    status_class = "pass" if aggregated["overall_passed"] else "fail"

    # Build HTML
    html_parts = []

    # HTML header with embedded CSS
    html_parts.append("""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Evaluation Report</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }

        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            line-height: 1.6;
            color: #333;
            background: #f5f5f5;
            padding: 20px;
        }

        .container {
            max-width: 1200px;
            margin: 0 auto;
            background: white;
            padding: 30px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }

        h1, h2, h3 {
            margin-bottom: 15px;
        }

        h1 {
            color: #2c3e50;
            border-bottom: 3px solid #3498db;
            padding-bottom: 10px;
        }

        h2 {
            color: #34495e;
            margin-top: 30px;
            border-bottom: 2px solid #ecf0f1;
            padding-bottom: 8px;
        }

        h3 {
            color: #7f8c8d;
            margin-top: 20px;
        }

        .status-banner {
            padding: 20px;
            border-radius: 5px;
            text-align: center;
            font-size: 24px;
            font-weight: bold;
            margin-bottom: 30px;
        }

        .status-banner.pass {
            background: #d4edda;
            color: #155724;
            border: 2px solid #c3e6cb;
        }

        .status-banner.fail {
            background: #f8d7da;
            color: #721c24;
            border: 2px solid #f5c6cb;
        }

        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 15px;
            margin: 20px 0;
        }

        .stat-card {
            padding: 15px;
            border-radius: 5px;
            background: #ecf0f1;
        }

        .stat-label {
            font-size: 14px;
            color: #7f8c8d;
            margin-bottom: 5px;
        }

        .stat-value {
            font-size: 28px;
            font-weight: bold;
            color: #2c3e50;
        }

        .stat-card.error { background: #fee; color: #c00; }
        .stat-card.error .stat-value { color: #c00; }
        .stat-card.warning { background: #fffbf0; color: #856404; }
        .stat-card.warning .stat-value { color: #856404; }
        .stat-card.info { background: #e7f3ff; color: #004085; }
        .stat-card.info .stat-value { color: #004085; }

        table {
            width: 100%;
            border-collapse: collapse;
            margin: 20px 0;
        }

        th, td {
            padding: 12px;
            text-align: left;
            border-bottom: 1px solid #ddd;
        }

        th {
            background: #34495e;
            color: white;
            font-weight: bold;
        }

        tr:hover {
            background: #f8f9fa;
        }

        .badge {
            display: inline-block;
            padding: 4px 8px;
            border-radius: 3px;
            font-size: 12px;
            font-weight: bold;
        }

        .badge.pass {
            background: #d4edda;
            color: #155724;
        }

        .badge.fail {
            background: #f8d7da;
            color: #721c24;
        }

        .issue-section {
            margin: 15px 0;
            padding: 15px;
            border-left: 4px solid #ccc;
            background: #f8f9fa;
            border-radius: 4px;
        }

        .issue-section.error {
            border-left-color: #dc3545;
            background: #fff5f5;
        }

        .issue-section.warning {
            border-left-color: #ffc107;
            background: #fffbf0;
        }

        .issue-section.info {
            border-left-color: #17a2b8;
            background: #e7f3ff;
        }

        .issue {
            margin: 10px 0;
            padding: 10px;
            background: white;
            border-radius: 3px;
        }

        .issue-header {
            font-weight: bold;
            margin-bottom: 5px;
        }

        .issue-location {
            font-size: 14px;
            color: #6c757d;
            margin: 5px 0;
        }

        .issue-suggestion {
            font-style: italic;
            color: #007bff;
            margin-top: 5px;
        }

        .chunk-info {
            background: #e9ecef;
            padding: 15px;
            border-radius: 5px;
            margin: 20px 0;
        }

        .chunk-info dt {
            font-weight: bold;
            margin-top: 10px;
        }

        .chunk-info dd {
            margin-left: 20px;
        }

        .footer {
            margin-top: 30px;
            padding-top: 15px;
            border-top: 1px solid #ddd;
            text-align: center;
            color: #6c757d;
            font-size: 14px;
        }

        .no-issues {
            text-align: center;
            padding: 40px;
            color: #28a745;
            font-size: 18px;
        }
    </style>
</head>
<body>
    <div class="container">
""")

    # Title and status banner
    title = f"Evaluation Report: {_escape_html(chunk.id)}" if chunk else "Evaluation Report"
    html_parts.append(f"        <h1>{title}</h1>\n")
    html_parts.append(f'        <div class="status-banner {status_class}">{overall_status}</div>\n')

    # Overall statistics
    html_parts.append('        <h2>Overall Statistics</h2>\n')
    html_parts.append('        <div class="stats-grid">\n')
    html_parts.append(f'            <div class="stat-card"><div class="stat-label">Evaluators Run</div><div class="stat-value">{aggregated["total_evaluators"]}</div></div>\n')
    html_parts.append(f'            <div class="stat-card"><div class="stat-label">Passed</div><div class="stat-value">{aggregated["passed_evaluators"]}</div></div>\n')
    html_parts.append(f'            <div class="stat-card"><div class="stat-label">Failed</div><div class="stat-value">{aggregated["failed_evaluators"]}</div></div>\n')

    avg_score = f'{aggregated["average_score"]:.2f}' if aggregated["average_score"] is not None else "—"
    html_parts.append(f'            <div class="stat-card"><div class="stat-label">Average Score</div><div class="stat-value">{avg_score}</div></div>\n')
    html_parts.append(f'            <div class="stat-card"><div class="stat-label">Total Issues</div><div class="stat-value">{aggregated["total_issues"]}</div></div>\n')
    html_parts.append(f'            <div class="stat-card error"><div class="stat-label">Errors</div><div class="stat-value">{aggregated["issues_by_severity"]["error"]}</div></div>\n')
    html_parts.append(f'            <div class="stat-card warning"><div class="stat-label">Warnings</div><div class="stat-value">{aggregated["issues_by_severity"]["warning"]}</div></div>\n')
    html_parts.append(f'            <div class="stat-card info"><div class="stat-label">Info</div><div class="stat-value">{aggregated["issues_by_severity"]["info"]}</div></div>\n')
    html_parts.append('        </div>\n')

    # Evaluator summary table
    html_parts.append('        <h2>Evaluator Summary</h2>\n')
    html_parts.append('        <table>\n')
    html_parts.append('            <thead><tr><th>Evaluator</th><th>Version</th><th>Status</th><th>Score</th><th>Issues</th><th>Errors</th><th>Warnings</th><th>Info</th></tr></thead>\n')
    html_parts.append('            <tbody>\n')

    for eval_result in aggregated["evaluator_results"]:
        status_badge = "pass" if eval_result["passed"] else "fail"
        status_text = "PASS" if eval_result["passed"] else "FAIL"
        score_text = f'{eval_result["score"]:.2f}' if eval_result["score"] is not None else "—"

        html_parts.append(f'                <tr>\n')
        html_parts.append(f'                    <td>{_escape_html(eval_result["name"])}</td>\n')
        html_parts.append(f'                    <td>{_escape_html(eval_result["version"])}</td>\n')
        html_parts.append(f'                    <td><span class="badge {status_badge}">{status_text}</span></td>\n')
        html_parts.append(f'                    <td>{score_text}</td>\n')
        html_parts.append(f'                    <td>{eval_result["issues"]}</td>\n')
        html_parts.append(f'                    <td>{eval_result["errors"]}</td>\n')
        html_parts.append(f'                    <td>{eval_result["warnings"]}</td>\n')
        html_parts.append(f'                    <td>{eval_result["info"]}</td>\n')
        html_parts.append(f'                </tr>\n')

    html_parts.append('            </tbody>\n')
    html_parts.append('        </table>\n')

    # Detailed issues
    html_parts.append('        <h2>Detailed Issues</h2>\n')

    if aggregated["total_issues"] > 0:
        for severity in [IssueLevel.ERROR, IssueLevel.WARNING, IssueLevel.INFO]:
            severity_issues = []
            for result in results:
                for issue in result.issues:
                    if issue.severity == severity:
                        severity_issues.append((result.eval_name, issue))

            if severity_issues:
                emoji = _format_severity_emoji(severity)
                severity_text = _format_severity_text(severity)
                severity_class = severity.value

                html_parts.append(f'        <div class="issue-section {severity_class}">\n')
                html_parts.append(f'            <h3>{emoji} {severity_text} ({len(severity_issues)})</h3>\n')

                for eval_name, issue in severity_issues:
                    html_parts.append(f'            <div class="issue">\n')
                    html_parts.append(f'                <div class="issue-header">[{_escape_html(eval_name)}] {_escape_html(issue.message)}</div>\n')

                    if issue.location:
                        html_parts.append(f'                <div class="issue-location">Location: {_escape_html(issue.location)}</div>\n')

                    if issue.suggestion:
                        html_parts.append(f'                <div class="issue-suggestion">💡 Suggestion: {_escape_html(issue.suggestion)}</div>\n')

                    html_parts.append(f'            </div>\n')

                html_parts.append(f'        </div>\n')
    else:
        html_parts.append('        <div class="no-issues">✨ No issues found! Translation looks great.</div>\n')

    # Chunk information
    if chunk:
        html_parts.append('        <h2>Chunk Information</h2>\n')
        html_parts.append('        <div class="chunk-info">\n')
        html_parts.append('            <dl>\n')
        html_parts.append(f'                <dt>ID</dt><dd>{_escape_html(chunk.id)}</dd>\n')
        html_parts.append(f'                <dt>Chapter</dt><dd>{_escape_html(chunk.chapter_id)}</dd>\n')
        html_parts.append(f'                <dt>Position</dt><dd>{chunk.position}</dd>\n')
        html_parts.append(f'                <dt>Status</dt><dd>{_escape_html(chunk.status.value)}</dd>\n')
        html_parts.append(f'                <dt>Source Word Count</dt><dd>{chunk.word_count}</dd>\n')
        html_parts.append(f'                <dt>Translation Word Count</dt><dd>{chunk.translation_word_count}</dd>\n')
        html_parts.append('            </dl>\n')
        html_parts.append('        </div>\n')

    # Footer
    html_parts.append(f'        <div class="footer">Report generated: {_escape_html(timestamp)}</div>\n')
    html_parts.append('    </div>\n')
    html_parts.append('</body>\n')
    html_parts.append('</html>')

    return ''.join(html_parts)
