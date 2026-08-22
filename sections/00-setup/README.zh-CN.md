<!-- source: README.md @ 5566322 -->

# 00 · Setup

[English](README.md) | [繁體中文](README.zh-TW.md) | 简体中文

> harness 里到处都要问 model。要是每个地方都各自调用某一家 provider 的 SDK，它的格式就会跟着跑进 prompt、跑进 log、跑进 loop。所以核心只认一套自己的消息格式，provider 收在一个随时可以换掉的调用后面。

DeepSeek Harness（dsh）是一套货真价实的 agent harness：一个大型的 TypeScript 代码库，里面的 tool、prompt，甚至一整个子系统，都是 plugin，挂在一个正在跑的 kernel 上。这份 tutorial 只用 Python 标准库，把它缩成一个最小版本，一个 Section 只加一个 Mechanism。

这些 Mechanism 全都绕着同一件事转：问 model，然后等它回话。历史要整理成 model 读得下去的样子，tool 要等 model 开口才会被叫起来，prompt 要先组好才喂得进去。

所以 mini-dsh 也得有一套“怎么问”的办法。最直觉的做法，是 import 某一家 provider 的 SDK，哪里要问就在哪里调用它。

这么做，等于让那一家 provider 渗进整套 harness 的每个角落。组 prompt 的代码会照着它的请求格式写，log 里存的是它返回的对象，compaction 认得的是它那套 role 名称；哪天想换一家，这三个地方都得跟着改。

而且 model 不会一次把话讲完。它是一边想一边把字吐出来，所以调用端要是非等到整段讲完不可，等的这段时间就什么都端不出去，log 也要拖到最后才有东西可以记。

所以：为什么 mini-dsh 的核心只认自己那套 Message 格式，而且一定要隔着一个随时可以换掉的 Model seam 才去问 model？

因为这套 harness 真正要讲的，是 model 调用外面那一圈东西，而那一圈东西都不该管回答的是谁家的 model。不管后面换成谁，送进去的都是同一套 Message，收回来的也是同一套 Message，provider 就变成一个随时拔得下来的零件。要做到这件事，Section 00 得先：

1. 给 mini-dsh 一套自己的 **Message 格式**，跟真正的 dsh 一样不绑任何 provider，任何一家的传输格式都别想渗进核心。
2. 把 **Model seam** 定下来：它就是一个普通的 callable，收下一串消息，先一块一块吐出 chunk 事件，最后用一条消息收尾，不多不少就一条。
3. 附上一个 **Scripted stand-in**，照着这套约定讲话，让这个 seam 一出现就有一个真的跑得动的实现。
4. 每一条响应都照同一套规则切成 chunk，所以从第一天起，这里的流式输出就是真的。

有了这个 seam，这份 tutorial 要怎么检查自己也就跟着定了。stand-in 手上是一条排好顺序的队列，里面全是写死的响应，它从来不看送进来的请求，所以每个 Section 的检查都能离线跑，不用 key，每次跑出来的东西一模一样。

---

## Mechanism

三个零件，一个文件放一个：

- **`Message`**（`message.py`）：跟 model 一来一往的每一条消息都长这样，一个冻结的 dataclass，只有 `role` 和 `content` 两个字段。
- **Model seam**：它不是一个类，而是一套调用惯例。`model(messages)` 先 yield 出 `("chunk", str)` 事件，最后 yield 一个 `("message", Message)`。
- **`ScriptedModel`**（`standin.py`）：seam 的第一个实现，一条队列，里面装着写死的响应。

这套 harness 的词汇，全部就是这个 Message：

```python
@dataclass(frozen=True)
class Message:
    role: str  # "user" | "assistant" | "tool"
    content: str
```

冻结，是因为一条消息记的是已经说出口的话，不是还能改的草稿。不绑 provider，是因为核心不该管回答的是谁家的 model；要把它翻成某一家的传输格式，那是 adapter 的事，而核心里面一个 adapter 也没有。

三种 role 就把 harness 里所有的来回都包完了：用户说了什么、model 说了什么、tool 回了什么。后面的 Section 会在这些消息外面加事件类型，而不是往消息里面加字段。

seam 本身就是一套调用惯例。任何一个 callable，只要收下一串消息、再 yield 出这两种事件，它就算是一个 model；所以 adapter 可以是一个函数，可以是一个闭包，也可以像 stand-in 那样是一个对象：

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

每一条响应在送出收尾那条消息之前，会先切成几块一样大的 chunk 送出去：

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

- **一个会做路由的 adapter registry。** `ctx.llm` 同时放着好几个 adapter，用 provider 名字当键；至于某一套部署要拿哪个 model 当默认，本身又是一个 plugin（[`packages/core/agent-default-model`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-default-model)，`ctx.agentDefaultModel`）。mini 这边一次只有一个 callable，要到 Section 10 才会给 seam 一个 service 的位置。
- **流式输出上可以挂 middleware。** 一道 `llm/stream` waterfall（`index.ts` 第 51 到 60 行）让 plugin 可以包住或旁观每一次 model 调用，而重试会以 `llm/retry` 这种 session 事件出现在 log 里。
- **真的照着各家协议讲话的 adapter。** 内置的 provider [`llm-deepseek`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/llm/llm-deepseek/src/index.ts) 和 [`llm-pi-ai`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/llm/llm-pi-ai/src/index.ts) 直接照各家自己的协议讲话。Ceiling：mini-dsh 不会做这种 adapter；它唯一碰到真 API 的代码，是 Live demo 在 `demo.py` 里那段大约 20 行、把消息翻成 Anthropic 格式的东西（Section 04 以后），而且它待在离线核心外面。
- **折成一份，而不是拆成三份。** 真正的 dsh 通常会把一个能力拆成三边：一个包定义接口，一些包提供它，一些包使用它。llm seam 把定义端和使用端折进同一个包，因为使用它的就是 agent loop 本身，不是一组随时可以换掉的 tool。Section 10 会把这个 seam 和这条折叠规则一起重现一遍。

---

## Failure modes

- **某一家 provider 的格式会一路蔓延出去。** provider 回什么就存什么，log 里躺的就是它那份 JSON，compaction 认得的是它那套 role 名称，组 prompt 的代码是照着它的请求格式写的。换一家，就得三个地方一起动刀。核心只认一套 Message 格式，翻译这件事就被关在 adapter 里面，哪里都跑不掉。
- **消息改得动，历史就会被人偷偷改掉。** Section 02 和 03 把记下来的消息当成已经发生的事实，而 compaction 想缩掉 model 看到的东西，也只能走 log 这条路。要是一条消息的字段随手就能重新指派，这两件事都会落空：记录和 model 眼前看到的会越差越远，而且改过的痕迹一点都不留。
- **seam 只回一整段写完的文本，流式输出就没了。** model 还在写的时候，调用端没有东西可以先端出去，Section 02 也没有 chunk 事件可以记，回答一长，看起来就像整个卡住。有了 chunk，第一批字一出来，harness 手上就有东西可以往下传。
- **只丢 chunk、不丢收尾那条消息，拼回原文的工作就落到每一个调用端头上。** loop 自己接一份，log 自己接一份，旁边盯着看的代码也各接各的，而每一份都可能在接缝上悄悄接错。最后那一个 `("message", Message)`，让这份留得住的记录只在 seam 这里拼一次。
- **把 seam 定成一个基类，等于把整套 harness 拖进每一个 adapter 里。** 要继承，provider 就得连 harness 那个类先假设好的东西一起吃下去；而一个普通的函数，或是一个包住另一个 model 的闭包，都会被挡在门外。改成一套调用惯例，要求就只停在“会 yield 出这两种事件”，换一个 model 也就是传一个不一样的 callable 进去而已。

---

## 跑跑看

[`src/`](src/) 是 Carry-forward 这条链的起点，每个文件都是新的：

- [`message.py`](src/message.py)：冻结的 `Message` dataclass。
- [`standin.py`](src/standin.py)：`ScriptedModel`，还有它那个每次切法都一样的切块函数。
- [`test.py`](src/test.py)：证明 seam 的约定站得住脚：所有 chunk 接起来刚好等于最后那条消息的内容，流式输出不是只吐一整块，队列也照顺序回答。

```bash
python sections/00-setup/src/test.py   # offline check, no key
```

Model seam 在这里已经有了，但还没有哪个 Mechanism 在驱动它，所以没有 `demo.py`。第一个 Live demo 要等 Section 04 的 agent loop 才会出现。

---

## 出处

- [learn-agent-memory](https://github.com/hardness1020/learn-agent-memory)：这个 Section 的检查惯例（离线、不用 key、每次结果都一样）就是从这个 tutorial 系列沿用过来的。
