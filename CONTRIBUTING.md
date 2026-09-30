# Contributing

Please open an issue describing the affected experiment or interface before a
substantial change. For a correction, include a minimal reproducer and the exact
configuration required; do not include credentials or subscriber secrets.

Run `python scripts/test_public_artifact.py` for the self-contained offline suite.
Mark simulated runs as MOCK and recorded runs as REPLAY. A source change, unit
test or command acknowledgement is not an OTA result.

Keep evaluation semantics and published baselines explicit. Do not replace
missing observations with favorable values, silently discard unsuccessful
episodes, or mix configuration/provenance from different runs.

By contributing original code, you agree to license that contribution under the
project's MIT license. Upstream-derived patches must retain the applicable
third-party notices and must not be represented as wholly original MIT code.
