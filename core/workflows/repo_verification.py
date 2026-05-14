from __future__ import annotations


def format_repo_scoped_verification_section(
    *,
    target_repo_full_name: str,
    target_ref: str,
    target_sha: str = "",
    output_artifact: str,
    output_summary_location: str,
) -> str:
    """Return prompt text requiring verification against an explicit repo/ref target."""
    target_repo = target_repo_full_name.strip() or "the target repository"
    ref = target_ref.strip() or "the target ref"
    sha = target_sha.strip()
    lines = [
        "Repository-Scoped Verification (required before final output):",
        "- Verify against this target, not whatever repository happens to be checked out locally:",
        f"  - Target repository: `{target_repo}`",
        f"  - Target ref/branch: `{ref}`",
    ]
    if sha:
        lines.append(f"  - Target commit SHA: `{sha}`")
    lines.extend(
        [
            f"  - Final artifact that must include verification status: `{output_artifact}`",
            f"- Before producing or uploading `{output_artifact}`, make sure your working tree is the target repository/ref above. If the current checkout is missing, wrong, or stale, fetch or clone `https://github.com/{target_repo}.git` and check out the target ref or SHA before validating.",
            "- Detect the most appropriate build, test, lint, format, or sanity commands from the target repository's own files and documentation. Prefer repository-defined scripts or workflows over invented commands, and do not hard-code behavior for any one repository name.",
            "- Run the selected checks against the target repository state after any generated changes have been applied. If no reliable checks can be inferred, do not present the result as fully verified; explicitly report that verification could not be performed and why.",
            f"- Record the verification target, all commands attempted, and each pass/fail/skipped status in {output_summary_location}. Failed or unavailable checks must be visible in the final Oz output.",
        ]
    )
    return "\n".join(lines)
