# Contributing

## How this repo decides when changes are accepted

The issue is where product alignment happens. Anyone can file one, and anyone can join the discussion. Once the problem is clear enough, the Warp team decides whether the next step is speccing or implementation.

That decision is expressed with issue labels:

- `ready-to-spec` means we agree on the problem but still want product and technical due diligence before code starts.
- `ready-to-implement` means the product shape and technical approach are already in good enough shape that someone can start writing code.

Those labels are the repo's way of saying when a change is open for contribution. They are not Oz-specific, and they do not mean only one person can work on something. They just tell contributors whether we are accepting a spec first or whether we are ready for code.

Other labels, such as automated triage labels for area or reproducibility, are informational only. They do not change whether an issue is ready for speccing or implementation.

## When to open a spec PR

Spec-only PRs (markdown-only changes) are accepted without requiring a matching `ready-to-spec` issue. Spec discussion is free-form and can start directly from a PR when that is the most useful place for the conversation.

In practice, that means:

- use the issue for product discussion first when a shared baseline is helpful
- the `ready-to-spec` label still signals that the Warp team wants product and technical due diligence on an issue before code starts, but it does not gate opening a spec PR
- open a PR with the product spec and tech spec whenever the spec material is ready to review
- use the PR as the place for product and technical discussion and iteration

For larger changes, the specs live in the PR and become the home for the back-and-forth. Once they are in good shape, the Warp team can approve them and the work can move into implementation.

## When to open a code PR

Code changes are accepted when they are tied to an issue that is marked `ready-to-implement`.

In practice, that means:

- use the issue to get the product discussion into a stable place
- wait until the Warp team marks the issue `ready-to-implement`
- open a PR with the implementation once the issue is in that state

For smaller changes, we can go straight from issue to code. For larger changes, we usually expect the spec step first and then implementation on that same PR or a linked follow-up PR.

## Who decides readiness

Contributors can file issues, comment on issues and PRs, and open PRs directly. The Warp team is still the group that decides whether an issue is ready for speccing or ready for implementation. Contributors should not treat discussion alone as approval to start a spec or code change if the readiness label is missing.

## A note on parallel work

Marking an issue as ready is not meant to lock it. It just means the repo is open for that next chunk of work. Someone can take a swing at it with Oz, another coding agent, or by hand. If multiple people explore the same issue, that is still normal open source behavior and we will select the best implementation through normal review.

## Local development

The webhook control plane (`api/`, `lib/`, `tests/`, `vercel.json`) is the delivery surface for PR-triggered flows; the GitHub Actions workflows under `.github/workflows/` still handle issue-triggered, plan-approval, and self-improvement flows. Both surfaces share the Python helpers in `lib/oz_workflows/` and `lib/scripts/`.

### Set up the Python env

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

### Run the test suites

```sh
# Webhook + dispatcher tests
python -m pytest tests

# Shared helper + GitHub Actions entrypoint tests
PYTHONPATH=lib:.github/scripts python -m unittest discover -s .github/scripts/tests
```

`run-tests.yml` runs both suites on every pull request.

### Run the webhook locally

```sh
vercel dev
```

`vercel dev` boots the same Python entrypoints (`api/webhook.py`, `api/cron.py`) behind a local HTTP server. To replay a synthetic GitHub webhook delivery, sign the payload with the same `OZ_GITHUB_WEBHOOK_SECRET` Vercel uses and POST it at `/api/webhook`:

```sh
BODY='{"action":"opened","pull_request":{"number":42,"state":"open","draft":false,"user":{"login":"alice","type":"User"}},"repository":{"full_name":"acme/widgets"},"installation":{"id":1234}}'
SECRET="$OZ_GITHUB_WEBHOOK_SECRET"
SIGNATURE="sha256=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $2}')"
curl -sS -X POST http://localhost:3000/api/webhook \
  -H "Content-Type: application/json" \
  -H "X-GitHub-Event: pull_request" \
  -H "X-Hub-Signature-256: $SIGNATURE" \
  --data "$BODY"
```

The handler returns 202 with the routed workflow id (or `null` when the event is intentionally ignored). Run `python -m pytest tests` to exercise the same logic without the HTTP plumbing.
