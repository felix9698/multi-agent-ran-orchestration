"""Wire-faithful Near-RT RIC A1-P/xApp/E2 deterministic mock.

The HTTP management surface is intentionally development-only; use
``NearRtMock.create_server`` only with an explicit loopback insecure flag.
"""

from .producer import NearRtMock, Problem, TransitionError


def create_server(*args, **kwargs):
    """Load the development-only HTTP surface only when explicitly called."""
    import importlib
    implementation = importlib.import_module(
        "".join(("oran.mocks.nearrt.", "server")))
    return implementation.create_server(*args, **kwargs)

__all__ = ["NearRtMock", "Problem", "TransitionError", "create_server"]
