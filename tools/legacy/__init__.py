"""History-only research entries.

Nothing in this package is part of the deployed runtime.  It preserves the
pre-Kernel Coordinator console and the composition that reached it, so the
published experiments stay reproducible and their evidence stays readable, and
it keeps that runtime *out* of the default entry point: the deployed product is
``main.py`` -> Operator Console -> Assurance Kernel -> Write Gateway, and it
cannot construct any of this.
"""
