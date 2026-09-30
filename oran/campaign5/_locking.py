"""One reentrant lifecycle boundary shared by the producer and its workers."""

from functools import wraps


def serialized(method):
    @wraps(method)
    def locked(self, *args, **kwargs):
        with self._lifecycle_lock:
            return method(self, *args, **kwargs)
    return locked
