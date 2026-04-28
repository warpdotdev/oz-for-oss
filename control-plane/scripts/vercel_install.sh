#!/usr/bin/env bash
# Local sync helper for the oz-for-oss control plane.
#
# The control plane reuses the ``oz_workflows`` package and the four
# PR-flow entrypoints from ``.github/scripts/``. The mirrored copies
# live at ``control-plane/lib/oz_workflows/`` and
# ``control-plane/lib/scripts/`` and ARE checked into the branch so
# Vercel ships them as part of the function bundle without needing to
# clone the rest of the repo at build time.
#
# Run this script after editing anything under ``.github/scripts/`` to
# refresh the mirrored copies, then commit the diff. CI does not run
# this script; Vercel does not run this script. It exists purely as a
# convenience for local development so contributors can keep the
# vendored copy in sync with the canonical source.
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

echo "vercel_install.sh: sync complete"
echo "Don't forget to commit any changes under control-plane/lib/."
