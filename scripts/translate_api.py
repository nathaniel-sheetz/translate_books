#!/usr/bin/env python3
"""
Translate chunks using Anthropic Claude or OpenAI GPT APIs.

Supports both real-time and batch translation modes.
"""

import argparse
import sys
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn
from rich.table import Table

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.models import Chunk
from src.utils.file_io import load_chunk, load_glossary, load_style_guide, save_chunk
from src.api_translator import (
    translate_chunk_realtime,
    submit_batch,
    check_batch_status,
    retrieve_batch_results,
    estimate_cost,
    save_batch_job,
    get_batch_job,
    update_batch_job_status,
    load_batch_jobs,
    APIError,
    APIKeyError,
    RateLimitError,
    CostLimitError,
)

console = Console()


def load_chunks_from_patterns(patterns: list[str]) -> list[Chunk]:
    """Load chunks from file patterns (supports globs)."""
    import glob

    chunks = []
    for pattern in patterns:
        # Expand glob pattern
        matching_files = glob.glob(pattern)

        if not matching_files:
            console.print(f"[yellow]Warning: No files match pattern: {pattern}[/yellow]")
            continue

        for file_path in matching_files:
            try:
                chunk = load_chunk(Path(file_path))
                chunks.append(chunk)
            except Exception as e:
                console.print(f"[red]Error loading {file_path}: {e}[/red]")

    return chunks


def translate_realtime(args):
    """Execute real-time translation."""
    console.print("\n[bold cyan]Real-time Translation Mode[/bold cyan]\n")

    # Load chunks
    console.print("[bold]Loading chunks...[/bold]")
    chunks = load_chunks_from_patterns(args.chunk_files)

    if not chunks:
        console.print("[red]Error: No chunks loaded. Check your file patterns.[/red]")
        return 1

    console.print(f"Loaded {len(chunks)} chunk(s)")

    # Load optional resources
    glossary = None
    if args.glossary:
        try:
            glossary = load_glossary(Path(args.glossary))
            console.print(f"Loaded glossary: {args.glossary}")
        except Exception as e:
            console.print(f"[yellow]Warning: Could not load glossary: {e}[/yellow]")

    style_guide = None
    if args.style_guide:
        try:
            style_guide = load_style_guide(Path(args.style_guide))
            console.print(f"Loaded style guide: {args.style_guide}")
        except Exception as e:
            console.print(f"[yellow]Warning: Could not load style guide: {e}[/yellow]")

    # Estimate cost
    console.print("\n[bold]Estimating cost...[/bold]")
    cost_info = estimate_cost(
        chunks,
        args.provider,
        args.model,
        batch_mode=False,
        glossary=glossary,
        style_guide=style_guide
    )

    console.print(f"Estimated input tokens: {cost_info['input_tokens']:,}")
    console.print(f"Estimated output tokens: {cost_info['output_tokens_estimate']:,}")
    console.print(f"[bold]Estimated cost: ${cost_info['cost_usd']:.4f}[/bold]")
    console.print(f"Cost per chunk: ${cost_info['cost_per_chunk_usd']:.4f}")

    # Check cost limit
    if args.cost_limit and cost_info['cost_usd'] > args.cost_limit:
        console.print(
            f"\n[red]Error: Estimated cost ${cost_info['cost_usd']:.4f} "
            f"exceeds limit ${args.cost_limit:.2f}[/red]"
        )
        console.print("[yellow]Tip: Use --batch mode for 50% discount[/yellow]")
        return 1

    # Confirm
    if not args.yes:
        response = input("\nProceed with translation? [y/N]: ")
        if response.lower() != 'y':
            console.print("Aborted.")
            return 0

    # Create output directory
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Translate chunks
    console.print(f"\n[bold]Translating {len(chunks)} chunk(s) with {args.provider} ({args.model})...[/bold]\n")

    successful = 0
    failed = 0

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        console=console
    ) as progress:
        task = progress.add_task("Translating...", total=len(chunks))

        for chunk in chunks:
            try:
                # Translate
                updated_chunk = translate_chunk_realtime(
                    chunk=chunk,
                    provider=args.provider,
                    model=args.model,
                    glossary=glossary,
                    style_guide=style_guide,
                    project_name=args.project_name,
                    source_language=args.source_language,
                    target_language=args.target_language,
                    max_retries=3,
                )

                # Save
                output_path = output_dir / f"{updated_chunk.id}.json"
                save_chunk(updated_chunk, output_path)

                successful += 1
                progress.update(task, advance=1, description=f"Translated: {chunk.id}")

            except (APIKeyError, RateLimitError, APIError) as e:
                console.print(f"\n[red]Error translating {chunk.id}: {e}[/red]")
                failed += 1
                progress.update(task, advance=1)

                if isinstance(e, APIKeyError):
                    console.print("[red]Check your API key configuration in .env file[/red]")
                    return 1

    # Summary
    console.print(f"\n[bold green]Translation complete![/bold green]")
    console.print(f"Successful: {successful}")
    console.print(f"Failed: {failed}")
    console.print(f"Output directory: {output_dir}")

    return 0 if failed == 0 else 1


def translate_batch(args):
    """Execute batch translation."""
    console.print("\n[bold cyan]Batch Translation Mode[/bold cyan]\n")
    console.print("[yellow]Note: Batch results take ~24 hours. 50% cost discount applied.[/yellow]\n")

    # Load chunks
    console.print("[bold]Loading chunks...[/bold]")
    chunks = load_chunks_from_patterns(args.chunk_files)

    if not chunks:
        console.print("[red]Error: No chunks loaded. Check your file patterns.[/red]")
        return 1

    console.print(f"Loaded {len(chunks)} chunk(s)")

    # Load optional resources
    glossary = None
    if args.glossary:
        try:
            glossary = load_glossary(Path(args.glossary))
            console.print(f"Loaded glossary: {args.glossary}")
        except Exception as e:
            console.print(f"[yellow]Warning: Could not load glossary: {e}[/yellow]")

    style_guide = None
    if args.style_guide:
        try:
            style_guide = load_style_guide(Path(args.style_guide))
            console.print(f"Loaded style guide: {args.style_guide}")
        except Exception as e:
            console.print(f"[yellow]Warning: Could not load style guide: {e}[/yellow]")

    # Estimate cost
    console.print("\n[bold]Estimating cost (with 50% batch discount)...[/bold]")
    cost_info = estimate_cost(
        chunks,
        args.provider,
        args.model,
        batch_mode=True,
        glossary=glossary,
        style_guide=style_guide
    )

    console.print(f"Estimated input tokens: {cost_info['input_tokens']:,}")
    console.print(f"Estimated output tokens: {cost_info['output_tokens_estimate']:,}")
    console.print(f"[bold]Estimated cost: ${cost_info['cost_usd']:.4f}[/bold] (50% discount applied)")
    console.print(f"Cost per chunk: ${cost_info['cost_per_chunk_usd']:.4f}")

    # Check cost limit
    if args.cost_limit and cost_info['cost_usd'] > args.cost_limit:
        console.print(
            f"\n[red]Error: Estimated cost ${cost_info['cost_usd']:.4f} "
            f"exceeds limit ${args.cost_limit:.2f}[/red]"
        )
        return 1

    # Confirm
    if not args.yes:
        response = input("\nSubmit batch job? [y/N]: ")
        if response.lower() != 'y':
            console.print("Aborted.")
            return 0

    # Create output directory
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Submit batch
    try:
        console.print(f"\n[bold]Submitting batch to {args.provider}...[/bold]")

        job_info = submit_batch(
            chunks=chunks,
            provider=args.provider,
            model=args.model,
            output_dir=output_dir,
            glossary=glossary,
            style_guide=style_guide,
            project_name=args.project_name,
            source_language=args.source_language,
            target_language=args.target_language,
        )

        # Save job info
        save_batch_job(job_info)

        console.print(f"\n[bold green]Batch job submitted successfully![/bold green]")
        console.print(f"Job ID: {job_info['job_id']}")
        console.print(f"Status: {job_info['status']}")
        console.print(f"Chunks: {job_info['chunk_count']}")
        console.print(f"\n[yellow]Check status with:[/yellow]")
        console.print(f"  python translate_api.py --check-batch {job_info['job_id']}")

        return 0

    except (APIKeyError, APIError) as e:
        console.print(f"\n[red]Error submitting batch: {e}[/red]")
        if isinstance(e, APIKeyError):
            console.print("[red]Check your API key configuration in .env file[/red]")
        return 1


def check_batch(args):
    """Check batch job status and retrieve results if complete."""
    console.print(f"\n[bold cyan]Checking Batch Job: {args.check_batch}[/bold cyan]\n")

    # Load job info
    job_info = get_batch_job(args.check_batch)

    if not job_info:
        console.print(f"[red]Error: Batch job {args.check_batch} not found in batch_jobs.json[/red]")
        console.print("[yellow]Tip: Make sure you're in the same directory where the batch was submitted.[/yellow]")
        return 1

    provider = job_info["provider"]

    try:
        # Check status
        status_info = check_batch_status(args.check_batch, provider)

        console.print(f"[bold]Job ID:[/bold] {status_info['job_id']}")
        console.print(f"[bold]Provider:[/bold] {provider}")
        console.print(f"[bold]Model:[/bold] {job_info['model']}")
        console.print(f"[bold]Status:[/bold] {status_info['status']}")
        console.print(f"[bold]Total requests:[/bold] {status_info['total_count']}")
        console.print(f"[bold]Succeeded:[/bold] {status_info['succeeded_count']}")
        console.print(f"[bold]Failed:[/bold] {status_info['failed_count']}")

        if status_info['completed_at']:
            console.print(f"[bold]Completed at:[/bold] {status_info['completed_at']}")

        # If completed, offer to retrieve results
        if status_info['status'] in ['completed', 'ended']:
            console.print("\n[bold green]✓ Batch is complete![/bold green]")

            # Check if already retrieved
            if job_info.get('status') == 'completed':
                console.print("[yellow]Results already retrieved and saved.[/yellow]")
                return 0

            # Retrieve results
            console.print("\n[bold]Retrieving results...[/bold]")

            # Load original chunks
            chunk_ids = job_info.get('chunk_ids', [])
            # Try to reconstruct chunk paths
            # This is a best-effort; ideally we'd save chunk paths in job_info
            console.print(f"Need to load {len(chunk_ids)} original chunks...")

            # For now, just tell user to re-run with chunk files
            console.print("\n[yellow]To retrieve results, run:[/yellow]")
            console.print(f"  python translate_api.py --retrieve-batch {args.check_batch} <chunk_files>")

        else:
            console.print(f"\n[yellow]Batch is still processing. Check again later.[/yellow]")
            console.print(f"[dim]Estimated time: ~24 hours from submission[/dim]")

        return 0

    except APIError as e:
        console.print(f"\n[red]Error checking batch status: {e}[/red]")
        return 1


def list_batches(args):
    """List all batch jobs."""
    console.print("\n[bold cyan]Batch Jobs[/bold cyan]\n")

    jobs = load_batch_jobs()

    if not jobs:
        console.print("[yellow]No batch jobs found.[/yellow]")
        return 0

    # Create table
    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("Job ID", style="dim")
    table.add_column("Provider")
    table.add_column("Model")
    table.add_column("Status")
    table.add_column("Chunks", justify="right")
    table.add_column("Submitted")

    for job in jobs:
        table.add_row(
            job['job_id'][:16] + "...",
            job['provider'],
            job['model'],
            job['status'],
            str(job['chunk_count']),
            job['submitted_at'][:19]
        )

    console.print(table)
    console.print(f"\nTotal: {len(jobs)} job(s)")

    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Translate chunks using Anthropic Claude or OpenAI GPT APIs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Real-time translation with Claude
  python translate_api.py chunks/*.json --provider anthropic --model claude-3-5-sonnet-20241022

  # Batch translation (50% discount, ~24h processing)
  python translate_api.py chunks/*.json --provider openai --model gpt-4o --batch

  # With glossary and style guide
  python translate_api.py chunks/*.json --provider anthropic --glossary glossary.json --style-guide style.md

  # Check batch status
  python translate_api.py --check-batch batch_abc123

  # List all batch jobs
  python translate_api.py --list-batches
        """
    )

    # Batch management commands
    parser.add_argument(
        '--check-batch',
        metavar='JOB_ID',
        help='Check status of a batch job'
    )

    parser.add_argument(
        '--list-batches',
        action='store_true',
        help='List all batch jobs'
    )

    # Main arguments (for translation)
    parser.add_argument(
        'chunk_files',
        nargs='*',
        help='Chunk JSON files (supports glob patterns like chunks/*.json)'
    )

    parser.add_argument(
        '--provider',
        choices=['anthropic', 'openai'],
        default='anthropic',
        help='API provider (default: anthropic)'
    )

    parser.add_argument(
        '--model',
        default='claude-3-5-sonnet-20241022',
        help='Model to use (default: claude-3-5-sonnet-20241022)'
    )

    parser.add_argument(
        '--batch',
        action='store_true',
        help='Use batch API (50%% discount, ~24h processing time)'
    )

    parser.add_argument(
        '--glossary',
        help='Path to glossary JSON file'
    )

    parser.add_argument(
        '--style-guide',
        help='Path to style guide file'
    )

    parser.add_argument(
        '--output',
        default='chunks/translated',
        help='Output directory for translated chunks (default: chunks/translated)'
    )

    parser.add_argument(
        '--project-name',
        default='Translation Project',
        help='Project name for context (default: "Translation Project")'
    )

    parser.add_argument(
        '--source-language',
        default='English',
        help='Source language (default: English)'
    )

    parser.add_argument(
        '--target-language',
        default='Spanish',
        help='Target language (default: Spanish)'
    )

    parser.add_argument(
        '--cost-limit',
        type=float,
        help='Maximum cost in USD (abort if estimate exceeds this)'
    )

    parser.add_argument(
        '--max-concurrent',
        type=int,
        default=5,
        help='Maximum concurrent API requests (real-time mode only, default: 5)'
    )

    parser.add_argument(
        '-y', '--yes',
        action='store_true',
        help='Skip confirmation prompts'
    )

    parser.add_argument(
        '-v', '--verbose',
        action='store_true',
        help='Verbose output'
    )

    args = parser.parse_args()

    # Handle batch management commands
    if args.check_batch:
        return check_batch(args)

    if args.list_batches:
        return list_batches(args)

    # Normal translation mode
    if not args.chunk_files:
        parser.print_help()
        console.print("\n[red]Error: No chunk files specified[/red]")
        return 1

    # Execute translation
    if args.batch:
        return translate_batch(args)
    else:
        return translate_realtime(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user[/yellow]")
        sys.exit(1)
    except Exception as e:
        console.print(f"\n[red]Unexpected error: {e}[/red]")
        if "--verbose" in sys.argv:
            raise
        sys.exit(1)
