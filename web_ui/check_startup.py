"""
Quick startup check for the Translation Web UI.

Verifies that:
- Flask is installed
- Template files exist
- Static files exist
- Sample chunks can be loaded
"""

import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

def check_flask():
    """Check if Flask is installed."""
    try:
        import flask
        print("[OK] Flask is installed")
        return True
    except ImportError:
        print("[FAIL] Flask not installed - run: pip install Flask")
        return False

def check_templates():
    """Check if template files exist."""
    template_path = Path(__file__).parent / "templates" / "index.html"
    if template_path.exists():
        print(f"[OK] Template found: {template_path}")
        return True
    else:
        print(f"[FAIL] Template missing: {template_path}")
        return False

def check_static_files():
    """Check if static files exist."""
    static_dir = Path(__file__).parent / "static"
    js_file = static_dir / "app.js"
    css_file = static_dir / "style.css"

    all_ok = True

    if js_file.exists():
        print(f"[OK] JavaScript found: {js_file}")
    else:
        print(f"[FAIL] JavaScript missing: {js_file}")
        all_ok = False

    if css_file.exists():
        print(f"[OK] CSS found: {css_file}")
    else:
        print(f"[FAIL] CSS missing: {css_file}")
        all_ok = False

    return all_ok

def check_dependencies():
    """Check if required dependencies are available."""
    try:
        from src.models import Chunk
        from src.utils.file_io import load_chunk
        print("[OK] Core dependencies available")
        return True
    except ImportError as e:
        print(f"[FAIL] Missing dependency: {e}")
        return False

def check_sample_chunks():
    """Check if there are sample chunks to work with."""
    chunks_dir = Path(__file__).parent.parent / "chunks"

    if not chunks_dir.exists():
        print(f"[WARN] No chunks directory found at: {chunks_dir}")
        print("       (This is OK if you haven't created chunks yet)")
        return True

    json_files = list(chunks_dir.glob("*.json"))

    if len(json_files) == 0:
        print(f"[WARN] No chunk files found in: {chunks_dir}")
        print("       (This is OK if you haven't created chunks yet)")
        return True

    print(f"[OK] Found {len(json_files)} chunk file(s) in: {chunks_dir}")

    # Try loading one chunk to verify format
    try:
        from src.utils.file_io import load_chunk
        chunk = load_chunk(json_files[0])
        print(f"     Sample chunk: {chunk.id} ({chunk.metadata.word_count} words)")
        return True
    except Exception as e:
        print(f"[FAIL] Error loading chunk: {e}")
        return False

def main():
    """Run all checks."""
    print("=" * 70)
    print("Translation Web UI - Startup Check")
    print("=" * 70)
    print()

    checks = [
        ("Flask Installation", check_flask),
        ("Template Files", check_templates),
        ("Static Files", check_static_files),
        ("Core Dependencies", check_dependencies),
        ("Sample Chunks", check_sample_chunks),
    ]

    results = []
    for name, check_fn in checks:
        print(f"\n{name}:")
        print("-" * 70)
        results.append(check_fn())

    print()
    print("=" * 70)

    if all(results):
        print("[OK] All checks passed!")
        print()
        print("To start the web UI:")
        print("  cd web_ui")
        print("  python app.py")
        print()
        print("Then open your browser to: http://localhost:5000")
        return 0
    else:
        print("[FAIL] Some checks failed - see errors above")
        return 1

if __name__ == "__main__":
    sys.exit(main())
