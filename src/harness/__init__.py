"""Orchestration layer for the translate-harness skill.

The skill (``.claude/skills/translate-harness/SKILL.md``) used to compose the
fine-grained ``src/`` primitives itself, with ~nine inline-Python heredocs that
each re-derived ``sys.path``, loaded/saved repo-global ``.tmp/`` state, called one
helper, and printed for the agent. That glue was untested, unversioned, and would
be copy-pasted into every future harness-style skill.

This package is the missing composition layer. ``flow`` exposes one function per
harness beat (reusing the existing primitives — no new business logic), ``state``
owns per-project ``.harness/`` paths + config, and ``scripts/harness.py`` is the
thin CLI the skill calls. See ``src/harness_guard.py`` for the validation guards
the ``*_commit`` flows fold in.
"""
