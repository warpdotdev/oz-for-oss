# Dockerfile for the triage agent isolation container.
#
# This container hosts the `triage-issue` workflow. It runs the bundled
# `oz` CLI against a read-only mount of the consuming repository and a
# writable `/mnt/output` volume. It does NOT have access to:
#   - GitHub credentials (the Python driver on the host makes all
#     GitHub-facing API calls)
#   - any repo state other than the mounted checkout
#   - private files or secrets beyond what the host explicitly mounts
#
# Expected read-only volume mounts, provided by the workflow at runtime:
#   /mnt/repo          - The consuming repository's checkout. Used as the
#                        agent's working directory so it can read source
#                        and companion skills at
#                        `.agents/skills/<agent>-local/SKILL.md`.
#
# Expected read-write volume mount:
#   /mnt/output        - Directory the agent writes its result JSON to
#                        (e.g. `triage_result.json` or
#                        `issue_response.json`). The host reads the
#                        expected file from this directory after the
#                        container exits.
#
# Expected environment variables:
#   WARP_API_KEY       - Required. API key for the Warp backend.
#   WARP_API_BASE_URL  - Optional. Override the default API base URL.
#
# The image is built locally by the consuming workflow and is not pushed
# to a registry.
#
# Invoked by run_agent_in_docker() in .github/scripts/oz_workflows/docker_agent.py:
#
#   docker run --rm \
#     -e WARP_API_KEY \
#     -e WARP_API_BASE_URL \
#     -v "$REPO_DIR:/mnt/repo:ro" \
#     -v "$OUTPUT_DIR:/mnt/output" \
#     oz-for-oss-triage \
#     agent run \
#       --skill triage-issue \
#       --cwd /mnt/repo \
#       --prompt "<prompt>" \
#       --output-format json \
#       --share

FROM warpdotdev/warp-agent:latest

# Install jq so the agent can validate the result JSON it writes to
# /mnt/output. The triage-issue skill explicitly instructs the agent to
# run `jq` against the produced file; the base warp-agent image does not
# ship jq, so we install it here. Switch back to the non-root
# `warp-agent` user after the install completes to keep the runtime
# user unchanged from the base image.
USER root
RUN apt-get update -qq \
    && apt-get install -y -qq --no-install-recommends jq \
    && rm -rf /var/lib/apt/lists/*
USER warp-agent

# Bake in both skills the triage prompt references at runtime so the
# container never has to fetch them. These skills express the stable
# cross-repo contract; consuming-repo overrides live in the mounted
# `/mnt/repo/.agents/skills/<agent>-local/SKILL.md` and are referenced
# from the prompt.
COPY .agents/skills/triage-issue/SKILL.md /home/warp-agent/.agents/skills/triage-issue/SKILL.md
COPY .agents/skills/dedupe-issue/SKILL.md /home/warp-agent/.agents/skills/dedupe-issue/SKILL.md

# Use a minimal entrypoint that skips git/gh setup. Triage does not
# mutate the repo or call GitHub from inside the container.
COPY docker/triage/entrypoint.sh /triage-entrypoint.sh
ENTRYPOINT ["/triage-entrypoint.sh"]
