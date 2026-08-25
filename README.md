# oz-for-oss

Oz for OSS is a reusable open-source automation platform that lets a Warp-hosted Oz agent triage issues, draft product and tech specs, open implementation PRs, review pull requests, respond to PR comments, and verify changes via slash commands. The intelligence lives in the agent skills under [`.agents/skills/`](.agents/skills/) and the prompt-construction layer that feeds them concrete repository context — everything else is delivery wiring around those skills.

Agent-backed work runs through a Vercel-hosted webhook control plane (`api/`, `core/`, `tests/`, `vercel.json`). The only GitHub Actions workflow kept in this repository is CI in [`.github/workflows/run-tests.yml`](.github/workflows/run-tests.yml); bot delivery no longer depends on reusable Actions wrappers under `.github`.

## Documentation

- [Platform overview](docs/platform.md) — agent roles, prompt construction, and how skills back each workflow.
- [Architecture](docs/architecture.md) — repository layout and the end-to-end webhook flow.
- [Onboarding](docs/onboarding.md) — install the GitHub App and deploy the Vercel control plane.
- [Contributing](CONTRIBUTING.md) — issue/PR workflow, label conventions, and local development.

## Automate development with Warp Factories

The agentic workflows this repo runs for issue triage, spec drafting, PR review, and implementation are one instance of a broader idea: [Warp Factories](https://www.warp.dev/factories), open infrastructure for cloud software factories — factories as code, on any model or harness, with evals, benchmarks, and self-improvement built in. Set up your first factory in about 5 minutes and build it out over time.

[Request early access](https://www.warp.dev/factories/request-access), or read more in the [Warp Factories docs](https://docs.warp.dev/factories).
