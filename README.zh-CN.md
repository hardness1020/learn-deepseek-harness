<!-- source: README.md @ 75b51d8 -->

<div align="center">

# learn-deepseek-harness

**一切都是 plugin：从零重建 DeepSeek Harness。**

[![Studied: dsh 0.1.0-rc.7](https://img.shields.io/badge/Studied-dsh_0.1.0--rc.7-blue)](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

[English](README.md) | [繁體中文](README.zh-TW.md) | 简体中文

</div>

[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)（dsh）是一套货真价实的 agent harness：一个建在 Cordis 上的大型 TypeScript 代码库，里面每一样东西都是 plugin。第一次读它的源代码会很吃力，因为它的设计想法散落在很多包里。

这份 tutorial 走另一条路。你只用 Python 标准库，跨 4 个 Phase、14 个 Section，重建一个最小版本，叫做 Mini-dsh。每个 Section 只加一个 Mechanism，用一个每次结果都一样的 Offline check 证明它会动，再带你去看真正的 dsh 是在哪里实现这件事的，所有指过去的链接都固定在上面那个 Studied version。

**目录**：[全貌](#全貌) · [怎么读](#怎么读) · [Sections](#sections) · [项目结构](#项目结构) · [怎么跑](#怎么跑) · [怎么参与](#怎么参与) · [延伸阅读](#延伸阅读)

## 全貌

```mermaid
flowchart LR
  subgraph F[Foundation]
    direction TB
    s00[00 setup] --> s01[01 kernel] --> s02[02 session-log] --> s03[03 compaction]
  end
  subgraph L[The Loop]
    direction TB
    s04[04 agent-loop] --> s05[05 tools] --> s06[06 scheduler] --> s07[07 inbox] --> s08[08 system-prompt] --> s09[09 skills]
  end
  subgraph C[Capabilities]
    direction TB
    s10[10 capability-seams] --> s11[11 jobs] --> s12[12 subagent]
  end
  subgraph X[Composition]
    s13[13 composition]
  end
  F --> L --> C --> X
```

有一条规则贯穿每个 Section，因为真正的系统就是建立在这条规则之上：

> 一切都是 plugin，而且每一次注册都可以反向撤销。

## 怎么读

每个 Section 都用同一套 Lens 来读，固定四个部分：

1. **Opening**：还没碰到任何代码之前，先讲清楚这个 Section 要回答的那一个设计问题。
2. **Mechanism**：你要动手做出来的那些零件，配上代码片段和一张流程图。
3. **In real dsh**：一张对照表，把你写的 Mini-dsh 符号对到真 dsh 的符号，每个链接都固定在 Studied version 上；后面再补上真系统有做、而重建版没做的那些部分，也就是 Ceiling。
4. **Failure modes**：少了这个 Mechanism 会坏掉什么，而不是只讲有了它会动什么。

Section 要照顺序读：每一个都把前一个的 `src/` 原封不动搬过来，然后只加一个 Mechanism，这就是 Carry-forward。读到哪个 Section，就顺手把它的 Offline check 跑一遍。想单独看清楚某一个 Mechanism，就 diff 相邻的两个 `src/` 目录：跑出来的 diff 刚好就是那个 Mechanism。

## Sections

| # | Section | 设计问题 | Mechanism |
|---|---------|-----------------|-----------|
| | **Foundation** | | |
| 00 | [Setup](sections/00-setup/README.zh-CN.md) | 为什么 mini-dsh 的核心只讲自己的 Message 形状，而且要通过一个可以换掉的 Model seam 去问 model？ | 不绑 provider 的 `Message`、会做流式输出的 Model seam、Scripted stand-in |
| 01 | [Kernel](sections/01-kernel/README.zh-CN.md) | 为什么卸载一个 plugin 这件事，能交给框架做对，而不是每个 plugin 自己收尾？ | 可反向撤销的 fiber/effect 注册 |
| 02 | [Session log](sections/02-session-log/README.zh-CN.md) | 为什么要从一份 log 推导出 model 看到的历史，而不是直接存一份消息列表？ | 只能追加的 log + surface + deriveMessages |
| 03 | [Compaction](sections/03-compaction/README.zh-CN.md) | 如果 log 只能追加，compaction 要怎么拿掉 model 看得到的东西？ | surface 的 `replace` 操作 |
| | **The Loop** | | |
| 04 | [Agent loop](sections/04-agent-loop/README.zh-CN.md) | 为什么每一个 step 都要重新组一次 prompt、重新推一次历史？ | turn/step 状态机，log 是唯一持久的状态 |
| 05 | [Tools](sections/05-tools/README.zh-CN.md) | 为什么被拒绝或出错的调用，还是会产生一条正常的 tool/result？ | 有作用域的 registry + pre/ask/guard/execute/post pipeline |
| 06 | [Scheduler](sections/06-scheduler/README.zh-CN.md) | 为什么可以并行跑的调用会叠在一起跑，互斥的调用会卡成一道关卡，而还没开始就被中止的调用会拿到一个合成出来的结果？ | 四阶段的并行 tool scheduler |
| 07 | [Inbox](sections/07-inbox/README.zh-CN.md) | 为什么 inbox 要有两个投递目标，而且只在 step 的边界认领？ | next-turn/next-step 两种介入时机 |
| 08 | [System prompt](sections/08-system-prompt/README.zh-CN.md) | 为什么动态状态是一条重新发出的 user 消息，而不是写进 system 文本里？ | 照顺序跑的 provider -> system 文本 + tool 列表 + runtime-context 快照 |
| 09 | [Skills](sections/09-skills/README.zh-CN.md) | 为什么 skill 列表是当成 context 注入，内容却要靠一次 tool 调用才加载进来？ | 分层的 provider registry；列表先注入，内容按需加载 |
| | **Capabilities** | | |
| 10 | [Capability seams](sections/10-capability-seams/README.zh-CN.md) | 一个能力要到什么时候才值得拆成三份？ | Definition/Provider/Consumer 三个抽象基类（fs/shell/sandbox/llm） |
| 11 | [Jobs](sections/11-jobs/README.zh-CN.md) | job id 一旦公开出去，取消的权责归谁？ | 只有拥有者能动的后台工作协议 |
| 12 | [Subagent](sections/12-subagent/README.zh-CN.md) | 为什么接口是架在“开一个 child、交回一次 run”上面，而不是继承出一个 agent 子类？ | 具名 provider 的委派 registry |
| | **Composition** | | |
| 13 | [Composition](sections/13-composition/README.zh-CN.md) | 为什么一个 patch 是整份 config 的替换，而不是深层合并？ | 在一份空的 entry 列表上，照顺序叠 patch 层 |

## 项目结构

```text
learn-deepseek-harness/
├── README.md
├── LICENSE
├── requirements.txt     # live demos only: anthropic, python-dotenv
├── .env.example         # ANTHROPIC_API_KEY / ANTHROPIC_MODEL / optional base URL
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

## 怎么跑

这份 tutorial 讲的每件事，都由 Offline check 来证明。它们只用标准库：不用装东西、不用 API key、不用网络，输出每次都一样。

```bash
python sections/00-setup/src/test.py     # one section
for t in sections/*/src/test.py; do python "$t" || break; done   # all sections
```

会碰到 model 的 Section（04 以后）另外附一个 Live demo，拿写好的 turn 去调用真正的 Anthropic API。没设 key 的话，它会安静地跳过。

```bash
pip install -r requirements.txt
cp .env.example .env   # then fill in ANTHROPIC_API_KEY
python sections/04-agent-loop/src/demo.py
```

## 怎么参与

- **把某个 Section 挖深**：更精准的代码片段、更好的 failure mode、给既有的 Mechanism 一个更严谨的检查。
- **纠正错误**：只要锁定的那版源代码跟它对不上，不管是 mini 对到真 dsh 的对照，还是任何关于 dsh 的说法，都欢迎指出来。

## 延伸阅读

- [Cordis primer](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/cordis-primer.md)：dsh 自己写的入门文，介绍它底下那套 plugin runtime。
- [Cordis tutorial](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/cordis-tutorial)：教你怎么写真正的 dsh plugin。这份 tutorial 把所有写 plugin 的操作细节都交给它。
- [Subsystem docs](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/subsystems)：每个子系统各一份设计文档，对应的就是每个 Section 的 In-real-dsh 那一格。
- [cordiverse/cordis](https://github.com/cordiverse/cordis)：dsh 内嵌进来的上游框架。
