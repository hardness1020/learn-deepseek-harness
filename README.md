<div align="center">

# learn-deepseek-harness

**Everything is a plugin: rebuild DeepSeek Harness from scratch.**

[![Studied: dsh 0.1.0-rc.7](https://img.shields.io/badge/Studied-dsh_0.1.0--rc.7-blue)](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

English | [繁體中文](README.zh-TW.md) | [简体中文](README.zh-CN.md)

<video src="https://github.com/user-attachments/assets/044d258b-7e3a-4448-b0b0-224bd6c504fa" controls width="800"></video>

</div>

[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) (dsh) is a production agent harness: a large TypeScript codebase built on Cordis, where everything is a plugin. Reading it from top to bottom is difficult because its core design is distributed across many packages.

This tutorial takes a different approach. You will rebuild a minimal version, Mini-dsh, using only Python's standard library. The material is organized into 14 sections across four phases. Each section adds one mechanism, verifies it with an offline check, and links it to the corresponding implementation in dsh.

**Contents**: [Big picture](#big-picture) · [How to learn](#how-to-learn) · [Sections](#sections) · [Repository structure](#repository-structure) · [Running](#running) · [Contributing](#contributing) · [References](#references)

## Big picture

Everything you build, on one page: the kernel that mounts it, the loop that runs it, and the seams it reaches through to touch anything outside the log.

![Mini-dsh architecture](assets/architecture.png)

One rule carries through every section because the real system is built on it:

> Everything is a plugin, and every registration is reversible.

## How to learn

Every section follows the same four-part structure:

1. **Opening**: the design question the section answers before introducing any code.
2. **Mechanism**: the components you will build, explained with excerpts and a flow diagram.
3. **In real dsh**: a table that maps Mini-dsh symbols to their dsh counterparts. The links are pinned to the studied version and note the production features omitted from this tutorial.
4. **Failure modes**: what breaks when the mechanism is missing, not only what works when it is present.

Read the sections in order. Each one copies the previous `src/` directory unchanged and adds one mechanism. Run the section's offline check as you go. To examine a mechanism in isolation, compare two adjacent `src/` directories; the diff contains only the new mechanism.

## Sections

| # | Section | Design question | Mechanism |
|---|---------|-----------------|-----------|
| | **Foundation** | | |
| 00 | [Setup](sections/00-setup/) | Why should Mini-dsh use its own `Message` format behind a swappable model interface? | provider-agnostic `Message`, streaming model interface, scripted stand-in |
| 01 | [Kernel](sections/01-kernel/) | Why should the framework own plugin cleanup? | fiber/effect reversible registrations |
| 02 | [Session log](sections/02-session-log/) | Why derive model history from a log instead of storing a message list? | append-only log + surface + `deriveMessages()` |
| 03 | [Compaction](sections/03-compaction/) | If the log is append-only, how does compaction remove anything the model sees? | surface `replace` op |
| | **The Loop** | | |
| 04 | [Agent loop](sections/04-agent-loop/) | Why reassemble the prompt and rederive history before every step? | turn/step state machine, with the log as the only durable state |
| 05 | [Tools](sections/05-tools/) | Why should a denied or failed call still produce a normal `tool/result`? | scoped registry + pre/ask/guard/execute/post pipeline |
| 06 | [Scheduler](sections/06-scheduler/) | Why do parallel-safe calls overlap, exclusive calls form barriers, and unstarted calls receive synthetic results after cancellation? | four-stage parallel tool scheduler |
| 07 | [Inbox](sections/07-inbox/) | Why use two inbox targets and claim messages only at step boundaries? | next-turn/next-step steering |
| 08 | [System prompt](sections/08-system-prompt/) | Why represent dynamic state as a re-emitted user message instead of system text? | ordered providers -> system text + tool list + runtime-context snapshot |
| 09 | [Skills](sections/09-skills/) | Why inject a catalog as context but load full skill content through a tool call? | layered provider registry; catalog injected, bodies loaded on demand |
| | **Capabilities** | | |
| 10 | [Capability seams](sections/10-capability-seams/) | When does a capability justify the three-way split? | Definition/Provider/Consumer ABCs (fs/shell/sandbox/llm) |
| 11 | [Jobs](sections/11-jobs/) | Who owns cancellation after a job ID is published? | owner-fenced background-work protocol |
| 12 | [Subagent](sections/12-subagent/) | Why should subagents use a provider interface instead of inheriting from `Agent`? | named-provider delegation registry |
| | **Composition** | | |
| 13 | [Composition](sections/13-composition/) | Why should a patch replace the entire configuration instead of deep-merging it? | ordered patch layers over an empty entry list |

## Repository structure

```text
learn-deepseek-harness/
├── README.md
├── LICENSE
├── requirements.txt     # live demos only: anthropic, python-dotenv
├── .env.example         # ANTHROPIC_API_KEY / ANTHROPIC_MODEL / optional base URL
├── assets/              # the architecture diagram, and the script that draws it
└── sections/
    ├── 00-setup/
    │   ├── README.md
    │   └── src/         # message.py, standin.py, test.py
    ├── 01-kernel/
    │   ├── README.md
    │   └── src/         # 00's src verbatim + kernel.py, test.py
    ├── ...
    └── 13-composition/
        ├── README.md
        └── src/         # 12's src verbatim + this Mechanism, test.py, demo.py
```

## Running

The offline checks verify each mechanism. They use only Python's standard library, require no API key or network connection, and produce deterministic output.

```bash
python sections/00-setup/src/test.py     # one section
for t in sections/*/src/test.py; do python "$t" || break; done   # all sections
```

Sections 04 and later also include a live demo that runs scripted turns against the Anthropic API. The script exits cleanly if no API key is configured.

```bash
pip install -r requirements.txt
cp .env.example .env   # then fill in ANTHROPIC_API_KEY
python sections/04-agent-loop/src/demo.py
```

## Contributing

- **Improve a section**: add a clearer excerpt, a more useful failure mode, or a stricter check for an existing mechanism.
- **Correct the record**: a mini-to-real mapping or claim about dsh that the pinned source contradicts.

## References

- [Cordis primer](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/cordis-primer.md): dsh's own intro to the plugin runtime it is built on.
- [Cordis tutorial](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/cordis-tutorial): writing real dsh plugins; this tutorial defers all plugin-authoring how-to there.
- [Subsystem docs](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/subsystems): design documentation for each subsystem, corresponding to the "In real dsh" section in each chapter.
- [cordiverse/cordis](https://github.com/cordiverse/cordis): the upstream framework dsh vendors.
