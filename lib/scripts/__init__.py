"""Workflow-specific helpers used by the webhook control plane.

Each module owns one workflow's context gathering, prompt construction,
and GitHub result-application logic. The Vercel webhook builders and
cron handlers import these helpers directly; they are no longer mirrored
from separate workflow entrypoints.
"""
