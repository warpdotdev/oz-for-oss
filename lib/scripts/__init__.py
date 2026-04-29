"""Mirrored copies of the GitHub Actions entrypoints.

Vercel install hook (``scripts/vercel_install.sh``) populates this
directory by copying the four PR-flow entrypoints from
``.github/scripts/`` so the control plane can reuse their helpers
without GitHub Actions runtime context.
"""
