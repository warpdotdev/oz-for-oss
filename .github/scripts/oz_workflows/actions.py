from __future__ import annotations

import os
import uuid


def _append_multiline(path: str, name: str, value: str) -> None:
    """Append a multiline output or environment entry using a unique delimiter."""
    delimiter = f"oz_{uuid.uuid4()}"
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"{name}<<{delimiter}\n{value}\n{delimiter}\n")


def set_output(name: str, value: str) -> None:
    """Publish a GitHub Actions step output when the workflow provides a sink."""
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        _append_multiline(output_path, name, value)


def append_summary(text: str) -> None:
    """Append text to the GitHub Actions step summary, preserving line endings."""
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return

    with open(summary_path, "a", encoding="utf-8") as handle:
        handle.write(text)
        if not text.endswith("\n"):
            handle.write("\n")


def notice(message: str) -> None:
    """Emit a GitHub Actions notice annotation."""
    print(f"::notice::{message}")


def warning(message: str) -> None:
    """Emit a GitHub Actions warning annotation."""
    print(f"::warning::{message}")


def error(message: str) -> None:
    """Emit a GitHub Actions error annotation."""
    print(f"::error::{message}")
