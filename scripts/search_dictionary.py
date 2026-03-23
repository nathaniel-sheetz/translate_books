#!/usr/bin/env python
"""
Search Spanish dictionaries (es_ES and es_MX) for words.

This tool allows you to search the installed Spanish dictionaries to:
- Check if a word is in the dictionary
- See which dictionary contains it (es_ES, es_MX, or both)
- View affix flags (morphological rules)
- Search with wildcards (e.g., "casa*")
- Get spelling suggestions for words not found
- View dictionary statistics

Usage:
    python search_dictionary.py word1 word2 word3
    python search_dictionary.py "casa*"     # Pattern search
    python search_dictionary.py --stats     # Show dictionary statistics

Examples:
    python search_dictionary.py lascar
    python search_dictionary.py casa hola amigo
    python search_dictionary.py "animal*"
    python search_dictionary.py --stats
"""

import sys
import os
from pathlib import Path


def get_dictionary_path(dict_name):
    """Get the path to a dictionary file."""
    # Try to find the enchant dictionary directory
    try:
        import enchant
        enchant_dir = Path(enchant.__file__).parent
        hunspell_dir = enchant_dir / "data" / "mingw64" / "share" / "enchant" / "hunspell"
        dict_path = hunspell_dir / f"{dict_name}.dic"
        if dict_path.exists():
            return dict_path
    except ImportError:
        pass

    # Fallback: Try common locations
    user_home = Path.home()
    possible_paths = [
        user_home / "AppData" / "Roaming" / "Python" / "Python314" / "site-packages" / "enchant" / "data" / "mingw64" / "share" / "enchant" / "hunspell" / f"{dict_name}.dic",
        user_home / "AppData" / "Roaming" / "enchant" / "hunspell" / f"{dict_name}.dic",
    ]

    for path in possible_paths:
        if path.exists():
            return path

    return None


def load_dictionary(dict_name):
    """
    Load dictionary file into memory.

    Returns:
        dict: {word: affix_flags}
    """
    dict_path = get_dictionary_path(dict_name)
    if not dict_path:
        print(f"[ERROR] Dictionary file not found: {dict_name}")
        return {}

    words = {}
    try:
        with open(dict_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            # First line is word count
            total_words = lines[0].strip()
            print(f"[INFO] Loading {dict_name}: {total_words} words from {dict_path.name}")

            for line in lines[1:]:
                line = line.strip()
                if not line:
                    continue

                # Format: word or word/FLAGS
                if '/' in line:
                    word, flags = line.split('/', 1)
                    words[word.lower()] = flags
                else:
                    words[line.lower()] = ""

    except Exception as e:
        print(f"[ERROR] Failed to load {dict_name}: {e}")

    return words


def search_word(word, es_dict, mx_dict, case_sensitive=False):
    """
    Search for a word in both dictionaries.

    Args:
        word: Word to search for
        es_dict: es_ES dictionary
        mx_dict: es_MX dictionary
        case_sensitive: Whether to match case exactly

    Returns:
        dict: Results with found status and flags
    """
    search_word = word if case_sensitive else word.lower()

    result = {
        'word': word,
        'found': False,
        'in_es_ES': False,
        'in_es_MX': False,
        'es_ES_flags': None,
        'es_MX_flags': None,
    }

    if search_word in es_dict:
        result['found'] = True
        result['in_es_ES'] = True
        result['es_ES_flags'] = es_dict[search_word]

    if search_word in mx_dict:
        result['found'] = True
        result['in_es_MX'] = True
        result['es_MX_flags'] = mx_dict[search_word]

    return result


def pattern_search(pattern, es_dict, mx_dict, max_results=50):
    """
    Search for words matching a pattern.

    Args:
        pattern: Pattern to match (e.g., "casa*" or "*cion")
        es_dict: es_ES dictionary
        mx_dict: es_MX dictionary
        max_results: Maximum number of results to return

    Returns:
        list: Matching words
    """
    import re

    # Convert wildcard pattern to regex
    regex_pattern = pattern.replace('*', '.*').replace('?', '.')
    regex_pattern = f"^{regex_pattern}$"
    regex = re.compile(regex_pattern, re.IGNORECASE)

    matches = set()

    # Search es_ES
    for word in es_dict.keys():
        if regex.match(word):
            matches.add(word)
            if len(matches) >= max_results:
                break

    # Search es_MX
    for word in mx_dict.keys():
        if regex.match(word):
            matches.add(word)
            if len(matches) >= max_results:
                break

    return sorted(matches)[:max_results]


def get_suggestions(word):
    """Get spelling suggestions using enchant."""
    try:
        import enchant
        es = enchant.Dict("es_ES")
        suggestions = es.suggest(word)
        return suggestions[:5]
    except:
        return []


def print_results(result):
    """Print search results in a formatted way."""
    word = result['word']

    if result['found']:
        print(f"\n[FOUND] '{word}'")

        if result['in_es_ES'] and result['in_es_MX']:
            print("  Dictionaries: es_ES AND es_MX (both)")
        elif result['in_es_ES']:
            print("  Dictionaries: es_ES (Spain Spanish)")
        elif result['in_es_MX']:
            print("  Dictionaries: es_MX (Mexican Spanish)")

        if result['es_ES_flags']:
            print(f"  es_ES flags: {result['es_ES_flags']}")
        if result['es_MX_flags']:
            print(f"  es_MX flags: {result['es_MX_flags']}")
    else:
        print(f"\n[NOT FOUND] '{word}'")
        suggestions = get_suggestions(word)
        if suggestions:
            print(f"  Suggestions: {', '.join(suggestions)}")
        else:
            print("  No suggestions available")


def show_statistics(es_dict, mx_dict):
    """Show dictionary statistics."""
    print("\n" + "=" * 70)
    print("SPANISH DICTIONARY STATISTICS")
    print("=" * 70)
    print(f"\nes_ES (Spain Spanish):")
    print(f"  Total words: {len(es_dict):,}")

    print(f"\nes_MX (Mexican Spanish):")
    print(f"  Total words: {len(mx_dict):,}")

    # Find words unique to each dictionary
    es_only = set(es_dict.keys()) - set(mx_dict.keys())
    mx_only = set(mx_dict.keys()) - set(es_dict.keys())
    both = set(es_dict.keys()) & set(mx_dict.keys())

    print(f"\nOverlap:")
    print(f"  In both: {len(both):,}")
    print(f"  es_ES only: {len(es_only):,}")
    print(f"  es_MX only: {len(mx_only):,}")

    # Show sample of es_ES only words
    print(f"\nSample es_ES only words:")
    for word in sorted(es_only)[:10]:
        print(f"  - {word}")

    # Show sample of es_MX only words
    print(f"\nSample es_MX only words:")
    for word in sorted(mx_only)[:10]:
        print(f"  - {word}")

    print("\n" + "=" * 70)


def main():
    """Main function."""
    if len(sys.argv) < 2:
        print("Usage: python search_dictionary.py <word1> [word2] [word3] ...")
        print("\nExamples:")
        print("  python search_dictionary.py lascar")
        print("  python search_dictionary.py casa hola amigo")
        print('  python search_dictionary.py "casa*"      # Pattern search')
        print("  python search_dictionary.py --stats      # Show statistics")
        sys.exit(1)

    # Load dictionaries
    print("\n" + "=" * 70)
    print("SPANISH DICTIONARY SEARCH")
    print("=" * 70 + "\n")

    es_dict = load_dictionary("es_ES")
    mx_dict = load_dictionary("es_MX")

    if not es_dict and not mx_dict:
        print("\n[ERROR] No dictionaries found!")
        print("Make sure Spanish dictionaries are installed.")
        print("See DICTIONARY_SETUP.md for installation instructions.")
        sys.exit(1)

    print()

    # Handle --stats flag
    if "--stats" in sys.argv:
        show_statistics(es_dict, mx_dict)
        return

    # Search for each word
    words = [arg for arg in sys.argv[1:] if not arg.startswith('--')]

    for word in words:
        # Check if it's a pattern search
        if '*' in word or '?' in word:
            print(f"\n[PATTERN SEARCH] '{word}'")
            matches = pattern_search(word, es_dict, mx_dict)
            if matches:
                print(f"  Found {len(matches)} matches:")
                for i, match in enumerate(matches[:20], 1):
                    # Show which dict it's in
                    in_es = match in es_dict
                    in_mx = match in mx_dict
                    location = "both" if (in_es and in_mx) else ("es_ES" if in_es else "es_MX")
                    print(f"    {i:2d}. {match} ({location})")
                if len(matches) > 20:
                    print(f"    ... and {len(matches) - 20} more")
            else:
                print("  No matches found")
        else:
            # Regular word search
            result = search_word(word, es_dict, mx_dict)
            print_results(result)

    print("\n" + "=" * 70 + "\n")


if __name__ == "__main__":
    main()
