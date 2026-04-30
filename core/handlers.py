"""Cron-side handler registry for cloud-agent workflows."""

from __future__ import annotations

from typing import Mapping

from .poll_runs import WorkflowHandlers
from .routing import (
    WORKFLOW_CREATE_IMPLEMENTATION_FROM_ISSUE,
    WORKFLOW_CREATE_SPEC_FROM_ISSUE,
    WORKFLOW_PLAN_APPROVED,
    WORKFLOW_RESPOND_TO_PR_COMMENT,
    WORKFLOW_REVIEW_PR,
    WORKFLOW_TRIAGE_NEW_ISSUES,
    WORKFLOW_VERIFY_PR_COMMENT,
)
from .workflow_adapters import GithubClientFactory, handlers_for_workflow
from .workflows import (
    CreateImplementationWorkflow,
    CreateSpecWorkflow,
    PlanApprovedWorkflow,
    RespondWorkflow,
    ReviewWorkflow,
    TriageWorkflow,
    VerifyWorkflow,
    build_workflow_registry,
)


def build_review_handlers(github_client_factory: GithubClientFactory) -> WorkflowHandlers:
    return handlers_for_workflow(
        ReviewWorkflow(), github_client_factory=github_client_factory
    )


def build_respond_handlers(github_client_factory: GithubClientFactory) -> WorkflowHandlers:
    return handlers_for_workflow(
        RespondWorkflow(), github_client_factory=github_client_factory
    )


def build_verify_handlers(github_client_factory: GithubClientFactory) -> WorkflowHandlers:
    return handlers_for_workflow(
        VerifyWorkflow(), github_client_factory=github_client_factory
    )



def build_triage_handlers(github_client_factory: GithubClientFactory) -> WorkflowHandlers:
    return handlers_for_workflow(
        TriageWorkflow(), github_client_factory=github_client_factory
    )


def build_create_spec_handlers(github_client_factory: GithubClientFactory) -> WorkflowHandlers:
    return handlers_for_workflow(
        CreateSpecWorkflow(), github_client_factory=github_client_factory
    )


def build_create_implementation_handlers(
    github_client_factory: GithubClientFactory,
) -> WorkflowHandlers:
    return handlers_for_workflow(
        CreateImplementationWorkflow(), github_client_factory=github_client_factory
    )


def build_plan_approved_handlers(github_client_factory: GithubClientFactory) -> WorkflowHandlers:
    return handlers_for_workflow(
        PlanApprovedWorkflow(), github_client_factory=github_client_factory
    )


def build_handler_registry(
    *, github_client_factory: GithubClientFactory
) -> Mapping[str, WorkflowHandlers]:
    return {
        name: handlers_for_workflow(
            workflow,
            github_client_factory=github_client_factory,
        )
        for name, workflow in build_workflow_registry().items()
    }


__all__ = [
    "GithubClientFactory",
    "build_create_implementation_handlers",
    "build_create_spec_handlers",
    "build_handler_registry",
    "build_plan_approved_handlers",
    "build_respond_handlers",
    "build_review_handlers",
    "build_triage_handlers",
    "build_verify_handlers",
    "WORKFLOW_CREATE_IMPLEMENTATION_FROM_ISSUE",
    "WORKFLOW_CREATE_SPEC_FROM_ISSUE",
    "WORKFLOW_PLAN_APPROVED",
    "WORKFLOW_RESPOND_TO_PR_COMMENT",
    "WORKFLOW_REVIEW_PR",
    "WORKFLOW_TRIAGE_NEW_ISSUES",
    "WORKFLOW_VERIFY_PR_COMMENT",
]
