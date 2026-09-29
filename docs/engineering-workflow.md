# Engineering workflow — branches and merging

This project does **not** use pull requests. There is no PR review step, no GitHub PR
approval, and nobody should ever be asked to "open a PR" or "merge the PR" — least of all
the founder. Work lands by the engineer who wrote it merging their own branch into `main`.

## The loop

1. Branch off `main`: `fix/aud-NNN-short-slug` or `feat/aud-NNN-short-slug`.
2. Implement the change, including tests.
3. Run the suite: `./scripts/test.sh` (backend pytest + frontend Vitest via
   `compose.test.yaml`). Use `--backend-only` / `--frontend-only` while iterating, but run
   the full suite before merging.
4. If the change affects deployed behaviour, confirm the staging deploy and
   `./scripts/smoke-test.sh` are green.
5. Green tests + green deploy → merge into `main` yourself and push:

   ```bash
   git checkout main && git pull --ff-only
   git merge --no-ff fix/aud-NNN-short-slug -m "Merge fix/aud-NNN-short-slug into main: <what changed> (AUD-NNN)"
   git push origin main
   ```

6. Comment the merge commit SHA on the Paperclip issue and mark it `done`.

An issue is not `done` until its branch is merged to `main`. "Branch pushed" is not done.

## When to escalate the merge instead of self-merging

Push the branch, mark the issue `in_review`, and ask **infraLead** to review and merge when:

- tests fail, the staging deploy is red, or you cannot run the suite at all;
- there is an unresolved merge conflict you are not confident resolving;
- the change is risky: schema-destructive migrations, secret or auth/session handling,
  or a cross-cutting refactor spanning backend and frontend;
- the decision behind the change is not yours to make.

In that comment, name the specific blocker and the action that unblocks it. Do not leave an
issue in `in_review` without both.

## QA gate

qaEngineer gates **branches**, not PRs. A green gate is a Paperclip comment clearing the
branch for merge; the branch owner then merges it. qaEngineer does not merge, and does not
push to `main`.

## Production deploys

Merging to `main` needs no founder approval. Deploying to production still does, per the
guarded deploy pipeline.
