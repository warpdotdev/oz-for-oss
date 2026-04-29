"""Pytest path bootstrap.

The Vercel runtime sets ``PYTHONPATH=.`` (configured in ``vercel.json``)
so the entrypoints in ``api/`` can ``from lib.signatures import ...``.
The test runner needs the same path on ``sys.path``; doing it here
keeps the unittest invocation stdlib-only — no editable install or
package metadata required.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
