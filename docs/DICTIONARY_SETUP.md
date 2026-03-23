# Dictionary Setup for PyEnchant

The dictionary evaluator requires Spanish dictionaries to be installed for PyEnchant.

## Current Status

✓ **COMPLETE** - Spanish dictionaries are now installed!

Installed dictionaries:
- es_ES (Spain Spanish) - Base dictionary
- es_MX (Mexican Spanish) - Mexican variant

The DictionaryEvaluator now checks BOTH dictionaries (OR logic), accepting words valid in either variant.

## Installation Options

### Option 1: Install Hunspell Spanish Dictionary (Windows)

1. Download Spanish dictionary files from:
   - https://github.com/wooorm/dictionaries/tree/main/dictionaries/es
   - Or: https://cgit.freedesktop.org/libreoffice/dictionaries/tree/es

2. You need two files:
   - `es_ES.aff` (affix file)
   - `es_ES.dic` (dictionary file)

3. Place them in PyEnchant's dictionary directory:
   ```
   C:\Users\{YourUsername}\AppData\Roaming\enchant\hunspell\
   ```

4. Verify installation:
   ```bash
   python -c "import enchant; print(enchant.list_dicts())"
   ```

### Option 2: Use Alternative Installation Method

PyEnchant on Windows can be tricky. Alternative approaches:

**A. Use LibreOffice dictionaries:**
1. Install LibreOffice (includes Spanish dictionaries)
2. PyEnchant may detect them automatically

**B. Use aspell (if you have Cygwin/MinGW):**
```bash
apt-get install aspell aspell-es
```

## Workaround for Development

For now, you can:

1. Skip dictionary evaluator until dictionaries are installed
2. Use only length and paragraph evaluators
3. Test with mock data

## Testing Dictionary Availability

Run this to see what's installed:
```bash
python -c "import enchant; print('Available:', [d[0] for d in enchant.list_dicts()])"
```

Expected output when working:
```
Available: ['en_US', 'es_ES', ...]
```

## Current Available Dictionaries

Currently installed:
- English variants: en_US, en_GB, en_CA, etc.
- **Spanish: ✓ INSTALLED** - es_ES, es_MX

## Next Steps

1. Install Spanish dictionary using one of the methods above
2. Run dictionary evaluator
3. Or continue with other evaluators and come back to this later

The dictionary evaluator will provide a helpful error message if dictionaries aren't found.
