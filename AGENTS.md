# Repository guidance

Read [CONTRIBUTING.md](./CONTRIBUTING.md) for contribution and setup
steps. Read [REPOSITORY.md](./REPOSITORY.md) before changing repository
settings. Read [ARCHITECTURE.md](./ARCHITECTURE.md) before changing the
P2P or RPC design.

When an agent works here, a separate worktree per task keeps the
contributor's own checkout clean: hooks and agents change files where
they run. For example, `git worktree add ../<name> origin/main -b <branch>`,
and `git worktree remove ../<name>` when done.

Choose the least capable model that can reliably complete the task: use
a fast model for straightforward execution, a reasoning-focused model
for multi-step work, and the most capable available model for difficult
or ambiguous problems. Increase capability if progress stalls.

## Code Review Rules

Review against [REVIEWING.md](./REVIEWING.md). Its *The verdict* says
which review opens with a verdict line and what every other review
posts.

The heading is the one Codex's GitHub review asks for
([Customize what Codex reviews](https://learn.chatgpt.com/docs/third-party/github#customize-what-codex-reviews)).
