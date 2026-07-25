"""FastAPI web UI. Deliberately last (spec §11).

Nothing here yet, and nothing should be added until the CLI is real. The point of
choosing fastapi is that it reuses the pydantic models in config.py and state.py
directly, so this file stays thin — it should never grow its own notion of what an
instance or a mod version is.
"""

from __future__ import annotations
