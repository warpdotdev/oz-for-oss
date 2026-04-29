"""Pytest path bootstrap.

The Vercel runtime sets ``PYTHONPATH=.`` (configured in ``vercel.json``)
so the entrypoints in ``api/`` can ``from lib.signatures import ...``.
The test runner needs the same path on ``sys.path``; doing it here
keeps the unittest invocation stdlib-only — no editable install or
package metadata required.

We also add ``lib/`` so the bundled ``oz_workflows`` package (and the
``scripts`` package the cron handlers import lazily as
``scripts.<workflow>``) resolves the same way the Vercel runtime
resolves them at runtime via the ``PYTHONPATH=lib`` mirror in
``vercel.json``.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
LIB_ROOT = REPO_ROOT / "lib"
if str(LIB_ROOT) not in sys.path:
    sys.path.insert(0, str(LIB_ROOT))
