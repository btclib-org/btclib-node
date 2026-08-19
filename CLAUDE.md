# CLAUDE.md

Guidance for Claude Code when working in this repository. See
README.md and CONTRIBUTING.md for how to build, test and contribute.

## Model

The default model for this repository is Sonnet. Switch to Opus only
for architectural decisions with conflicting constraints (design
choices with non-obvious trade-offs, refactors that cross many
modules with unclear dependencies, diagnosis where the symptom does
not point to the cause). Use `/model opus` for the session, then
switch back to Sonnet.

Do not use Fable unless explicitly instructed.
