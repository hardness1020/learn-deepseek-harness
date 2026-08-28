<!-- source: README.md @ 3705bd7 -->

# 00 · Setup

[English](README.md) | [繁體中文](README.zh-TW.md) | 简体中文

> harness 里许多地方都需要调用模型。如果每个模块都直接依赖某家 provider 的 SDK，它的格式就会扩散到 prompt、log 和 loop 中。因此，核心只使用统一的消息格式，并把 provider 藏在可替换的模型接口后面。

DeepSeek Harness（dsh）是一套大型 TypeScript agent harness。工具、prompt，甚至完整的子系统，都以 plugin 形式挂载在运行中的 kernel 上。本教学只使用 Python 标准库实现最小版本，每章专注加入一项机制。

后续所有机制都创建在同一个基础上：把请求送给模型，再接收回复。对话历史要转成模型能理解的格式，工具要等模型发出调用后才运行，prompt 也必须在请求送出前组装完成。

Mini-dsh 因此需要一个统一的模型调用方式。最简单的做法，是在每个需要模型的地方直接 import 某家 provider 的 SDK。

问题是，provider 的格式会因此渗入整套 harness。prompt 组装程序会依赖它的请求格式，log 会保存它返回的对象，compaction 也会绑定它的 role 命名。未来一旦更换 provider，这些模块都必须一起修改。

另一个重点是流式输出。模型通常会逐段产生回复。如果调用端一定要等到全部完成才能返回，用户就只能空等，log 也无法实时记录中间过程。

因此，本章要回答的问题是：为什么 Mini-dsh 的核心只认自己的 `Message` 格式，并通过可替换的 Model seam 调用模型？

这套 harness 关心的是模型调用周边的机制，不应依赖背后究竟是哪家模型。无论更换哪个 provider，核心收发的都是同一种 `Message`，provider 因此成为可以独立替换的组件。本章会先创建：

1. Mini-dsh 自己的 **`Message` 格式**，不与任何 provider 绑定。
2. **Model seam** 的调用规范：一个普通 callable，接收一组消息，先产生多个 chunk 事件，最后再产生一则完整消息。
3. 可直接运行的 **Scripted stand-in**，用预先设置的回复实现这套规范。
4. 稳定、可测试的分块规则，让流式输出从一开始就是真正的运行模式，而不是事后模拟。

这个 seam 也奠定了整份教学的测试方式。stand-in 内部只有一列预先排好的回复，完全不读取请求内容。因此，后续每章的测试都能在离线环境中运行，不需 API key，结果也可重现。

---

## 核心机制

本章包含三个核心组件，各自放在独立文件中：

- **`Message`**（`message.py`）：用于模型交互的统一消息格式。它是一个冻结的 dataclass，只包含 `role` 和 `content`。
- **Model seam**：一套调用约定，而不是基类。`model(messages)` 会先 yield `("chunk", str)`，最后再 yield `("message", Message)`。
- **`ScriptedModel`**（`standin.py`）：Model seam 的第一个实现，按顺序返回预先设置的回复。

整套 harness 都使用同一种 `Message` 格式：

```python
@dataclass(frozen=True)
class Message:
    role: str  # "user" | "assistant" | "tool"
    content: str
```

这个 dataclass 设为不可变，因为消息一旦写入历史，就不应再被事后修改。它也不绑定任何 provider：核心只处理自己的消息格式，至于如何转成特定服务的请求格式，则由 adapter 负责。

三种 role 已足以描述 harness 内的所有交互：用户输入、模型回复，以及工具结果。后续章节会在消息外层加入事件类型，而不是不断扩充消息本身的字段。

Model seam 本身只是一套调用约定。任何 callable 只要接收一组消息，并 yield 出这两种事件，就能当作模型使用。因此，adapter 可以是函数、闭包，也可以像 stand-in 一样实现成对象：

```python
class ScriptedModel:
    def __init__(self, responses):
        self._queue = list(responses)

    def __call__(self, messages):
        """The Model seam: yields ("chunk", str)... then ("message", Message)."""
        text = self._queue.pop(0)
        for piece in _chunks(text):
            yield ("chunk", piece)
        yield ("message", Message(role="assistant", content=text))
```

`ScriptedModel` 不会读取传入的 `messages`。无论输入是什么，它都按照测试中预先排好的顺序回复，因此第一次调用一定取得第一则回复，测试结果也能稳定重现。

每则回复在送出最终消息前，会先切成大小相近的 chunk 逐段产生：

```python
def _chunks(text, n=3):
    size = max(1, -(-len(text) // n))
    return [text[i : i + size] for i in range(0, len(text), size)]
```

一次调用从头到尾穿过 seam，长这样：

```text
check                                  ScriptedModel(["Hello, reader."])
  │
  │  model([Message("user", "hi")])
  ├──────────────────────────────────►  pop the next canned response
  │                                     (the request is never read)
  │   ("chunk", "Hello")   ◄──┐
  │   ("chunk", ", rea")   ◄──┼─────── split into fixed-size chunks
  │   ("chunk", "der.")    ◄──┘
  │   ("message", Message("assistant", "Hello, reader."))
  │◄──────────────────────────────────
```

真正重要的是这两个阶段。chunk 用于实时流式输出，最后的 `Message` 则保留完整内容，供后续写入记录。第 02 章会将两者记成不同的事件类型；第 04 章的 loop 则会原样转送，不额外缓冲。

### 改了什么

第 00 章是整条 Carry-forward 链的起点，后续每一章都会沿用以下内容：

- `src/` 从本章开始累积：`message.py` 和 `standin.py` 是实现，`test.py` 是离线测试。
- 第 01 章会完整沿用这份 `src/`，只加入 kernel。之后也维持相同方式，让相邻章节的 diff 聚焦在新机制上。
- 目前还没有 plugin、log 或 agent。Model seam 现阶段只定义调用方式，后续章节才会加入实际调用它的组件。

---

## 对照真正的 dsh

以下链接皆指向研究版本 [`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca)。Model seam 在真正的 dsh 里的位置是 [`packages/llm`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/llm)。

| Mini-dsh | 真正的 dsh | 说明 |
| --- | --- | --- |
| `Message` | [`packages/llm/llm/src/types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/llm/llm/src/types.ts) | 消息类型由 llm seam 定义，同样不绑定 provider。`ToolSchema`（第 333 行）也在这个文件中，工具之后会通过它向模型描述自己。Mini-dsh 则只需要一个 dataclass。 |
| Model seam 的约定 | [`packages/llm/llm/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/llm/llm/src/index.ts)：`LlmAdapter`（第 180 行） | 真正的 seam 同样采用流式接口：`stream(options)` 会返回 `AsyncIterable<StreamChunk>`。Mini-dsh 使用「先产生 chunk，最后产生完整消息」的简化版本。 |
| 摆在 seam 后面的 `ScriptedModel` | [`packages/llm/llm/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/llm/llm/src/index.ts)：`LlmRuntime`、`ctx.llm`（第 284 行） | adapter 通过 `ctx.llm.registerAdapter(providers, adapter)` 注册，换掉的时候调用端不会察觉。stand-in 就是 mini-dsh 的第一个 adapter。 |
| 调用 `model(messages)` 的检查 | [`packages/core/agent-loop/src/agent.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-loop/src/agent.ts) | 真正用它的是 loop：先 `ctx.llm.prepareCall()`，再 `preparedCall.stream(request)`（第 345、449 行）。第 04 章会让 mini 也有同一个调用端。 |
| 先一串 chunk，最后一则消息 | [`packages/core/session/src/types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session/src/types.ts)（第 236 行） | 等到 log 出现（第 02 章），流式输出的这两个阶段就变成 session 事件类型 `assistant/chunk` 和 `assistant/message`。 |

真正的 llm seam 还提供以下功能：

- **一个会做路由的 adapter registry。** `ctx.llm` 同时放着好几个 adapter，用 provider 名字当键；至于某一套部署要拿哪个 model 当默认，本身又是一个 plugin（[`packages/core/agent-default-model`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-default-model)，`ctx.agentDefaultModel`）。mini 这边一次只有一个 callable，要到第 10 章才会给 seam 一个 service 的位置。
- **流式接口上可以挂 middleware。** 一道 `llm/stream` waterfall（`index.ts` 第 51 到 60 行）让 plugin 可以包住或旁观每一次 model 调用，而重试会以 `llm/retry` 这种 session 事件出现在 log 里。
- **连接不同 provider 的 adapter。** 内置的 [`llm-deepseek`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/llm/llm-deepseek/src/index.ts) 和 [`llm-pi-ai`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/llm/llm-pi-ai/src/index.ts) 会处理各家服务的协议。本教学不实现完整 adapter；唯一接触实际 API 的地方，是第 04 章之后 `demo.py` 中约 20 行的 Anthropic 格式转换，而且与离线核心分开。
- **折成一份，而不是拆成三份。** 真正的 dsh 通常会把一个能力拆成三边：一个软件包定义接口，一些软件包提供它，一些软件包使用它。llm seam 把定义端和使用端折进同一个软件包，因为使用它的就是 agent loop 本身，不是一组随时可以换掉的 tool。第 10 章会把这个 seam 和这条折叠规则一起重现一遍。

---

## 常见失败模式

- **provider 格式渗入核心。** 如果直接保存 provider 返回的 JSON，log、compaction 和 prompt 组装都会依赖它的字段与 role 命名。更换 provider 时，这些模块就得一起修改。统一使用 `Message`，可以把格式转换集中在 adapter 中。
- **可修改的消息会让历史失真。** 第 02、03 章把已写入的消息视为既成事实；如果字段还能任意修改，记录与模型实际看到的内容可能逐渐分歧，而且没有修改痕迹。
- **只返回完整文本，就无法真正实现流式输出。** 模型产生回复时，调用端没有任何内容可以先显示，log 也无法记录 chunk。回复越长，用户等待的空窗就越明显。
- **只有 chunk，会迫使每个调用端自行重组完整内容。** loop、log 和监看程序都要各自拼接一次，也可能得到不一致的结果。最后的 `("message", Message)` 让完整消息只需在 seam 中组装一次。
- **用基类定义 seam，会增加不必要的耦合。** adapter 必须继承 harness 的类，普通函数或包装另一个模型的闭包也难以直接使用。改用调用约定后，只要能 yield 指定事件的 callable 都能成为模型实现。

---

## 动手验证

[`src/`](src/) 是 Carry-forward 这条链的起点，每个文件都是新的：

- [`message.py`](src/message.py)：冻结的 `Message` dataclass。
- [`standin.py`](src/standin.py)：`ScriptedModel` 与固定规则的分块函数。
- [`test.py`](src/test.py)：确认所有 chunk 拼接后等于最终消息、流式事件确实包含多个区块，而且默认回复会依序取用。

```bash
python sections/00-setup/src/test.py   # offline check, no key
```

本章已创建 Model seam，但还没有机制会主动调用它，因此不提供 `demo.py`。第一个在线示例会在第 04 章加入 agent loop 后出现。

---

## 参考资料

- [learn-agent-memory](https://github.com/hardness1020/learn-agent-memory)：本章的检查惯例（离线、不用 key、每次结果都一样）就是从这个 tutorial 系列沿用过来的。
