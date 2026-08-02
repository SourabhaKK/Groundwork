# CLAUDE.md

@AGENTS.md

Operational commands, code style, testing rules, and boundaries are in `AGENTS.md` above.

Claude Code was used as the implementation tool for this project. The build
followed the process in `workflows.md`, which required every non-trivial
decision to be stated with its reasoning and its rejected alternative, and
confirmed, before any code was written. That process is recorded rather than
paraphrased: `workflows.md` holds the rules and the pre-build plan audits, and
`BUILD_LOG.md` holds what each phase actually produced — including the
assumptions that live testing broke.

Anyone continuing work here should keep to it. The boundaries in `AGENTS.md`
are the load-bearing part: they are the reason grounding guarantees in this
codebase are structural rather than prompted.

Documents, in read order: `DESIGN_NOTE.md` → `PRD.md` → `TECH_STACK.md` →
`TASKS.md` → `BUILD_LOG.md`. `workflows.md` covers process rather than product.
