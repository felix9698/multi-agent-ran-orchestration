"""Run-directory persistence, re-exported from the neutral ``runstore``.

One schema for Live, Replay, Synthetic and Emulated runs.  Schema authority:
``docs/phase-b-gui/session-store.1.0.0.schema.json``.

The implementation lives in the top-level :mod:`runstore` package: the run
directory is a storage format, not a rendering concern, and ``assurance/batch/``
writes the same one without being allowed to import a console.  The modules
here re-export it so every existing console import resolves unchanged.
"""
