<!-- source: README.md @ 55e829b -->

# 00 · Setup

[English](README.md) | [繁體中文](README.zh-TW.md) | 简体中文

> 每个 Section 收尾，都要检查程序内容，用结果说话。可是真的 model，同一道题问两次就给你两个答案，所以这里先让脚本顶上。

这份 tutorial 要重建一套 harness，而它里面每一个 Mechanism 都绕着一次 model 调用转：历史是为 model 推导出来的，tool 是 model 叫起来的，prompt 是为 model 组出来的。14 个 Section，每一个的结尾都有跑得起来的检查，必须证明自己那个 Mechanism 真的会动。

最直觉的做法，是把一个真的 API 摆在这些检查后面：问一次真的 model，再对它的回答做断言。

但真的 model 要 key、要网络、要花钱，而且同样的输入喂进去，吐回来的东西每次都不一样。断言挂掉的时候，你分不出是代码坏了，还是 model 今天心情不同；会因为两种原因失败的检查，就算过了也证明不了什么。更麻烦的是，这种不稳定出现在最不该出现的地方：这里要研究的是 model 外面那套 harness，从来不是 model 本身。

所以：为什么每个 Section 的检查都得离线、对着 stand-in 跑？

因为检查存在的理由，就是证明这个 Section 的 Mechanism；而 model 刚好是唯一一个会动、行为却不归这个 Mechanism 管的零件。把 model 锁住，每次检查都会得到固定结果：不用 key、不用网络，跑几次输出都一模一样。要做到这件事，Section 00 得先：

1. 给 mini-dsh 一套自己的 **Message 形状**，跟真正的 dsh 一样不绑任何 provider，这样就不会有哪家厂商的传输格式渗进核心。
2. 把 **Model seam** 定下来：一个普通的 callable，收下消息列表，先流式吐出 chunk 事件，最后刚好收在一条消息上。
3. 附上一个 **Scripted stand-in**，照这份约定讲话：一条照顺序排好的队列，里面是写死的响应，从来不去看送进来的请求。
4. 每一条响应都用同一套规则切成 chunk，这样流式输出从第一天就是真的，而且每次跑出来一模一样。

---

## Mechanism

三个零件，一个文件放一个：

- **`Message`**（`message.py`）：跟 model 来回交换的东西都长这个形状，一个冻结的 dataclass，只有 `role` 和 `content`。
- **Model seam**：它不是一个类，是一套调用惯例。`model(messages)` 先 yield 出 `("chunk", str)` 事件，最后 yield 一个 `("message", Message)`。
- **`ScriptedModel`**（`standin.py`）：seam 的离线实现，一条队列，里面装着写死的响应。

Message 的形状就是这套系统的全部词汇：

```python
@dataclass(frozen=True)
class Message:
    role: str  # "user" | "assistant" | "tool"
    content: str
```

之所以冻结，是因为一条消息记录的是已经说出口的话，不是还能改的草稿。之所以不绑 provider，是因为核心不该在意回答的是谁家的 model；把这个形状翻成某家厂商的传输格式，那是 adapter 的事，而核心里面一个 adapter 也没有。

stand-in 是 seam 的第一个实现，而且刻意做得很被动：

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

`messages` 传进来了，却从来没被读过。不管你问什么，stand-in 都照着脚本、照着顺序回答。要是它会去比对请求内容，比对规则就会一条一条长出来，规则之间又互相牵扯，多到自己变成第二个 model，然后这个 model 又得再测一次。改用照顺序排的队列，整份脚本就摊在写它的检查里：第一条响应永远对应第一次调用。

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

这两个阶段比 stand-in 本身重要得多。chunk 是当场流过去的那一段；最后那条 `Message` 才是留得住的记录，而且它每次都会把完整的文本再讲一遍。到了 Section 02，log 会把这两种东西存成不同的事件类型；到了 Section 04，loop 会把两种都往下传，中间不做任何缓冲。因为 stand-in 从第一天就在做流式输出，后面没有任何一个 Section 需要拿真的 API 来第一次面对流式输出。

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

- **拿真的 model 来跑，每次检查都像在赌运气。** 同样的输入，吐回来的东西每次都不一样，于是断言不是写得很模糊（`"contains a word"`），就是时好时坏。stand-in 每次跑出来的输出一模一样，所以检查可以直接断言确切的内容，而且说到做到。
- **会去读请求的 stand-in，迟早变成第二个 model。** 比对规则会越积越多，规则之间又互相牵扯，很快这个替身就聪明到足以出错。队列的约定是故意做笨的：第一条响应对第一次调用，而且整份脚本就明明白白摊在检查里。
- **一口气把整段吐完的 stand-in，等于把流式输出往后拖。** 如果只 yield 最后那条消息，处理 chunk 的代码要到 Section 04 才第一次跑起来，而且是对着真的 API 跑，出事还重现不了。每次切法都一样的 chunk，让流式输出从第一次检查开始就是真的。
- **对着 stand-in 的内部下断言，检查到的只是测试用的架子。** 伸手去摸 `_queue`，会让检查绑死在一个真 adapter 根本没有的东西上。这条规则贯穿全部 14 个 Section：只对穿过 seam 的东西下断言，等 log 出现以后就对着 log 下，永远不要对着 stand-in 下。
- **脚本用完了，就要明明白白地失败。** 多调用一次 model，就会从空队列里 pop，然后直接抛错，所以问过头的检查会失败，而不是默默重用一个刚好会过的答案。

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
