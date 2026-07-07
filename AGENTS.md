# AGENTS.md — Atlas hook

This repo is the **ingstr** component of the AgentEco platform, governed by
**Architecture-Above-Code** (AAC). Its architecture does not live in this repo — it lives
above it, in the project vault at `Atlas-AgentEco/components/ingstr/`
(dev machine: `G:\VSProjects\Atlas-AgentEco\components\ingstr\`; the method spec is
`G:\VSProjects\Atlas\AAC-method.md`).

## Before working — every session

From the vault root (`Atlas-AgentEco/`):

1. Read `architecture/constitution.md` — the global principles.
2. Resolve ingstr's edges in `registry/io-graph.yml`
   (or the compiled `registry/.compiled/ingstr/io-manifest.yml` if present).
3. Read each upstream provider's `docs/provides/` at the **pinned** version.
   If latest > pinned, note the drift — review impact, re-pin deliberately.
4. Read consumers' `docs/needs/` on every edge where `from == ingstr` — asks ingstr must answer.
5. Skim `architecture/proposals/` for in-flight ADRs with `affects: [ingstr]`.

## After working

- Publish contracts ingstr provides to `components/ingstr/docs/provides/`, and asks/feedback
  aimed at providers to `components/ingstr/docs/needs/`, versioning per AAC-method §4
  (PATCH in place; MINOR/MAJOR = new `…vX_Y.md`, prior file to `archive/`).
- Changes to shared architecture go through an ADR in `architecture/proposals/` —
  never edit the constitution directly.
- Bump `updated:` in `components/ingstr/component.md`.
