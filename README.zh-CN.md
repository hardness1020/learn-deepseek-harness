<!-- source: README.md @ 2e2d45e -->

<div align="center">

# learn-deepseek-harness

**一切都是 plugin：从零重建 DeepSeek Harness。**

[![Studied: dsh 0.1.0-rc.7](https://img.shields.io/badge/Studied-dsh_0.1.0--rc.7-blue)](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

[English](README.md) | [繁體中文](README.zh-TW.md) | 简体中文

<video src="https://github.com/user-attachments/assets/044d258b-7e3a-4448-b0b0-224bd6c504fa" controls width="800"></video>

</div>

[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)（dsh）是一套 agent harness：一个大型的 TypeScript 代码库，建立在 Cordis 之上，里面每一样东西都是 plugin。一上来就直接读它的源代码会很吃力，因为它的设计想法散落在很多包里。

这份 tutorial 采取不同的做法。使用 Python 标准库，分成 4 个 Phase、14 个 Section，一路重建出一个最小的版本，叫做 Mini-dsh。每个 Section 只加一个 Mechanism，并用一个 Offline check 加以验证。接着再看真正的 dsh 如何实现。

**目录**：[全貌](#全貌) · [怎么读](#怎么读) · [Sections](#sections) · [项目结构](#项目结构) · [怎么跑](#怎么跑) · [怎么参与](#怎么参与) · [延伸阅读](#延伸阅读)

## 全貌

实现内容全都在这张图上：kernel 负责把每个 plugin 挂载起来，loop 负责把整套流程跑起来，而要碰到 log 以外的东西时，就得经过那几道 seam。

![Mini-dsh 架构](assets/architecture.png)

> 一切都是 plugin，而且每一次注册都可以反向撤销。

## 怎么读

每个 Section 都用同一套 Lens 来读，固定分成四个部分：

1. **Opening**：还没碰到任何代码之前，先讲清楚这个 Section 要回答的那一个设计问题。
2. **Mechanism**：你要动手做出来的那些零件，配上代码片段和一张流程图。
3. **In real dsh**：一张对照表，把你写的 Mini-dsh 类和函数，对到真 dsh 里对应的类、函数和文件，每个链接都固定在 Studied version 上。表格后面再补上真系统有做、而 Mini-dsh 没做的那些部分，也就是 Ceiling。
4. **Failure modes**：少了这个 Mechanism 会坏掉什么，而不是只讲有了它会动什么。

Section 要照顺序读：每一个都把前一个的 `src/` 原封不动搬过来，然后只加一个 Mechanism，这就是 Carry-forward。读到哪个 Section，就顺手把它的 Offline check 跑一遍。想单独看清楚某一个 Mechanism，就把相邻的两个 `src/` 目录 diff 一下，跑出来的差异刚好就是那个 Mechanism。

## Sections

| # | Section | 设计问题 | Mechanism |
|---|---------|-----------------|-----------|
| | **Foundation** | | |
| 00 | [Setup](sections/00-setup/README.zh-CN.md) | 为什么 mini-dsh 的核心只讲自己那套 Message 格式，而且一定要隔着一个随时可以换掉的 Model seam，才去问 model？ | 不绑 provider 的 `Message`、会流式输出的 Model seam、Scripted stand-in |
| 01 | [Kernel](sections/01-kernel/README.zh-CN.md) | 为什么卸载一个 plugin 这件事，可以交给框架去做对，而不是每个 plugin 自己收尾？ | 可反向撤销的 fiber/effect 注册 |
| 02 | [Session log](sections/02-session-log/README.zh-CN.md) | 为什么要从一份 log 推导出 model 看到的历史，而不是直接存一份消息列表？ | 只能追加的 log + surface + deriveMessages |
| 03 | [Compaction](sections/03-compaction/README.zh-CN.md) | 如果 log 只能追加，compaction 要怎么拿掉 model 看得到的东西？ | surface 的 `replace` 操作 |
| | **The Loop** | | |
| 04 | [Agent loop](sections/04-agent-loop/README.zh-CN.md) | 为什么每一个 step 都要重新组一次 prompt、重新推一次历史？ | turn/step 状态机，log 是唯一持久的状态 |
| 05 | [Tools](sections/05-tools/README.zh-CN.md) | 为什么一个被拒绝、或是执行出错的调用，还是会产生一条正常的 tool/result？ | 有作用域的 registry + pre/ask/guard/execute/post pipeline |
| 06 | [Scheduler](sections/06-scheduler/README.zh-CN.md) | 为什么可以并行跑的调用会叠在一起跑，互斥的调用会挡成一道关卡，而还没开始就被中止的调用，会拿到一个合成出来的结果？ | 四阶段的并行 tool scheduler |
| 07 | [Inbox](sections/07-inbox/README.zh-CN.md) | 为什么 inbox 要有两个投递目标，而且只在 step 的边界认领？ | next-turn/next-step 两种介入时机 |
| 08 | [System prompt](sections/08-system-prompt/README.zh-CN.md) | 为什么动态的状态要当成一条重新发出的 user 消息，而不是写进 system 文本里？ | 照顺序跑的 provider -> system 文本 + tool 列表 + runtime-context 快照 |
| 09 | [Skills](sections/09-skills/README.zh-CN.md) | 为什么 skill 列表是当成 context 注入，内容却要靠一次 tool 调用才加载进来？ | 分层的 provider registry；列表先注入，内容按需加载 |
| | **Capabilities** | | |
| 10 | [Capability seams](sections/10-capability-seams/README.zh-CN.md) | 一个能力要到什么时候才值得拆成三份？ | Definition/Provider/Consumer 三个抽象基类（fs/shell/sandbox/llm） |
| 11 | [Jobs](sections/11-jobs/README.zh-CN.md) | job id 一旦公开出去，取消的权责归谁？ | 只有拥有者能动的后台工作协议 |
| 12 | [Subagent](sections/12-subagent/README.zh-CN.md) | 为什么接口是架在“开一个 child、交回一次 run”上面，而不是从 agent 继承出一个子类？ | 具名 provider 的委派 registry |
| | **Composition** | | |
| 13 | [Composition](sections/13-composition/README.zh-CN.md) | 为什么一个 patch 是整份 config 的替换，而不是深层合并？ | 在一份空的 entry 列表上，照顺序叠 patch 层 |

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
        └── src/         # 12's src verbatim + this Mechanism, test.py, demo.py
```

## 怎么跑

这份 tutorial 讲的每件事，都由 Offline check 来证明。它们只用标准库，不用安装任何东西、不用 API key、也不用网络，而且每次跑出来的输出都一样。

```bash
python sections/00-setup/src/test.py     # one section
for t in sections/*/src/test.py; do python "$t" || break; done   # all sections
```

会碰到 model 的 Section（04 以后）还另外附一个 Live demo，拿事先写好的 turn 去调用真正的 Anthropic API。没设 key 的话，它会安静地跳过。

```bash
pip install -r requirements.txt
cp .env.example .env   # then fill in ANTHROPIC_API_KEY
python sections/04-agent-loop/src/demo.py
```

## 怎么参与

- **把某个 Section 挖深**：更精准的代码片段、更好的 failure mode，或是给既有的 Mechanism 一个更严谨的检查。
- **纠正错误**：不管是 mini 对到真 dsh 的对照，还是任何一句关于 dsh 的说法，只要跟锁定的那版源代码对不上，都欢迎指出来。

## 延伸阅读

- [Cordis primer](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/cordis-primer.md)：dsh 自己写的入门文，介绍它底下那套 plugin runtime。
- [Cordis tutorial](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/cordis-tutorial)：教你怎么写真正的 dsh plugin，这份 tutorial 把写 plugin 的操作细节全都交给它。
- [Subsystem docs](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/subsystems)：每个子系统各一份设计文档，对应的就是每个 Section 里 In-real-dsh 的那一格。
- [cordiverse/cordis](https://github.com/cordiverse/cordis)：dsh 内嵌进来的上游框架。
