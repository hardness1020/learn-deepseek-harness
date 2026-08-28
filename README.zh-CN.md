<!-- source: README.md @ 3705bd7 -->

<div align="center">

# learn-deepseek-harness

**一切皆为 plugin：从零拆解并重建 DeepSeek Harness。**

[![Studied: dsh 0.1.0-rc.7](https://img.shields.io/badge/Studied-dsh_0.1.0--rc.7-blue)](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

[English](README.md) | [繁體中文](README.zh-TW.md) | 简体中文

<video src="https://github.com/user-attachments/assets/044d258b-7e3a-4448-b0b0-224bd6c504fa" controls width="800"></video>

</div>

[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)（dsh）基于 Cordis 构建，是一套 agent harness。它是个庞大的 TypeScript 项目，从工具到完整子系统都以 plugin 组成。如果一开始就直接读源代码，很容易迷失在各个软件包之间，因为核心设计分散在整个代码库中。

这份教学换一种读法：只用 Python 标准库，分成 4 个阶段、14 个章节，逐步实现一个最小版本 Mini-dsh。每章只加入一项核心机制，再用离线测试验证行为，最后对照真正的 dsh 如何实现。

**目录**：[架构概览](#架构概览) · [阅读方式](#阅读方式) · [章节索引](#章节索引) · [项目结构](#项目结构) · [运行方式](#运行方式) · [参与贡献](#参与贡献) · [延伸阅读](#延伸阅读)

## 架构概览

这张图汇总了 Mini-dsh 的整体架构。kernel 负责挂载与卸载 plugin，loop 负责推进 agent 流程，任何需要访问 log 以外资源的操作，都必须经过明确的能力接口（seam）。

![Mini-dsh 架构](assets/architecture.png)

> 一切都是 plugin，每一次注册也都能完整撤销。

## 阅读方式

每一章都沿用相同的分析架构，固定分成四个部分：

1. **开场**：先不看代码，直接说明本章要解决的设计问题。
2. **核心机制**：逐一拆解需要实现的组件，并搭配代码片段与流程图说明。
3. **对照真正的 dsh**：将 Mini-dsh 的类与函数，对应到真正 dsh 中的实现文件。所有链接都固定在本教学研究的版本，并补充 Mini-dsh 为了保持精简而没有实现的功能。
4. **常见失败模式**：解释少了这项机制后，系统会在哪些地方出问题。

建议依序阅读。每一章都会完整沿用前一章的 `src/`，只添加一项机制，这就是本教学的 Carry-forward 结构。读完一章后，可以直接运行对应的离线测试。若想聚焦某项机制，只要比较前后两章的 `src/` 目录，diff 中的差异就是该章添加的内容。

## 章节索引

| # | 章节 | 设计问题 | 核心机制 |
|---|---------|-----------------|-----------|
| | **Foundation** | | |
| 00 | [Setup](sections/00-setup/README.zh-CN.md) | 为什么核心只使用统一的 `Message` 格式，并通过可替换的 Model seam 调用模型？ | 不绑定 provider 的 `Message`、流式 Model seam、Scripted stand-in |
| 01 | [Kernel](sections/01-kernel/README.zh-CN.md) | 为什么 plugin 的卸载与清理应由框架统一管理？ | 可反向撤销的 fiber/effect 注册 |
| 02 | [Session log](sections/02-session-log/README.zh-CN.md) | 为什么模型历史应从 log 推导，而不是另外保存一份消息列表？ | append-only log + surface + `derive_messages()` |
| 03 | [Compaction](sections/03-compaction/README.zh-CN.md) | 在 log 只能追加的情况下，compaction 如何缩小模型可见的历史？ | surface 的 `replace` 操作 |
| | **The Loop** | | |
| 04 | [Agent loop](sections/04-agent-loop/README.zh-CN.md) | 为什么每个 step 都要重新组装 prompt，并从 log 推导历史？ | turn/step 状态机，log 是唯一可持久状态 |
| 05 | [Tools](sections/05-tools/README.zh-CN.md) | 为什么被拒绝或运行失败的调用，仍然必须产生 `tool/result`？ | scoped registry + pre/ask/guard/execute/post pipeline |
| 06 | [Scheduler](sections/06-scheduler/README.zh-CN.md) | 为什么并行安全的调用会重叠执行，互斥调用会形成 barrier，而取消后尚未开始的调用会收到合成结果？ | 四阶段并行工具 scheduler |
| 07 | [Inbox](sections/07-inbox/README.zh-CN.md) | 为什么 inbox 需要 `next-turn` 与 `next-step` 两种目标，并只在 step 边界认领？ | next-turn/next-step 两种介入时机 |
| 08 | [System prompt](sections/08-system-prompt/README.zh-CN.md) | 为什么动态状态要以 user 消息重新发送，而不是写进 system prompt？ | 有顺序的 provider -> system prompt + 工具列表 + runtime-context 快照 |
| 09 | [Skills](sections/09-skills/README.zh-CN.md) | 为什么 skill 列表以 context 注入，完整内容却由工具按需加载？ | 分层 provider registry；列表先注入，内容按需加载 |
| | **Capabilities** | | |
| 10 | [Capability seams](sections/10-capability-seams/README.zh-CN.md) | 一个能力在什么情况下，才值得拆成 Definition、Provider 与 Consumer？ | Definition/Provider/Consumer 角色分离（fs/shell/sandbox/llm） |
| 11 | [Jobs](sections/11-jobs/README.zh-CN.md) | job id 公开后，谁拥有读取、等待与取消它的权限？ | 只允许拥有者操作的背景工作协议 |
| 12 | [Subagent](sections/12-subagent/README.zh-CN.md) | 为什么 subagent 接口是「启动 child，返回 run」，而不是继承 `Agent`？ | 具名 provider 的委派 registry |
| | **Composition** | | |
| 13 | [Composition](sections/13-composition/README.zh-CN.md) | 为什么 patch 会替换完整 config，而不是深层合并？ | 在空的 entry 列表上依序叠加 patch 层 |

## 项目结构

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
        └── src/         # 完整沿用第 12 章的 src，并加入本章机制、test.py、demo.py
```

## 运行方式

教学中的每项行为都有离线测试可验证。这些测试只使用 Python 标准库，不需安装额外软件包、API key 或网络，而且每次运行都会得到相同结果。

```bash
python sections/00-setup/src/test.py     # one section
for t in sections/*/src/test.py; do python "$t" || break; done   # all sections
```

从第 04 章开始，需要调用模型的章节也会提供在线示例，使用预先写好的 turn 调用 Anthropic API。如果没有设置 API key，示范会自动跳过，不会报错。

```bash
pip install -r requirements.txt
cp .env.example .env   # then fill in ANTHROPIC_API_KEY
python sections/04-agent-loop/src/demo.py
```

## 参与贡献

- **深化某个章节**：补上更精准的代码片段、更完整的失败模式，或更严谨的测试。
- **修正内容**：如果 Mini-dsh 与真正 dsh 的对照有误，或文中任何说法与锁定版本的源代码不一致，欢迎提出修正。

## 延伸阅读

- [Cordis primer](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/cordis-primer.md)：dsh 自己写的入门文，介绍它底下那套 plugin runtime。
- [Cordis tutorial](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/cordis-tutorial)：教你怎么写真正的 dsh plugin，这份 tutorial 把写 plugin 的操作细节全都交给它。
- [Subsystem docs](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/subsystems)：每个子系统都有独立的设计文档，可搭配各章的「对照真正的 dsh」段落阅读。
- [cordiverse/cordis](https://github.com/cordiverse/cordis)：dsh 内嵌进来的上游框架。
