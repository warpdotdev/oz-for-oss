# oz-for-oss

Oz for OSS is a reusable open-source automation platform that lets a Warp-hosted Oz agent triage issues, draft product and tech specs, open implementation PRs, review pull requests, respond to PR comments, and verify changes via slash commands. The intelligence lives in the agent skills under [`.agents/skills/`](.agents/skills/) and the prompt-construction layer that feeds them concrete repository context — everything else is delivery wiring around those skills.

Agent-backed work runs through a Vercel-hosted webhook control plane (`api/`, `core/`, `tests/`, `vercel.json`). The only GitHub Actions workflow kept in this repository is CI in [`.github/workflows/run-tests.yml`](.github/workflows/run-tests.yml); bot delivery no longer depends on reusable Actions wrappers under `.github`.

## Documentation

- [Platform overview](docs/platform.md) — agent roles, prompt construction, and how skills back each workflow.
- [Architecture](docs/architecture.md) — repository layout and the end-to-end webhook flow.
- [Onboarding](docs/onboarding.md) — install the GitHub App and deploy the Vercel control plane.
- [Contributing](CONTRIBUTING.md) — issue/PR workflow, label conventions, and local development.

## Have an open-source project?

Actively maintained open-source projects can apply for the [Oz Open Source Partnership](https://docs.warp.dev/support-and-community/community/open-source-partnership) to receive free Oz credits for using these workflows. Accepted projects can use Oz agents for tasks like issue triage, pull request review, documentation, and implementation support across their repositories.

To apply, [fill out the application form](https://tally.so/r/LZWxqG) and we'll be in touch.
