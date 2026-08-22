<!-- source: README.md @ 8c7e193 -->

# 00 · Setup

[English](README.md) | [繁體中文](README.zh-TW.md) | 简体中文

> 哪里需要答案，就直接用一家 provider 的 SDK，那家厂商的形状最后会跑进 prompt、跑进 log、跑进 loop。核心只认一种消息形状、只留一个可以换掉的调用，provider 就会待在边界上。

DeepSeek Harness（dsh）是一套货真价实的 agent harness：一个大型的 TypeScript 代码库，里面的 tool、prompt，甚至一整个子系统，都是 plugin，挂在一个正在跑的 kernel 上。这份 tutorial 只用 Python 标准库，把它重建成一个最小的版本，一个 Section 只加一个 Mechanism。

这些 Mechanism 全都绕着同一件事转：跟 model 要一个回答。历史是为 model 推导出来的，tool 是 model 叫起来的，prompt 是为 model 组出来的。

所以重建这件事需要一个“怎么问”的办法，而最直觉的办法，就是 import 一家 provider 的 SDK，哪里需要答案就在哪里调用它。

这么一来，那家 provider 会渗进整套 harness。它的请求格式会跑到组 prompt 的地方，它的响应对象会跑进 log，它的 role 名称会跑进 compaction；想换一家 provider，每个沾到的地方都得动手改。

而且答案不是一次到齐的。model 是一边写一边把字吐出来，所以调用端要是非等到一整串完整的文本不可，等的这段时间就什么都端不出来，log 也要等到最后才有东西可以记。

所以：为什么 mini-dsh 的核心只讲自己的 Message 形状，而且要通过一个可以换掉的 Model seam 去问 model？

因为这套 harness 真正要讲的，是 model 调用外面那一整圈事情，而那些事情都不该管回答的是谁家的 model。送进去是同一种形状，拿回来也是同一种形状，provider 就变成一个插上去就能用的零件。要做到这件事，Section 00 得先：

1. 给 mini-dsh 一套自己的 **Message 形状**，跟真正的 dsh 一样不绑任何 provider，这样就不会有哪家厂商的传输格式渗进核心。
2. 把 **Model seam** 定下来：一个普通的 callable，收下消息列表，先流式吐出 chunk 事件，最后刚好收在一条消息上。
3. 附上一个 **Scripted stand-in**，照这份约定讲话，让这个 seam 一出现就有一个真的跑得动的实现。
4. 每一条响应都用同一套规则切成 chunk，这样流式输出从第一天就是真的。

这份 tutorial 怎么检查自己，是从这个 seam 长出来的。stand-in 照着一条排好顺序的队列回答，里面全是写死的响应，而且它从来不去看送进来的请求，所以每个 Section 的检查都能离线跑、不用 key，每次跑出来的东西都一模一样。

---

## Mechanism

三个零件，一个文件放一个：

- **`Message`**（`message.py`）：跟 model 来回交换的东西都长这个形状，一个冻结的 dataclass，只有 `role` 和 `content`。
- **Model seam**：它不是一个类，是一套调用惯例。`model(messages)` 先 yield 出 `("chunk", str)` 事件，最后 yield 一个 `("message", Message)`。
- **`ScriptedModel`**（`standin.py`）：seam 的第一个实现，一条队列，里面装着写死的响应。

Message 的形状就是这套系统的全部词汇：

```python
@dataclass(frozen=True)
class Message:
    role: str  # "user" | "assistant" | "tool"
    content: str
```

之所以冻结，是因为一条消息记录的是已经说出口的话，不是还能改的草稿。之所以不绑 provider，是因为核心不该在意回答的是谁家的 model；把这个形状翻成某家厂商的传输格式，那是 adapter 的事，而核心里面一个 adapter 也没有。

三种 role 就涵盖了这套 harness 会有的所有来回：用户说了什么、model 说了什么、tool 回了什么。后面的 Section 会在这些消息外面加事件类型，而不是往消息里面加字段。

seam 本身就是一套调用惯例。任何一个 callable，只要收下一份消息列表、再 yield 出这两种事件，它就算是一个 model；所以 adapter 可以是一个函数，可以是一个闭包，也可以像 stand-in 那样是一个对象：

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

`messages` 传进来了，却从来没被读过。不管你问什么，stand-in 都照着脚本、照着顺序回答，而整份脚本就摊在写它的那个检查里：第一条响应永远对应第一次调用。

每一条响应在送出最后那条消息之前，会先切成几块固定大小的 chunk 流式发出去：

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

这两个阶段比 stand-in 本身重要得多。chunk 是当场流过去的那一段；最后那条 `Message` 才是留得住的记录，而且它每次都会把完整的文本再讲一遍。到了 Section 02，log 会把这两种东西存成不同的事件类型；到了 Section 04，loop 会把两种都往下传，中间不做任何缓冲。

### 改了什么

Section 00 前面没有东西，所以这一格记的是后面每个 Section 都会继承的起点：

- `src/` 从这里开始：`message.py` 和 `standin.py` 是源代码，`test.py` 是检查。
- Carry-forward 这条规则从这里开始。Section 01 会把这份 `src/` 原封不动抄过去，只加上它的 kernel，所以相邻两个 Section 的 diff 刚好就是一个 Mechanism，多的没有。
- 这里的东西还不知道 plugin、log 或 agent 是什么。seam 现在只是一套调用惯例，等着有人来调用它。

---

## In real dsh

所有指过去的链接都固定在 Studied version [`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca) 上。Model seam 在真正的 dsh 里的位置是 [`packages/llm`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/llm)。

| Mini-dsh | 真正的 dsh | 说明 |
| --- | --- | --- |
| `Message` | [`packages/llm/llm/src/types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/llm/llm/src/types.ts) | 词汇类型归 llm seam 管，跟我们一样不绑 provider；`ToolSchema`（第 333 行）也在这个文件里，后面 tool 就是靠它向 model 自我介绍的。Mini-dsh 的整套词汇只有一个 dataclass。 |
| Model seam 的约定 | [`packages/llm/llm/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/llm/llm/src/index.ts)：`LlmAdapter`（第 180 行） | 那边的 seam 一样是流式的：`stream(options)` 返回一个 `AsyncIterable<StreamChunk>`。mini 这边“先 chunk、最后一条消息”的惯例是同一个想法，只是把最后那条消息讲明白了。 |
| 摆在 seam 后面的 `ScriptedModel` | [`packages/llm/llm/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/llm/llm/src/index.ts)：`LlmRuntime`、`ctx.llm`（第 284 行） | adapter 通过 `ctx.llm.registerAdapter(providers, adapter)` 注册，换掉的时候调用端不会察觉。stand-in 就是 mini-dsh 的第一个 adapter。 |
| 调用 `model(messages)` 的检查 | [`packages/core/agent-loop/src/agent.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-loop/src/agent.ts) | 真正用它的是 loop：先 `ctx.llm.prepareCall()`，再 `preparedCall.stream(request)`（第 345、449 行）。Section 04 会让 mini 也有同一个调用端。 |
| 先一串 chunk，最后一条消息 | [`packages/core/session/src/types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session/src/types.ts)（第 236 行） | 等到 log 出现（Section 02），流式输出的这两个阶段就变成 session 事件类型 `assistant/chunk` 和 `assistant/message`。 |

真正的 llm seam 在这个 Section 的 Mechanism 之上，还多做了这些：

- **一个会做路由的 adapter registry。** `ctx.llm` 同时放着好几个 adapter，用 provider 名字当键；要挑出一次部署的默认 model，本身又是一个 plugin （[`packages/core/agent-default-model`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-default-model)， `ctx.agentDefaultModel`）。mini 这边一次只有一个 callable，要到 Section 10 才会给 seam 一个 service 的位置。
- **流式输出上可以挂 middleware。** 一道 `llm/stream` waterfall（`index.ts` 第 51 到 60 行）让 plugin 可以包住或旁观每一次 model 调用，而重试会以 `llm/retry` 这种 session 事件出现在 log 里。
- **真的会讲厂商协议的 adapter。** 内置的 provider [`llm-deepseek`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/llm/llm-deepseek/src/index.ts) 和 [`llm-pi-ai`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/llm/llm-pi-ai/src/index.ts) 会讲各家厂商自己的协议。Ceiling：mini-dsh 不会去重建任何一个讲厂商协议的 adapter；它唯一碰到真 API 的代码，是 Live demo 在 `demo.py` 里那段大约 20 行、把消息翻成 Anthropic 格式的东西（Section 04 以后），而且它待在离线核心外面。
- **折成一份，而不是拆成三份。** 真正的 dsh 通常会把一个能力拆成三边：一个包定义接口，一些包提供它，一些包使用它。llm seam 把定义端和使用端折进同一个包，因为使用它的就是 agent loop 本身，不是一组随时可以换掉的 tool。Section 10 会把这个 seam 和这条折叠规则一起重建一遍。

---

## Failure modes

- **某一家厂商的响应形状会四处蔓延。** provider 回什么就存什么，于是 log 里放的是它那份 JSON，compaction 学到的是它的 role 名称，组 prompt 的代码则是照着它的请求格式写的。等到要换一家 provider，这三个地方都得改。只有一种 Message 形状，翻译这件事就被关在一个 adapter 里面。
- **消息可以改，历史就能被就地改写。** Section 02 和 03 把记下来的消息当成已经发生的事实，而 compaction 想缩掉 model 看到的东西，也只能走 log 这条路。要是一条消息的字段可以随手重新指派，这两件事就都不成立了：记录和 model 看到的画面会越飘越开，而且改过的痕迹一点都不留。
- **seam 只回一整串写完的文本，流式输出就被丢掉了。** model 在写的时候，调用端没有东西可以端出来，Section 02 也没有 chunk 事件可以记，而一段长一点的回答看起来就像卡住了。有了 chunk，只要第一批字节出来，harness 手上就有东西可以往下传。
- **只有 chunk、没有收尾的那条消息，重组的工作就落到每一个调用端头上。** loop、log，还有每一段在旁边看着的代码，都得自己接出一份自己的副本，而每一份都可能在接缝的地方悄悄接错。最后那一个 `("message", Message)` 让这份留得住的记录只在 seam 这里组一次。
- **把 seam 定义成一个基类，等于把整套 harness 拖进每一个 adapter 里。** 要继承，就代表 provider 得一并吃下 harness 那个类已经先假设好的东西；而一个普通的函数，或是一个包住另一个 model 的闭包，就再也不算数了。改成一套调用惯例，要求就只停在“会 yield 出这两种事件”，换一个 model 也就是传一个不一样的 callable 进去而已。

---

## 跑跑看

[`src/`](src/) 是 Carry-forward 这条链的起点，每个文件都是新的：

- [`message.py`](src/message.py)：冻结的 `Message` dataclass。
- [`standin.py`](src/standin.py)：`ScriptedModel`，还有它那个每次切法都一样的切块函数。
- [`test.py`](src/test.py)：seam 的约定站得住脚：所有 chunk 接起来刚好等于最后那条消息的内容，流式输出不是只吐出一整块，队列也照顺序回答。

```bash
python sections/00-setup/src/test.py   # offline check, no key
```

Model seam 在这里已经有了，但还没有哪个 Mechanism 在驱动它，所以没有 `demo.py`。第一个 Live demo 要等 Section 04 的 agent loop 才会出现。

---

## 出处

- [learn-agent-memory](https://github.com/hardness1020/learn-agent-memory)：这个 Section 的检查惯例（离线、不用 key、每次结果都一样）就是从这个 tutorial 系列沿用过来的。
