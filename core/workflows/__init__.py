from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from oz.agent_workflow import (
    ProgressCommentSpec,
    WorkflowDispatch,
    make_run_adapter,
)

from oz.helpers import ENFORCEMENT_COMMENT_RUN_ID

from core.routing import (
    MAX_DAILY_REVIEW_INVOCATIONS,
    WORKFLOW_CREATE_IMPLEMENTATION_FROM_ISSUE,
    WORKFLOW_CREATE_SPEC_FROM_ISSUE,
    WORKFLOW_PLAN_APPROVED,
    WORKFLOW_RESPOND_TO_PR_COMMENT,
    WORKFLOW_REVIEW_PR,
    WORKFLOW_TRIAGE_NEW_ISSUES,
    WORKFLOW_VERIFY_PR_COMMENT,
    has_review_command,
)
from core.state import RunState
from core.workflow_adapters import reconstruct_progress

logger = logging.getLogger(__name__)


def _resolve_owner_repo(payload: Mapping[str, Any]) -> tuple[str, str, str]:
    repo_obj = payload.get("repository") or {}
    if not isinstance(repo_obj, dict):
        raise ValueError("payload.repository is missing or not an object")
    full_name = str(repo_obj.get("full_name") or "").strip()
    if "/" not in full_name:
        raise ValueError(
            f"payload.repository.full_name {full_name!r} is not an 'owner/repo' slug"
        )
    owner, repo = full_name.split("/", 1)
    return owner, repo, full_name


def _resolve_installation_id(payload: Mapping[str, Any]) -> int:
    installation = payload.get("installation") or {}
    if not isinstance(installation, dict):
        raise ValueError("payload.installation is missing or not an object")
    raw = installation.get("id")
    try:
        installation_id = int(raw or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"payload.installation.id is not an int: {raw!r}") from exc
    if installation_id <= 0:
        raise ValueError("payload.installation.id must be a positive integer")
    return installation_id


def _resolve_pr_number(payload: Mapping[str, Any]) -> int:
    pr = payload.get("pull_request")
    if isinstance(pr, dict) and pr.get("number") is not None:
        return int(pr["number"])
    issue = payload.get("issue")
    if isinstance(issue, dict) and issue.get("number") is not None:
        return int(issue["number"])
    raise ValueError("payload does not include a PR or issue number")


def _resolve_issue_number(payload: Mapping[str, Any]) -> int:
    issue = payload.get("issue")
    if isinstance(issue, dict) and issue.get("number") is not None:
        return int(issue["number"])
    raise ValueError("payload does not include an issue number")


def _resolve_linked_issue_numbers(payload: Mapping[str, Any]) -> list[int]:
    raw = payload.get("linked_issue_number")
    try:
        number = int(raw or 0)
    except (TypeError, ValueError):
        return []
    if number <= 0:
        return []
    return [number]


def _resolve_requester(payload: Mapping[str, Any]) -> str:
    comment = payload.get("comment")
    if isinstance(comment, dict):
        login = (comment.get("user") or {}).get("login")
        if isinstance(login, str) and login.strip():
            return login.strip()
    review = payload.get("review")
    if isinstance(review, dict):
        login = (review.get("user") or {}).get("login")
        if isinstance(login, str) and login.strip():
            return login.strip()
    sender = payload.get("sender")
    if isinstance(sender, dict):
        login = sender.get("login")
        if isinstance(login, str) and login.strip():
            return login.strip()
    return ""


def _resolve_trigger_source(payload: Mapping[str, Any], event_hint: str | None = None) -> str:
    if event_hint:
        return event_hint
    if isinstance(payload.get("comment"), dict):
        if isinstance(payload.get("pull_request"), dict):
            return "pull_request_review_comment"
        return "issue_comment"
    if isinstance(payload.get("review"), dict):
        return "pull_request_review"
    if isinstance(payload.get("pull_request"), dict):
        return "pull_request"
    return ""


def _resolve_trigger_kind(payload: Mapping[str, Any]) -> str:
    if isinstance(payload.get("comment"), dict) and isinstance(payload.get("pull_request"), dict):
        return "review"
    if isinstance(payload.get("review"), dict):
        return "review_body"
    return "conversation"


def _resolve_trigger_comment_id(payload: Mapping[str, Any]) -> int:
    comment = payload.get("comment")
    if isinstance(comment, dict):
        return int(comment.get("id") or 0)
    review = payload.get("review")
    if isinstance(review, dict):
        return int(review.get("id") or 0)
    return 0


def _resolve_review_reply_target(payload: Mapping[str, Any], pr: Any) -> tuple[Any, int] | None:
    if isinstance(payload.get("comment"), dict) and isinstance(payload.get("pull_request"), dict):
        comment_id = int(payload["comment"].get("id") or 0)
        if comment_id > 0:
            return (pr, comment_id)
    return None


def _get_field(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _is_automation_user(user: Any) -> bool:
    user_type = str(_get_field(user, "type", "") or "").strip().lower()
    if user_type == "bot":
        return True
    login = str(_get_field(user, "login", "") or "").strip().lower()
    return bool(login) and login.endswith("[bot]")


def _explicit_review_invocations_in_window(
    pr: Any,
) -> tuple[int, datetime | None]:
    """Count Oz review progress comments on *pr* within the rolling 24-hour window.

    Each dispatched /warp-agent-review run produces one bot-authored issue comment
    carrying the ``review-pull-request`` workflow metadata. Progress comments
    are the unit of counting; enforcement-only comments (identified by the
    ``pr-issue-state-enforcement`` run_id) are excluded because no review run
    was dispatched for them.

    Returns ``(count, oldest)`` where *oldest* is the ``created_at`` of the
    earliest in-window progress comment, used to compute the retry message.
    The current invocation has not yet been dispatched, so *count* reflects
    only prior completed runs. Callers must therefore use ``>= MAX`` to block
    and ``== MAX - 1`` to emit the advisory.

    Comments without a ``created_at`` are counted conservatively but do not
    contribute to *oldest*, so the retry duration may be omitted when
    timestamps are unavailable.
    """
    window_start = datetime.now(timezone.utc) - timedelta(hours=24)
    review_workflow_marker = f'"workflow":"{WORKFLOW_REVIEW_PR}"'
    enforcement_marker = f'"run_id":"{ENFORCEMENT_COMMENT_RUN_ID}"'
    count = 0
    oldest: datetime | None = None
    for comment in list(pr.get_issue_comments()):
        if not _is_automation_user(_get_field(comment, "user")):
            continue
        body = str(_get_field(comment, "body", "") or "")
        if review_workflow_marker not in body:
            continue
        if enforcement_marker in body:
            continue
        created_at = _get_field(comment, "created_at")
        if created_at is None:
            # No timestamp: count conservatively without updating oldest.
            count += 1
            continue
        # Normalise to UTC-aware for comparison.
        if getattr(created_at, "tzinfo", None) is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        if created_at >= window_start:
            count += 1
            if oldest is None or created_at < oldest:
                oldest = created_at
    return count, oldest


def _format_retry_duration(oldest_invocation: datetime) -> str:
    """Return a human-readable duration until the oldest invocation ages out."""
    reset_at = oldest_invocation + timedelta(hours=24)
    remaining = reset_at - datetime.now(timezone.utc)
    total_seconds = max(0, int(remaining.total_seconds()))
    hours, remainder = divmod(total_seconds, 3600)
    minutes = remainder // 60
    if hours > 0:
        return f"~{hours}h {minutes}m"
    if minutes > 0:
        return f"~{minutes}m"
    return "shortly"


def _is_explicit_review_invocation(payload: Mapping[str, Any]) -> bool:
    comment = payload.get("comment")
    if not isinstance(comment, dict):
        return False
    return has_review_command(str(comment.get("body") or ""))


class BaseWorkflow:
    workflow: str
    config_name: str

    def load_artifact(self, run_id: str) -> dict[str, Any]:
        return {}

    def progress_for_state(self, repo_handle: Any, *, state: RunState) -> Any:
        return reconstruct_progress(repo_handle, state=state, workflow=self.workflow)

    def run_adapter_for_state(self, *, state: RunState, progress: Any, run: Any | None = None) -> Any:
        return make_run_adapter(state=state, progress=progress, run=run)


class ReviewWorkflow(BaseWorkflow):
    workflow = WORKFLOW_REVIEW_PR
    config_name = WORKFLOW_REVIEW_PR

    def build_dispatch(
        self,
        payload: Mapping[str, Any],
        *,
        github_client: Any,
        workspace_path: Path | None = None,
    ) -> WorkflowDispatch | None:
        from oz.helpers import format_review_start_line  # type: ignore[import-not-found]
        from oz.ownership import OWNERSHIP_REPO  # type: ignore[import-not-found]
        from workflows import review_pr as review_workflow  # type: ignore[import-not-found]

        owner, repo, full_name = _resolve_owner_repo(payload)
        pr_number = _resolve_pr_number(payload)
        requester = _resolve_requester(payload)
        trigger_source = _resolve_trigger_source(payload)
        repo_handle = github_client.get_repo(full_name)
        pr = repo_handle.get_pull(pr_number)
        if _is_explicit_review_invocation(payload):
            try:
                invocation_count, oldest_invocation = _explicit_review_invocations_in_window(pr)
            except Exception:
                # Fail open: if the throttle lookup itself fails for any
                # reason (transient API error, permissions issue, etc.) we
                # still honor the request rather than silently dropping a
                # legitimate review trigger.
                logger.exception(
                    "Failed to count explicit /warp-agent-review invocations for %s PR #%s; allowing review",
                    full_name,
                    pr_number,
                )
                invocation_count = 0
                oldest_invocation = None
            retry_suffix = (
                f" Your next slot opens in {_format_retry_duration(oldest_invocation)}."
                if oldest_invocation is not None
                else ""
            )
            if invocation_count >= MAX_DAILY_REVIEW_INVOCATIONS:
                logger.info(
                    "Skipping /warp-agent-review for %s PR #%s: %s prior runs in window meets/exceeds daily limit %s",
                    full_name,
                    pr_number,
                    invocation_count,
                    MAX_DAILY_REVIEW_INVOCATIONS,
                )
                try:
                    repo_handle.get_issue(pr_number).create_comment(
                        f"You've used all {MAX_DAILY_REVIEW_INVOCATIONS} `/warp-agent-review` slots "
                        f"for the current 24-hour window.{retry_suffix}"
                    )
                except Exception:
                    logger.exception(
                        "Failed to post review-limit comment for %s PR #%s",
                        full_name,
                        pr_number,
                    )
                return None
            if invocation_count == MAX_DAILY_REVIEW_INVOCATIONS - 1:
                try:
                    repo_handle.get_issue(pr_number).create_comment(
                        f"This is your last `/warp-agent-review` for the current 24-hour window.{retry_suffix}"
                    )
                except Exception:
                    logger.exception(
                        "Failed to post review advisory comment for %s PR #%s",
                        full_name,
                        pr_number,
                    )
        if not review_workflow.enforce_pr_issue_state_for_review(
            repo_handle,
            owner=owner,
            repo=repo,
            pr=pr,
            requester=requester,
            explicit_issue_numbers=_resolve_linked_issue_numbers(payload),
        ):
            return None
        # Resolve the ownership-areas repo handle through the same
        # payload-keyed ``github_client``. When the org-wide install on
        # ``warpdotdev`` covers both the consuming repo and
        # ``warp-ownership``, one token reads both. The slug is hardcoded
        # to ``warpdotdev/warp-ownership`` since this integration is
        # scoped to that repo. Failures here are logged and treated as
        # "ownership areas unavailable" so the review still dispatches
        # against the legacy STAKEHOLDERS prompt for consumers outside
        # the warpdotdev org or installs without access.
        ownership_repo_handle: Any | None = None
        try:
            ownership_repo_handle = github_client.get_repo(OWNERSHIP_REPO)
        except Exception:
            logger.exception(
                "review-pr: failed to resolve ownership-areas repo %s; "
                "falling back to STAKEHOLDERS",
                OWNERSHIP_REPO,
            )
            ownership_repo_handle = None
        context = review_workflow.gather_review_context(
            repo_handle,
            owner=owner,
            repo=repo,
            pr_number=pr_number,
            trigger_source=trigger_source,
            requester=requester,
            workspace_path=workspace_path or Path("/tmp"),
            ownership_repo_handle=ownership_repo_handle,
        )
        return WorkflowDispatch(
            workflow=self.workflow,
            repo=full_name,
            installation_id=_resolve_installation_id(payload),
            config_name=self.config_name,
            title=f"PR review #{pr_number}",
            skill_name=context["skill_name"],
            prompt=review_workflow.build_review_prompt_for_dispatch(context),
            payload_subset=getattr(
                review_workflow,
                "review_payload_subset",
                lambda ctx: dict(ctx),
            )(context),
            progress=ProgressCommentSpec(
                repo_handle=repo_handle,
                owner=owner,
                repo=repo,
                issue_number=pr_number,
                workflow=self.workflow,
                start_line=format_review_start_line(
                    spec_only=bool(context.get("spec_only")),
                    is_rereview=trigger_source in {"issue_comment", "pull_request_review_comment"},
                ),
                requester_login=requester,
                event_payload=payload,
            ),
            attachments=getattr(
                review_workflow,
                "review_context_attachments",
                lambda ctx: [],
            )(context),
        )

    def load_artifact(self, run_id: str) -> dict[str, Any]:
        from oz.artifacts import load_review_artifact  # type: ignore[import-not-found]

        return load_review_artifact(run_id)

    def apply_result(self, repo_handle: Any, *, context: Mapping[str, Any], run: Any, result: Mapping[str, Any], progress: Any, github_client: Any | None = None) -> None:
        from workflows.review_pr import apply_review_result  # type: ignore[import-not-found]

        apply_review_result(repo_handle, context=context, run=run, result=dict(result), progress=progress)


class RespondWorkflow(BaseWorkflow):
    workflow = WORKFLOW_RESPOND_TO_PR_COMMENT
    config_name = WORKFLOW_RESPOND_TO_PR_COMMENT

    def _should_dispatch_run(self, context: Mapping[str, Any]) -> bool:
        if context.get("branch_strategy") == "blocked":
            return False
        if (
            bool(context.get("is_cross_repository"))
            and not bool(context.get("trigger_actor_is_trusted"))
        ):
            return False
        if (
            not bool(context.get("is_cross_repository"))
            and context.get("can_push_to_head_branch") is False
        ):
            return False
        return True

    def build_dispatch(
        self,
        payload: Mapping[str, Any],
        *,
        github_client: Any,
        workspace_path: Path | None = None,
    ) -> WorkflowDispatch | None:
        from workflows import respond_to_pr_comment as respond_workflow  # type: ignore[import-not-found]

        owner, repo, full_name = _resolve_owner_repo(payload)
        pr_number = _resolve_pr_number(payload)
        requester = _resolve_requester(payload)
        trigger_kind = _resolve_trigger_kind(payload)
        trigger_comment_id = _resolve_trigger_comment_id(payload)
        repo_handle = github_client.get_repo(full_name)
        pr = repo_handle.get_pull(pr_number)
        review_reply_target = _resolve_review_reply_target(payload, pr)
        context = respond_workflow.gather_pr_comment_context(
            repo_handle,
            owner=owner,
            repo=repo,
            pr_number=pr_number,
            trigger_kind=trigger_kind,
            trigger_comment_id=trigger_comment_id,
            requester=requester,
            event=dict(payload),
            review_reply_target=review_reply_target,
            workspace_path=workspace_path or Path("/tmp"),
            client=github_client,
            pr=pr,
        )
        if not self._should_dispatch_run(context):
            return None
        return WorkflowDispatch(
            workflow=self.workflow,
            repo=full_name,
            installation_id=_resolve_installation_id(payload),
            config_name=self.config_name,
            title=f"Respond to PR comment #{pr_number}",
            skill_name="implement-issue",
            prompt=respond_workflow.build_pr_comment_prompt(context),
            payload_subset=getattr(
                respond_workflow,
                "pr_comment_payload_subset",
                lambda ctx: dict(ctx),
            )(context),
            progress=ProgressCommentSpec(
                repo_handle=repo_handle,
                owner=owner,
                repo=repo,
                issue_number=pr_number,
                workflow=self.workflow,
                start_line=str(context.get("progress_start_line") or ""),
                requester_login=requester,
                event_payload=payload,
                review_reply_target=review_reply_target,
            ),
            attachments=getattr(
                respond_workflow,
                "pr_comment_context_attachments",
                lambda ctx: [],
            )(context),
        )

    def _review_reply_target_for_state(self, state: RunState, repo_handle: Any) -> tuple[Any, int] | None:
        payload = state.payload_subset or {}
        review_reply_target_id = int(payload.get("review_reply_target_id") or 0)
        if review_reply_target_id <= 0:
            return None
        pr_number = int(payload.get("pr_number") or 0)
        if pr_number <= 0:
            return None
        return (repo_handle.get_pull(pr_number), review_reply_target_id)

    def progress_for_state(self, repo_handle: Any, *, state: RunState) -> Any:
        return reconstruct_progress(
            repo_handle,
            state=state,
            workflow=self.workflow,
            review_reply_target=self._review_reply_target_for_state(state, repo_handle),
        )

    def apply_result(self, repo_handle: Any, *, context: Mapping[str, Any], run: Any, result: Mapping[str, Any], progress: Any, github_client: Any | None = None) -> None:
        from workflows.respond_to_pr_comment import apply_pr_comment_result  # type: ignore[import-not-found]

        apply_pr_comment_result(
            repo_handle,
            context=context,
            run=run,
            client=github_client,
            progress=progress,
        )


class VerifyWorkflow(BaseWorkflow):
    workflow = WORKFLOW_VERIFY_PR_COMMENT
    config_name = WORKFLOW_VERIFY_PR_COMMENT

    def build_dispatch(
        self,
        payload: Mapping[str, Any],
        *,
        github_client: Any,
        workspace_path: Path | None = None,
    ) -> WorkflowDispatch:
        from workflows import verify_pr_comment as verify_workflow  # type: ignore[import-not-found]

        owner, repo, full_name = _resolve_owner_repo(payload)
        pr_number = _resolve_pr_number(payload)
        requester = _resolve_requester(payload)
        trigger_comment_id = _resolve_trigger_comment_id(payload)
        repo_handle = github_client.get_repo(full_name)
        context = verify_workflow.gather_verify_context(
            repo_handle,
            owner=owner,
            repo=repo,
            pr_number=pr_number,
            trigger_comment_id=trigger_comment_id,
            requester=requester,
            workspace_path=workspace_path or Path("/tmp"),
        )
        prompt = verify_workflow.build_verification_prompt(
            owner=context["owner"],
            repo=context["repo"],
            pr_number=context["pr_number"],
            base_branch=context["base_branch"],
            head_branch=context["head_branch"],
            trigger_comment_id=context["trigger_comment_id"],
            requester=context["requester"],
            verification_skills_text=context["verification_skills_text"],
        )
        return WorkflowDispatch(
            workflow=self.workflow,
            repo=full_name,
            installation_id=_resolve_installation_id(payload),
            config_name=self.config_name,
            title=f"Verify PR #{pr_number}",
            skill_name="verify-pr",
            prompt=prompt,
            payload_subset=getattr(
                verify_workflow,
                "verify_payload_subset",
                lambda ctx: dict(ctx),
            )(context),
            progress=ProgressCommentSpec(
                repo_handle=repo_handle,
                owner=owner,
                repo=repo,
                issue_number=pr_number,
                workflow=self.workflow,
                start_line="I'm running `/oz-verify` for this pull request using the repository's verification-enabled skills.",
                requester_login=requester,
                event_payload=payload,
            ),
            attachments=getattr(
                verify_workflow,
                "verify_context_attachments",
                lambda ctx: [],
            )(context),
        )

    def load_artifact(self, run_id: str) -> dict[str, Any]:
        from oz.artifacts import load_run_artifact  # type: ignore[import-not-found]
        from workflows.verify_pr_comment import VERIFICATION_REPORT_FILENAME  # type: ignore[import-not-found]

        return load_run_artifact(run_id, filename=VERIFICATION_REPORT_FILENAME)

    def apply_result(self, repo_handle: Any, *, context: Mapping[str, Any], run: Any, result: Mapping[str, Any], progress: Any, github_client: Any | None = None) -> None:
        from oz.verification import list_downloadable_verification_artifacts  # type: ignore[import-not-found]
        from workflows.verify_pr_comment import VERIFICATION_REPORT_FILENAME, apply_verification_result  # type: ignore[import-not-found]

        apply_verification_result(
            repo_handle,
            context=context,
            run=run,
            result=dict(result),
            artifacts=list_downloadable_verification_artifacts(
                run,
                exclude_filenames={VERIFICATION_REPORT_FILENAME},
            ),
            progress=progress,
        )


class TriageWorkflow(BaseWorkflow):
    workflow = WORKFLOW_TRIAGE_NEW_ISSUES
    config_name = WORKFLOW_TRIAGE_NEW_ISSUES

    def build_dispatch(
        self,
        payload: Mapping[str, Any],
        *,
        github_client: Any,
        workspace_path: Path | None = None,
    ) -> WorkflowDispatch:
        from oz.helpers import format_triage_start_line, triggering_comment_prompt_text  # type: ignore[import-not-found]
        from workflows import triage_new_issues as triage_workflow  # type: ignore[import-not-found]

        owner, repo, full_name = _resolve_owner_repo(payload)
        issue_number = _resolve_issue_number(payload)
        requester = _resolve_requester(payload)
        trigger_comment_id = _resolve_trigger_comment_id(payload)
        repo_handle = github_client.get_repo(full_name)
        context = triage_workflow.gather_triage_context(
            repo_handle,
            owner=owner,
            repo=repo,
            issue_number=issue_number,
            requester=requester,
            triggering_comment_id=trigger_comment_id,
            triggering_comment_text=triggering_comment_prompt_text(dict(payload)),
        )
        return WorkflowDispatch(
            workflow=self.workflow,
            repo=full_name,
            installation_id=_resolve_installation_id(payload),
            config_name=self.config_name,
            title=f"Triage issue #{issue_number}",
            skill_name="triage-issue",
            prompt=triage_workflow.build_triage_prompt_for_dispatch(context, repo_handle=repo_handle),
            payload_subset=getattr(
                triage_workflow,
                "triage_payload_subset",
                lambda ctx: dict(ctx),
            )(context),
            progress=ProgressCommentSpec(
                repo_handle=repo_handle,
                owner=owner,
                repo=repo,
                issue_number=issue_number,
                workflow=self.workflow,
                start_line=format_triage_start_line(is_retriage=bool(context.get("is_retriage"))),
                requester_login=requester,
                event_payload=payload,
            ),
            attachments=getattr(
                triage_workflow,
                "triage_context_attachments",
                lambda ctx: [],
            )(context),
        )

    def load_artifact(self, run_id: str) -> dict[str, Any]:
        from oz.artifacts import load_triage_artifact  # type: ignore[import-not-found]

        return load_triage_artifact(run_id)

    def apply_result(self, repo_handle: Any, *, context: Mapping[str, Any], run: Any, result: Mapping[str, Any], progress: Any, github_client: Any | None = None) -> None:
        from workflows.triage_new_issues import apply_triage_result_for_dispatch  # type: ignore[import-not-found]

        apply_triage_result_for_dispatch(
            repo_handle,
            context=context,
            run=run,
            result=dict(result),
            progress=progress,
        )


class CreateSpecWorkflow(BaseWorkflow):
    workflow = WORKFLOW_CREATE_SPEC_FROM_ISSUE
    config_name = WORKFLOW_CREATE_SPEC_FROM_ISSUE

    def build_dispatch(
        self,
        payload: Mapping[str, Any],
        *,
        github_client: Any,
        workspace_path: Path | None = None,
    ) -> WorkflowDispatch:
        from oz.helpers import triggering_comment_prompt_text  # type: ignore[import-not-found]
        from workflows import create_spec_from_issue as create_spec_workflow  # type: ignore[import-not-found]

        owner, repo, full_name = _resolve_owner_repo(payload)
        issue_number = _resolve_issue_number(payload)
        requester = _resolve_requester(payload)
        trigger_comment_id = _resolve_trigger_comment_id(payload)
        repo_handle = github_client.get_repo(full_name)
        context = create_spec_workflow.gather_create_spec_context(
            repo_handle,
            owner=owner,
            repo=repo,
            issue_number=issue_number,
            requester=requester,
            triggering_comment_id=trigger_comment_id,
            triggering_comment_text=triggering_comment_prompt_text(dict(payload)),
            event_payload=dict(payload),
            github_client=github_client,
        )
        return WorkflowDispatch(
            workflow=self.workflow,
            repo=full_name,
            installation_id=_resolve_installation_id(payload),
            config_name=self.config_name,
            title=f"Create specs for issue #{issue_number}",
            skill_name=create_spec_workflow.SPEC_DRIVEN_IMPLEMENTATION_SKILL,
            prompt=create_spec_workflow.build_create_spec_prompt_for_dispatch(context),
            payload_subset=getattr(
                create_spec_workflow,
                "create_spec_payload_subset",
                lambda ctx: dict(ctx),
            )(context),
            progress=ProgressCommentSpec(
                repo_handle=repo_handle,
                owner=owner,
                repo=repo,
                issue_number=issue_number,
                workflow=self.workflow,
                start_line=str(context.get("progress_start_line") or ""),
                requester_login=requester,
                event_payload=payload,
            ),
            attachments=getattr(
                create_spec_workflow,
                "create_spec_context_attachments",
                lambda ctx: [],
            )(context),
        )

    def apply_result(self, repo_handle: Any, *, context: Mapping[str, Any], run: Any, result: Mapping[str, Any], progress: Any, github_client: Any | None = None) -> None:
        from workflows.create_spec_from_issue import apply_create_spec_result  # type: ignore[import-not-found]

        apply_create_spec_result(repo_handle, context=context, run=run, progress=progress)


class CreateImplementationWorkflow(BaseWorkflow):
    workflow = WORKFLOW_CREATE_IMPLEMENTATION_FROM_ISSUE
    config_name = WORKFLOW_CREATE_IMPLEMENTATION_FROM_ISSUE

    def build_dispatch(
        self,
        payload: Mapping[str, Any],
        *,
        github_client: Any,
        workspace_path: Path | None = None,
    ) -> WorkflowDispatch:
        from oz.helpers import triggering_comment_prompt_text  # type: ignore[import-not-found]
        from workflows import create_implementation_from_issue as implementation_workflow  # type: ignore[import-not-found]

        owner, repo, full_name = _resolve_owner_repo(payload)
        issue_number = _resolve_issue_number(payload)
        requester = _resolve_requester(payload)
        repo_handle = github_client.get_repo(full_name)
        context = implementation_workflow.gather_create_implementation_context(
            repo_handle,
            owner=owner,
            repo=repo,
            issue_number=issue_number,
            requester=requester,
            triggering_comment_text=triggering_comment_prompt_text(dict(payload)),
            event_payload=dict(payload),
            workspace_path=workspace_path or Path("/tmp"),
            github_client=github_client,
        )
        return WorkflowDispatch(
            workflow=self.workflow,
            repo=full_name,
            installation_id=_resolve_installation_id(payload),
            config_name=self.config_name,
            title=f"Implement issue #{issue_number}",
            skill_name=implementation_workflow.IMPLEMENT_SPECS_SKILL,
            prompt=implementation_workflow.build_create_implementation_prompt_for_dispatch(context),
            payload_subset=getattr(
                implementation_workflow,
                "create_implementation_payload_subset",
                lambda ctx: dict(ctx),
            )(context),
            progress=ProgressCommentSpec(
                repo_handle=repo_handle,
                owner=owner,
                repo=repo,
                issue_number=issue_number,
                workflow=self.workflow,
                start_line=str(context.get("progress_start_line") or ""),
                requester_login=requester,
                event_payload=payload,
            ),
            attachments=getattr(
                implementation_workflow,
                "create_implementation_context_attachments",
                lambda ctx: [],
            )(context),
        )

    def apply_result(self, repo_handle: Any, *, context: Mapping[str, Any], run: Any, result: Mapping[str, Any], progress: Any, github_client: Any | None = None) -> None:
        from workflows.create_implementation_from_issue import apply_create_implementation_result  # type: ignore[import-not-found]

        apply_create_implementation_result(repo_handle, context=context, run=run, progress=progress)


class PlanApprovedWorkflow(CreateImplementationWorkflow):
    workflow = WORKFLOW_PLAN_APPROVED
    config_name = WORKFLOW_CREATE_IMPLEMENTATION_FROM_ISSUE

    def build_dispatch(
        self,
        payload: Mapping[str, Any],
        *,
        github_client: Any,
        workspace_path: Path | None = None,
    ) -> WorkflowDispatch:
        from oz.helpers import resolve_issue_number_for_pr  # type: ignore[import-not-found]
        from workflows import create_implementation_from_issue as implementation_workflow  # type: ignore[import-not-found]

        owner, repo, full_name = _resolve_owner_repo(payload)
        requester = _resolve_requester(payload)
        repo_handle = github_client.get_repo(full_name)
        issue_number = int(payload.get("linked_issue_number") or 0)
        if issue_number <= 0:
            pr_payload = payload.get("pull_request") or {}
            pr_number = int(pr_payload.get("number") or 0) if isinstance(pr_payload, dict) else 0
            if pr_number <= 0:
                raise ValueError("plan-approved payload is missing linked_issue_number and pr_number")
            pr_obj = repo_handle.get_pull(pr_number)
            changed_files = [str(f.filename) for f in list(pr_obj.get_files())]
            resolved = resolve_issue_number_for_pr(repo_handle, owner, repo, pr_obj, changed_files)
            if not resolved:
                raise ValueError(f"plan-approved PR #{pr_number} has no resolvable linked issue")
            issue_number = int(resolved)
        context = implementation_workflow.gather_create_implementation_context(
            repo_handle,
            owner=owner,
            repo=repo,
            issue_number=issue_number,
            requester=requester,
            triggering_comment_text="",
            event_payload=dict(payload),
            workspace_path=workspace_path or Path("/tmp"),
            github_client=github_client,
        )
        payload_subset = getattr(
            implementation_workflow,
            "create_implementation_payload_subset",
            lambda ctx: dict(ctx),
        )(context)
        payload_subset["trigger_source"] = "plan-approved"
        return WorkflowDispatch(
            workflow=self.workflow,
            repo=full_name,
            installation_id=_resolve_installation_id(payload),
            config_name=self.config_name,
            title=f"Implement issue #{issue_number} (plan-approved)",
            skill_name=implementation_workflow.IMPLEMENT_SPECS_SKILL,
            prompt=implementation_workflow.build_create_implementation_prompt_for_dispatch(context),
            payload_subset=payload_subset,
            progress=ProgressCommentSpec(
                repo_handle=repo_handle,
                owner=owner,
                repo=repo,
                issue_number=issue_number,
                workflow=WORKFLOW_CREATE_IMPLEMENTATION_FROM_ISSUE,
                start_line=str(context.get("progress_start_line") or ""),
                requester_login=requester,
                event_payload=payload,
            ),
            attachments=getattr(
                implementation_workflow,
                "create_implementation_context_attachments",
                lambda ctx: [],
            )(context),
        )

    def progress_for_state(self, repo_handle: Any, *, state: RunState) -> Any:
        return reconstruct_progress(
            repo_handle,
            state=state,
            workflow=WORKFLOW_CREATE_IMPLEMENTATION_FROM_ISSUE,
        )



def build_workflow_registry() -> dict[str, BaseWorkflow]:
    workflows: list[BaseWorkflow] = [
        ReviewWorkflow(),
        RespondWorkflow(),
        VerifyWorkflow(),
        TriageWorkflow(),
        CreateSpecWorkflow(),
        CreateImplementationWorkflow(),
        PlanApprovedWorkflow(),
    ]
    return {workflow.workflow: workflow for workflow in workflows}


__all__ = [
    "BaseWorkflow",
    "CreateImplementationWorkflow",
    "CreateSpecWorkflow",
    "PlanApprovedWorkflow",
    "RespondWorkflow",
    "ReviewWorkflow",
    "TriageWorkflow",
    "VerifyWorkflow",
    "build_workflow_registry",
]
