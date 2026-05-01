# Triage agent container

This directory holds the Docker image that runs the `triage-issue` skill
on behalf of the `triage-new-issues` and
`respond-to-triaged-issue-comment` GitHub Actions workflows.

The image replaces the previous "run inside a pre-defined Warp cloud
environment" pattern. Instead, it boots the bundled `oz` CLI inside a
purpose-built container that has:

- a read-only mount of the consuming repository at `/mnt/repo`
- a writable mount at `/mnt/output` for the result JSON
- no GitHub credentials (the Python driver on the host owns all GitHub
  mutations)
- no git or gh CLI setup

The pattern mirrors
[`warpdotdev/repo-sync/docker/pr-description`](https://github.com/warpdotdev/repo-sync/tree/main/docker/pr-description).

## What gets baked in

- `FROM warpdotdev/warp-agent:latest` — provides the `oz` CLI at
  `/opt/warpdotdev/oz-stable/oz`.
- `.agents/skills/triage-issue/SKILL.md` and
  `.agents/skills/dedupe-issue/SKILL.md` copied into
  `/home/warp-agent/.agents/skills/` so `oz agent run --skill triage-issue`
  finds them without needing the consuming repo to ship them.

Consuming-repo overrides (`triage-issue-local`, `dedupe-issue-local`)
remain in the mounted repo and are referenced from the prompt as
`/mnt/repo/.agents/skills/<agent>-local/SKILL.md`.

## Build locally

From the repository root:

```sh
docker build -f docker/triage/Dockerfile -t oz-for-oss-triage .
```

The GitHub Actions workflows build this image fresh on every run, so the
same command is what CI runs.

## Run against a live issue

Use `scripts/local_triage.py` to exercise the full triage flow against
any public issue without mutating GitHub:

```sh
export WARP_API_KEY=...
python scripts/local_triage.py \
    --repo warpdotdev/oz-for-oss \
    --issue 123 \
    --mode triage
```

The script:

1. Clones the target repo (or uses `--repo-dir`) so the container can
   read companion skills and source files.
2. Fetches the issue, its comments, and the context the workflow uses
   via the `gh` CLI.
3. Builds the same prompt the workflow builds (via shared helpers in
   `.github/scripts/triage_new_issues.py` and
   `.github/scripts/respond_to_triaged_issue_comment.py`).
4. Invokes `run_agent_in_docker(...)` against this image.
5. Prints the parsed `triage_result.json` (or `issue_response.json` when
   `--mode respond`) and the Warp session link.

See `scripts/local_triage.py --help` for the full flag list.

## Manual docker run

If you want to drive the container yourself, without the Python helper:

```sh
mkdir -p /tmp/triage-output
docker run --rm \
    -e WARP_API_KEY \
    -v "$PWD:/mnt/repo:ro" \
    -v "/tmp/triage-output:/mnt/output" \
    oz-for-oss-triage \
    agent run \
        --skill triage-issue \
        --cwd /mnt/repo \
        --prompt "Triage GitHub issue #N in repository owner/name ..." \
        --output-format json \
        --share
```

The agent writes its result to `/mnt/output/triage_result.json`. The
container never talks to GitHub on its own.
