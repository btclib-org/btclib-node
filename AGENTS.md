# Repository guidance

Read [CONTRIBUTING.md](./CONTRIBUTING.md) for contribution, worktree,
setup, and cleanup steps. Read [REPOSITORY.md](./REPOSITORY.md) before
changing repository settings, and [REVIEWING.md](./REVIEWING.md) before
reviewing a pull request. Read [ARCHITECTURE.md](./ARCHITECTURE.md)
before changing the P2P or RPC design.

Keep the primary checkout clean and on `main`; use a separate worktree
for each task, following the setup and cleanup commands in
`CONTRIBUTING.md`.

Choose the least capable model that can reliably complete the task: use
a fast model for straightforward execution, a reasoning-focused model
for multi-step work, and the most capable available model for difficult
or ambiguous problems. Increase capability if progress stalls.
