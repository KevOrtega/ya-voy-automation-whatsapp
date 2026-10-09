# Agent skills

Skills for AI coding agents working on Ya Voy. Each skill is a folder with a `SKILL.md`
(YAML frontmatter with `name` and `description`, then instructions), following the
[Agent Skills](https://agentskills.io) format used by Claude Code (`.claude/skills/`) and other
harnesses. Agents that do not load skills automatically should read the relevant `SKILL.md`
when its `description` matches the task; [`AGENTS.md`](../../AGENTS.md) lists when to use each one.

## Project-specific skills

Written for this repository. Edit them freely when the docs or conventions change.

| Skill | Purpose |
|-------|---------|
| [`ya-voy-conventions`](ya-voy-conventions/SKILL.md) | Stack, language rules, where the docs and DC/RF/CU ids live, how to trace a change |
| [`concurrency-and-race-testing`](concurrency-and-race-testing/SKILL.md) | Implementing and testing atomic claim, claim vs cancel, webhook dedup and outbox idempotency against real Postgres |
| [`evolution-api-events`](evolution-api-events/SKILL.md) | What the docs assume about Evolution API payloads and what is still pending verification with a real number |
| [`stage-delivery-checklist`](stage-delivery-checklist/SKILL.md) | What a stage must include before it is handed to the auditor |

## Vendored third-party skills

Copied verbatim (no local edits) from **obra/superpowers**.

| Field | Value |
|-------|-------|
| Source | https://github.com/obra/superpowers |
| Version | v6.4.2 |
| Commit | `8ca22dba9a94f28898bbce59f2537ff4d87c747d` (2026-09-25) |
| License | MIT, Copyright (c) 2025 Jesse Vincent; full text in [`LICENSE.superpowers`](LICENSE.superpowers) |

Vendored skills: `test-driven-development`, `systematic-debugging`,
`verification-before-completion`, `writing-plans`, `executing-plans`,
`subagent-driven-development`, `dispatching-parallel-agents`, `requesting-code-review`,
`receiving-code-review`, `using-git-worktrees`, `finishing-a-development-branch`.

Notes on the vendored copies:

- They refer to each other as `superpowers:<skill-name>`; in this repo that means the
  skill folder with the same name here.
- Their default paths (`docs/superpowers/plans/`, `docs/superpowers/specs/`) are overridden by
  `AGENTS.md`: plans for this project go to `docs/plans/`.
- `executing-plans` and `subagent-driven-development` share helper scripts and keep a
  per-plan workspace under `.superpowers/sdd/`, which ignores itself in git.
- `executing-plans` mentions `../using-superpowers/references/` for per-harness tool names;
  that skill is not vendored (see below), so that pointer is dangling and can be ignored.

Not vendored, on purpose:

- `brainstorming`: the requirements and design are already settled in `docs/` (DC-01..DC-35);
  it also ships an optional local web "visual companion" that loads a remote logo for usage
  telemetry.
- `using-superpowers`: a bootstrap that forces skill lookup before every reply; `AGENTS.md`
  covers that role for this repo.
- `writing-skills`, `diagnosing-superpowers`: meta tooling for authoring skills and debugging
  the superpowers plugin itself.

To update: clone the upstream repo, copy the same folders over these, review the diff, and
update the commit hash above.
