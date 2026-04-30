"""Pytest path bootstrap.

The Vercel runtime sets ``PYTHONPATH=.`` (configured in ``vercel.json``)
so the entrypoints in ``api/`` can ``from core.signatures import ...``.
The test runner needs the same path on ``sys.path``; doing it here
keeps the unittest invocation stdlib-only — no editable install or
package metadata required.

We also add ``core/`` so the bundled workflow package that the
cron handlers import lazily as
``workflows.<workflow>``) resolves the same way the Vercel runtime
resolves them at runtime via the ``PYTHONPATH=core`` mirror in
``vercel.json``.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
CORE_ROOT = REPO_ROOT / "core"
if str(CORE_ROOT) not in sys.path:
    sys.path.insert(0, str(CORE_ROOT))
