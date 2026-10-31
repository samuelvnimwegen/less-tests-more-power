---
name: auto-review
description: Review a PR in this repo with nobody watching and stage the findings as a PENDING review that only Samuel can see. Runs /code-review at xhigh, a design agent, the repo's checks and a test coverage pass, then posts every finding as a pending inline comment. It never asks a question, so a cloud task, a GitHub Action or a scheduled run can start it. Use when asked to "auto review PR N", or when a trigger passes a PR number.
argument-hint: <PR number> [--effort=low|medium|high|xhigh|max]
allowed-tools: Bash Read Grep Glob Write Skill Agent
---

Review one PR and leave the findings as a **pending** review on GitHub. Nobody else can see a pending review. Samuel reads it, deletes what he disagrees with and submits the rest himself. Never submit it.

This skill runs unattended. Never ask a question and never wait for an answer. When something blocks the run, stop and say why in the report.

## Ground rules

- Use only `gh api` REST calls and `git`. Never `gh pr`, `gh issue`, `gh repo view` or `gh api graphql`. Claude's cloud sessions reach GitHub through a proxy that blocks GraphQL, and all of those use it.
- Work in a scratch folder, `RUN=$(mktemp -d)`. Leave the checkout the session started in alone, apart from `git fetch`.
- Get the repo from the remote:

```
REPO=$(git remote get-url origin | sed -E 's#(git@github.com:|https://github.com/)##; s#\.git$##')
```

## Step 1: Find the PR

Parse `$ARGUMENTS`:

- **A PR number.** Without one, look up the open PR for the current branch with `gh api "repos/$REPO/pulls?head=${REPO%%/*}:$(git branch --show-current)&state=open" --jq '.[0].number'`. If that gives nothing, stop.
- **`--effort=<level>`** sets the `/code-review` level. The default is `xhigh`. `ultra` is not allowed, because it runs in the cloud and posts a submitted review. Fall back to `xhigh` and say so.

Fetch the PR:

```
gh api repos/$REPO/pulls/$N --jq '{state, merged, title, body, html_url, head_sha: .head.sha, head: .head.ref, base: .base.ref}'
```

Stop when it's closed or merged. Read the base from `base`. Never assume `development` or `main`.

Check who `gh` acts as with `gh api user --jq .login`. A login that ends in `[bot]` would own a review Samuel can never see, so stop. If the call is refused, carry on. Cloud sessions only allow repo endpoints.

## Step 2: Check for a pending review

```
gh api repos/$REPO/pulls/$N/reviews --jq '.[] | select(.state=="PENDING") | .id'
```

GitHub allows one pending review per person per PR. If one exists, stop and report its id. It may hold comments Samuel wrote by hand, and deleted pending comments can't be recovered. This check comes before any analysis, so a blocked run costs nothing.

## Step 3: Get the diff and the code

```
gh api repos/$REPO/pulls/$N/files --paginate --jq '.[] | "\(.filename) +\(.additions) -\(.deletions)"'
gh api repos/$REPO/pulls/$N -H "Accept: application/vnd.github.diff" > $RUN/pr.diff
git fetch -q origin pull/$N/head
git fetch -q origin $BASE
git worktree add -q --detach $RUN/head $HEAD_SHA
git worktree add -q --detach $RUN/base origin/$BASE
```

Read each changed file in full in `$RUN/head`, not only the hunks. The diff is still what counts as changed, and a line comment can only sit on a line inside it.

## Step 4: Check the PR against its issue

Look in the PR body for `Closes #N`, `Fixes #N`, `Resolves #N`, a bare `#N` or an issue URL. GitHub's own list of linked issues is GraphQL only, so the body is the source here. Read each issue with its comments:

```
gh api repos/$REPO/issues/$I --jq '{number, title, body, state}'
gh api repos/$REPO/issues/$I/comments --jq '.[].body'
```

Turn the issue into a flat list of things the PR should deliver. Drop template text and process steps. Mark each item **covered** (name the file), **partial** (name the gap) or **not found**. Partial and not found items go in the review body under `Issue coverage`, one line each, written as a question: "Step 3 of #12 (retry on a 429) isn't in the diff. Left for later?" A partial item with a clear stopping point can sit inline where the work stops.

No linked issue means this step is skipped. Say so in the report.

## Step 5: Start the two agents

Launch both in the background with the **Agent** tool before Step 6, so they work while `/code-review` runs. Brief both with the hard limits first: no posting to GitHub, no `gh api` with POST, PATCH or DELETE, no commits, no push and no edits outside `$RUN`. Then give them the PR number, the head SHA, the base branch, both worktree paths, the PR body and the issue list from Step 4.

### The design agent

A `general-purpose` agent that reads the change and asks three questions. Does the code sit in the right place? Is there more of it than the task needs? Is there a much simpler way to get the same behaviour?

Tell it what to look for:

- Code in a module that doesn't own the idea, or a new helper next to one the repo already has.
- Abstractions, options or base classes built for a case the PR doesn't use. A parameter whose only caller always passes the same value.
- A function whose whole body is one line, unless something needs it as an interface.
- Comments that repeat the line under them, and docstrings longer than the function needs.
- New one off flags or branches added to a flow that had nothing to do with the change.
- A reframing that keeps the behaviour and deletes whole branches or layers. It never proposes a change in behaviour.

It judges against the repo's `CLAUDE.md` if there is one, the ruff and basedpyright setup in `pyproject.toml`, and Samuel's global `CLAUDE.md` when the session has it loaded. It skips anything the linters already report.

Ask for a short verdict, then at most five concerns ordered by weight. Each concern names its fix, and the one file and line where it shows. A concern about the whole change says so instead. Zero concerns is a fine answer, so it should never pad.

### The checks agent

Use the `checks-runner` agent when it exists. Otherwise use a `general-purpose` agent with `model: "sonnet"`. It gets 10 minutes at most. Give it the deadline as a clock time, and tell it to stop and report what it has when time is up.

It runs these in `$RUN/head`, installing uv first if it's missing:

```
timeout 120 uv sync
timeout 60 uv run ruff check .
timeout 60 uv run ruff format --check .
timeout 120 uv run basedpyright
timeout 240 uv run pytest -q
```

Only failing tests run again in `$RUN/base`, so an old failure isn't blamed on this PR. It never sets `LESSTESTS_LIVE=1`, because those tests need Docker. For each break it reports the command, its exact output, whether base breaks too, and the changed line it comes from.

## Step 6: Run `/code-review`

Invoke it with the **Skill** tool: `skill: "code-review"`, `args: "<effort> <N>"`. Always pass the PR number. Without it, `/code-review` reviews the local working tree instead. Never pass `--comment` or `--fix`, and never use `ultra`. If it can't load the PR, try once more with `origin/<head>` as the target. If that fails too, say so in the report and carry on.

Pass every finding through. Don't drop any for being small or unsure. Samuel is the filter, and deleting a pending comment takes one click. Only three things change on the way:

- **Merge duplicates** with the coverage pass and the agents. One comment per point.
- **Rewrite the wording** to the rules in "Writing the comments". Keep the claim, the file and the line.
- **Fix the claims.** Re-measure every number yourself and quote your own figure. Before labelling anything High, find the code that produces the bad input. If nothing can produce it, it's Low and the comment says why.

## Step 7: Test coverage

Run this when the PR changes Python under `src/`. Sort each change:

- **New public class or function** with no test: flag it.
- **Changed code that already had tests**, with no test file touched: flag it. The old tests may still test the old behaviour.
- **Changed code that never had tests**: skip it.

`git grep <name> origin/$BASE -- tests/` tells which case it is. Anchor the comment on the production line, not on a test file. The question is whether the behaviour is tested, not whether the tests are pretty.

## Step 8: Collect and merge

Wait for both agents, but never more than a minute past the checks deadline. Checks that didn't finish go in the report as unfinished. Then:

- Each design concern on a changed line becomes a comment with the `**Design**` label. The verdict and concerns without a line go in the report.
- Each break on a changed line becomes a comment. Rerun its command yourself once. Only when you see the same output does it get the ` (verified with code run)` suffix, like `**High (verified with code run)**`. Show what the run printed in place of a made up example. A break that matches a `/code-review` finding joins that comment.
- Failures that also happen on base, and checks that passed, go in the report only.
- When a design concern and a line finding say the same thing, keep the line finding.

## Writing the comments

Samuel reads these on his own PR, sometimes weeks later. Write so he gets each one on the first read.

**Label first.** The body opens with one label in bold on its own line:

| Label | When |
|---|---|
| `**High**` | Wrong data reaches a caller, or the code crashes on input it should take. |
| `**Medium**` | Real, but it needs an odd input, unusual config or two things at once. |
| `**Low**` | Nothing breaks today. It will drift or mislead the next reader. |
| `**Nitpick**` | Style or naming. Fine to ignore. |
| `**Design**` | From the design agent. The code works, its shape or size is the question. |

The first four can take the ` (verified with code run)` suffix. A finding about a docstring is a Nitpick, unless the code is the wrong part and the docstring only gave it away. No emoji anywhere.

**Plain words.**

- One fact per sentence, about 15 words. At most one code name per sentence.
- 60 to 90 words of prose per comment. A code example doesn't count.
- Say what goes wrong, when it goes wrong, and what to do.
- Say "I think" on a judgement. A fact you checked stays flat.
- No dashes and no semicolons.

**Show the break in code** when it can be shown. Use the real names, show the input that starts it, mark the failing line and keep it under about 8 lines:

````
`widths` is empty on the first frame, so this divides by zero.

```python
widths = []                         # first frame
mean = sum(widths) / len(widths)    # ZeroDivisionError
```

Return None when `widths` is empty.
````

Never invent a function name to build an example. Write the failure in prose instead.

**Suggestion blocks** only when the fix is exact and at most about 5 lines. The block holds the full replacement for the lines the comment covers.

**The repo is public.** Never cite a home directory path, a scratch file or anything outside the repo. A comment becomes public once Samuel submits it.

## Step 9: Post the pending review

Zero comments and nothing for the body means no review. Skip to the report.

Write the payload to `$RUN/review.json` and read it back before sending, so a broken code fence shows up here:

```json
{
  "commit_id": "<head sha>",
  "body": "<label counts, the Issue coverage lines, and a split if the design agent asked for one>",
  "comments": [
    {"path": "src/lesstests/foo.py", "line": 42, "side": "RIGHT", "body": "**High**\n\n..."},
    {"path": "src/lesstests/bar.py", "start_line": 10, "start_side": "RIGHT", "line": 14, "side": "RIGHT", "body": "**Low**\n\n..."}
  ]
}
```

- `line` is the line number in the new file. A comment on a deleted line uses `side: "LEFT"` and the old line number.
- Never add an `event` field. Leaving it out is what keeps the review pending.

```
gh api -X POST repos/$REPO/pulls/$N/reviews --input $RUN/review.json
```

GitHub rejects the whole call when one comment points at a line outside the diff. Fix that comment and send the full set again. Never drop comments to get it through.

Then check it stayed pending:

```
gh api repos/$REPO/pulls/$N/reviews --jq '.[] | select(.state=="PENDING") | {id, user: .user.login}'
```

Any other state means it got submitted by accident. Say so at the top of the report.

## Step 10: Clean up and report

Remove both worktrees with `git worktree remove --force $RUN/head` and `git worktree remove --force $RUN/base`.

Then report, short:

- The PR link, and its `/files` link where the review waits.
- The effort used and the comment count per label.
- The issue coverage tally, or that no issue was linked.
- Each check as passed, failed, failing on base too, or unfinished. How many breaks became verified comments.
- The design verdict, and any concern that had no line.
- Anything that blocked or failed, like `/code-review` not loading the PR.
- That the review is pending, or why no review was posted.

## Hard rules

- Never submit the review. No `event` field and no `gh pr review`.
- Never delete, replace or add to an existing pending review.
- Never ask a question or wait for input.
- Never use GraphQL, `gh pr` or `gh issue`.
- Never let an agent post, push, commit or edit outside `$RUN`.
- Never put ` (verified with code run)` on a finding you didn't rerun yourself.
- Never quote a number you haven't reproduced.
- Never set `LESSTESTS_LIVE=1`, call a paid API or hit a live service.
