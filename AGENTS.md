# Repository instructions

## Agent skills

### Issue tracker

Issues, PRDs, and Wayfinder maps are tracked in GitHub Issues for
`nisavid/cqmgr`. See `docs/agents/issue-tracker.md`.

### Triage labels

Use the canonical `needs-triage`, `needs-info`, `ready-for-agent`,
`ready-for-human`, and `wontfix` labels. See
`docs/agents/triage-labels.md`.

### Domain docs

This is a single-context repository with root `CONTEXT.md` and system-wide
ADRs under `docs/adr/`. See `docs/agents/domain.md`.

## Product context

Read `CONTEXT.md` before planning or editing. Use its canonical language in
code, tests, documentation, issues, and commits. Keep detailed contracts and
research under `docs/` and keep `README.md` as the verified human entrypoint.

Use the standing skill that fits the work. Use `grilling` and
`domain-modeling` for unresolved product decisions, `wayfinder` for planning
maps, `tdd` for implementation, and `impeccable` for interface work.

## Git and validation

This is a personal `nisavid` project. Use `Ivan D Vasin <ivan@nisavid.io>` for
Git work and the `nisavid` GitHub account for repository mutations. Prefix
branches with `ivan/`. Use Conventional Commits for commits and pull request
titles.

For every Git-backed task, use `checkpointing-and-publishing-git-work` at the
start, at clean checkpoints, and before stopping. Every change requires
`git diff --check`. Changes to Serena configuration also require
`serena project health-check .` and `serena memories check`.

## Safety

Read-only quota inspection is distinct from quota mutation. Never create,
amend, or delete a live cloud quota request without explicit task-specific
authorization for the exact resource scope and operation.
