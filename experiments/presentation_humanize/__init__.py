"""Offline, fact-locked Chinese presentation experiment.

The package stays outside ``src/astock`` and deliberately avoids importing the
harness at package import time. This keeps ``python -m ...harness`` deterministic
and prevents experiment code from becoming a production Response Gateway hook.
"""

__all__: list[str] = []
