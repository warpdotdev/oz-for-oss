#!/usr/bin/env bash
# Vercel install hook for the oz-for-oss control plane.
#
# Vercel's project root is ``control-plane/``, but the GitHub Actions
# entrypoints we want to reuse live under ``.github/scripts/``. Vercel does
# not ship those by default, so this script mirrors the package and the
# four PR entrypoints into ``control-plane/lib/`` before Vercel's build
# step runs. The mirrored copies are intentionally git-ignored so the
# repository keeps a single source of truth at ``.github/scripts/``.
#
# After this script runs:
#   - ``control-plane/lib/oz_workflows/`` is a copy of
#     ``.github/scripts/oz_workflows/``.
#   - ``control-plane/lib/scripts/<name>.py`` is a copy of each of the
#     four PR-flow entrypoints (``review_pr.py``,
#     ``respond_to_pr_comment.py``, ``verify_pr_comment.py``,
#     ``enforce_pr_issue_state.py``).
#
# ``vercel.json`` extends ``PYTHONPATH`` to ``".:lib"`` so the
# Vercel function code can ``from oz_workflows.oz_client import ...``
# and ``from scripts.review_pr import gather_review_context`` directly.
#
# The same script also installs the runtime Python deps via the
# Vercel-provided ``pip``. We invoke it from the project's
# ``installCommand`` so deps land before the Python builder snapshots
# the function bundle.

set -euo pipefail

# Vercel runs ``installCommand`` from the project root.
PROJECT_ROOT="$(pwd)"

# When running locally outside of Vercel the project root is
# ``control-plane/`` and the source tree lives one directory up. When
# running on Vercel the runner clones the whole repo so the source
# package is reachable by stepping up one level. Either way, resolve the
# repo root by walking up until we find ``.github/scripts``.
find_repo_root() {
    local dir="$PROJECT_ROOT"
    while [[ "$dir" != "/" ]]; do
        if [[ -d "$dir/.github/scripts/oz_workflows" ]]; then
            printf '%s' "$dir"
            return 0
        fi
        dir="$(dirname "$dir")"
    done
    return 1
}

if ! REPO_ROOT="$(find_repo_root)"; then
    echo "vercel_install.sh: could not locate repository root containing .github/scripts/oz_workflows" >&2
    exit 1
fi

SOURCE_PACKAGE="$REPO_ROOT/.github/scripts/oz_workflows"
SOURCE_SCRIPTS_DIR="$REPO_ROOT/.github/scripts"
TARGET_LIB_DIR="$PROJECT_ROOT/lib"
TARGET_PACKAGE_DIR="$TARGET_LIB_DIR/oz_workflows"
TARGET_SCRIPTS_DIR="$TARGET_LIB_DIR/scripts"

ENTRYPOINTS=(
    review_pr.py
    respond_to_pr_comment.py
    verify_pr_comment.py
    enforce_pr_issue_state.py
)

echo "vercel_install.sh: mirroring oz_workflows from $SOURCE_PACKAGE"
rm -rf "$TARGET_PACKAGE_DIR"
mkdir -p "$TARGET_PACKAGE_DIR"
cp -R "$SOURCE_PACKAGE/." "$TARGET_PACKAGE_DIR/"

mkdir -p "$TARGET_SCRIPTS_DIR"
# Mark ``lib/scripts/`` as a regular package so the function code can
# ``from scripts.<entrypoint> import ...``.
if [[ ! -f "$TARGET_SCRIPTS_DIR/__init__.py" ]]; then
    cat <<'PY' > "$TARGET_SCRIPTS_DIR/__init__.py"
"""Mirrored copies of the GitHub Actions entrypoints.

Vercel install hook (``scripts/vercel_install.sh``) populates this
directory by copying the four PR-flow entrypoints from
``.github/scripts/`` so the control plane can reuse their helpers
without GitHub Actions runtime context.
"""
PY
fi

for entrypoint in "${ENTRYPOINTS[@]}"; do
    src="$SOURCE_SCRIPTS_DIR/$entrypoint"
    dst="$TARGET_SCRIPTS_DIR/$entrypoint"
    if [[ ! -f "$src" ]]; then
        echo "vercel_install.sh: source entrypoint $src is missing" >&2
        exit 1
    fi
    echo "vercel_install.sh: mirroring $entrypoint"
    cp "$src" "$dst"
done

# Install the Python runtime dependencies. Vercel's Python builder
# normally runs ``pip install -r requirements.txt`` itself, but
# ``installCommand`` overrides the default so we have to do it here.
if command -v pip >/dev/null 2>&1; then
    echo "vercel_install.sh: installing Python deps via pip"
    pip install --upgrade --quiet -r "$PROJECT_ROOT/requirements.txt"
elif command -v pip3 >/dev/null 2>&1; then
    echo "vercel_install.sh: installing Python deps via pip3"
    pip3 install --upgrade --quiet -r "$PROJECT_ROOT/requirements.txt"
else
    echo "vercel_install.sh: pip not available on PATH; skipping dependency install" >&2
fi

echo "vercel_install.sh: install hook complete"
