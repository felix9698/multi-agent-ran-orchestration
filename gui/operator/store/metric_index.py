"""Re-export of :mod:`runstore.metric_index`.

See ``gui/operator/store/records.py`` for why the module moved and why this
path stays.  The registry it reads is unchanged:
``docs/phase-b-gui/metric-registry.1.0.0.json``.
"""

from runstore.metric_index import *  # noqa: F401,F403
