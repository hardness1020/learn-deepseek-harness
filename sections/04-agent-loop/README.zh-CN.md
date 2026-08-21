<!-- source: README.md @ e5a7812 -->

# 04 · Agent loop

[English](README.md) | [繁體中文](README.zh-TW.md) | 简体中文

> 这个 loop 身上没有任何值得存下来的状态。每个 step 它都重读一次 log，问 model 一次，再把答案写回去。

Section 00 到 03 做出了一份 session log：它能推导出 model 看到的历史，能一个 chunk 一个 chunk 接住回应，也能 compact。但没有东西在推动它。到目前为止，每一个检查都是自己手动把对话一条一条接下去，每一条消息都自己 append 进去。

还缺的是那台机器：接住用户打的字，调用 model，把回应记下来，一直重复到事情做完为止。这台机器就是 agent loop，mini-dsh 把它跑一次叫做一个 **turn**，一个 turn 由一个或多个 **step** 组成。

最直觉的做法，是在内存里留一份活的消息列表。用户说了什么就 append 进去，model 回了什么也 append 进去，每次要问 model 就把整份列表交出去。不用推导，不用投影，就是一个会越长越大的 Python list。

但那份列表等于把真相又抄了一份。compaction（Section 03）会在它背后偷偷改 surface。程序一崩，那份列表就没了。要接续一个 session，得先把它重建出来，然后祈祷重建的内容跟 model 当初真的看到的一样。

所以：为什么每一个 step 都要重新组一次 prompt、重新推一次历史？

因为 log 本来就是唯一持久的状态，loop 应该靠着它，而不是跟它抢着当真相。要做到这件事，loop 必须：

1. 把一个 **turn** 跑成一连串 **step**：`send()` 会一直往下 step，直到某个 step 交出的是一个结束理由，而不是还有事要做。
2. 每个 step 一开始就先从 session log 重新推导出 model 的历史，一次都不留旧的，接着通过 Model seam 调用 model 一次，把吐回来的每个 chunk 和最后那条消息都 append 回去。
3. 把 turn 和 step 的边界写成 log 事件（`turn/start`、`step/start`、`step/end`、`turn/end`），这些只进 log，这样光看 log 就知道整个故事。
4. 每个 step 都写一行 `request/header`，记下这次送出去了什么，这样 log 自己就能证明 model 当时被喂了什么。
5. Agent 这个对象上不留任何持久的东西：任何一个 Agent 只要接到同一份 log，都能接得一模一样，所以接续就等于把 log 重放一遍，再配一个新的 Agent。

---

## Mechanism

一个新文件 `agent_loop.py`，里面三个零件：

- **`Agent.send()`**：一个 turn。先 append 用户的消息和 `turn/start`，然后一直 step，直到某个 step 交出结束理由，最后 append `turn/end`。
- **`Agent._step()`**：一个 step。推导历史，记下自己准备送出去的东西，调用 model 并把回应一段一段收回来，全部 append 回去，再交代自己是怎么结束的。
- **`AgentRegistry`**：由 plugin 提供的 `agents` service，跟 Section 02 的 `sessions` service 是同一套做法。

一个 turn 就是一个 while 循环，离开的条件就是 step 给的答案：

```python
def send(self, text):
    """One turn: the user's message in, steps until one ends with a reason."""
    if self.status == "running":
        raise RuntimeError("agent is mid-turn; the log allows one story at a time")
    self.status = "running"
    try:
        self.session.append("user/message", {"content": text})
        self.session.append("turn/start", {})
        while self._step() is None:
            pass
        self.session.append("turn/end", {})
    finally:
        self.status = "idle"
```

而设计问题的答案就在 step 里面，一行就讲完了：

```python
def _step(self):
    """One step: re-derive history, one model call, append it all back."""
    self.session.append("step/start", {})
    messages = self.session.derive_messages()  # re-derived, never cached
    self.session.append("request/header", {"messages": len(messages)})
    for kind, value in self.model(messages):
        if kind == "chunk":
            self.session.append("assistant/chunk", {"text": value})
        else:
            self.session.append("assistant/message", {"content": value.content})
    reason = "completed"
    self.session.append("step/end", {"reason": reason})
    return reason
```

`derive_messages()` 是在 step 里面跑的，跑在 `step/start` 写进去之后。step 自己不持有历史，它只是跟 log 借一份，而且借来只够用在一次 model 调用上。

下面是一段对话的第二个 turn，log 是这样记的：

```text
send("and now?")
  │  10  user/message      {"content": "and now?"}
  │  11  turn/start
  │
  ├─ step ─────────────────────────────────────────────
  │  12  step/start
  │      derive_messages()          ◄── read the log, fresh
  │  13  request/header    {"messages": 3}
  │  14  assistant/chunk   ┐
  │  15  assistant/chunk   │ streamed through the Model seam
  │  16  assistant/chunk   ┘
  │  17  assistant/message {"content": "Now this."}
  │  18  step/end          {"reason": "completed"}
  ├─ reason is "completed" ► leave the loop
  │
  │  19  turn/end
```

每一行都是在 Section 02 那个 session 上做一次 `append()`。那些边界标记和 header 都只进 log（`surface_op` 是 `None`），所以 model 永远看不到它们；`derive_messages()` 拿回来的还是只有真正的消息。

因为 step 每次都重读 log，其他 Mechanism 不用特别做什么就搭得起来。在两个 turn 之间做一次 compact（Section 03），下一行 `request/header` 记下的数字就会变小：step 推导出来的是压缩过的视角，因为 log 现在就是投影成那样。没有人去通知 loop 发生过 compaction。也不需要。

出事的时候，这一招一样划算。model 调用跑到一半死掉，log 上会留下 `step/start`、一行 `request/header`、几个没下文的 chunk，然后就没了。不需要任何修补步骤：chunk 只进 log，所以下一次推导出来的历史本来就是干净的，而 Offline check 就是故意在 chunk 还在往回吐的时候把 model 弄死，用这个来证明。

接续的时候也划算。Agent 身上就只有一个 session、一个 Model seam 的 callable，还有一个 `status` 标志，而那个标志只表示“现在正在一个 turn 中间”。把 log 重放进一个新的 session，交给一个全新的 Agent，接下来那个 turn 写进去的每一行，会跟原本那个 Agent 会写的一模一样。

有一件事要老实说：这个 section 做到的是重新推导历史，设计问题里“重新组 prompt”那一半还在后面。在 Section 08 把 system prompt 做出来之前，mini 送出去的请求就只有推导出来的消息而已。

### 改了什么

跟 Section 03 比起来：

- `kernel.py`、`message.py`、`session_log.py`、`standin.py` 都原封不动搬过来；`agent_loop.py` 是唯一新增的源文件，所以跟 03 的 diff 就是这个 section 的 Mechanism，没有别的。
- 03 的检查里那个要手动一步步推的 `stream_turn()` 辅助函数不见了。现在 loop 是真的被测到的代码，检查是通过 `send()` 来推动它。
- 今天这个 while-step 循环每个 turn 只会跑一次，因为现在还没有 tool，每个 step 都以 `"completed"` 结束。这个循环的形状和结束理由，就是 Section 05 要接进来的地方。
- 这是第一个会碰到 model 的 Section，所以 `demo.py` 出现了：同一个 loop，只是把真正的 Anthropic API 接到 Model seam 上（ADR 0001）。

---

## In real dsh

所有指过去的链接都固定在 Studied version [`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca) 上。loop 本身住在 [`packages/core/agent-loop`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-loop)，对外那层 registry 则在 [`packages/core/agent`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent)。

| Mini-dsh | 真正的 dsh | 说明 |
| --- | --- | --- |
| `Agent.send()` 和 `_step()` | [`packages/core/agent-loop/src/agent.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-loop/src/agent.ts)：`ReactLoopAgent` | 真正在跑的那一套是 `kick` -> `turn()` -> `preStep()` -> `step()` -> `buildRequest()`；每个 step 都从 log 重新推导出消息，也重新组一次 prompt。 |
| `AgentRegistry`，也就是 `agents` service | [`packages/core/agent/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent/src/index.ts)：`AgentRegistry` | `ctx.agents` 里放的是一个个 `Agent` handle，从外面看不到里面；真正在跑的那个 loop，是由一个可以换掉的 factory（`setFactory()`）做出来的，而这个 factory 由 `dsh-agent-loop` 注册。 |
| `status`：`"idle"` 或 `"running"` | [`packages/core/agent/src/runtime-types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent/src/runtime-types.ts)：`AgentStatus` | 一样是这两个状态，只是挂在一个宽得多的 `Agent` seam 接口上（`cancel`、`send`、`followup`、`steer`、`inject`）。 |
| `turn/start`、`step/start`、`step/end`、`turn/end`、`request/header` 这几行 | [`packages/core/agent-loop/src/agent.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-loop/src/agent.ts) | turn/step 这套持久的词汇，就是 loop 自己 append 进去的 session 事件，跟这里一模一样；`agent/*` 那条 bus 上只有生命周期、inbox 和拦截点。 |
| `_step()` 里那次 Model seam 调用 | [`packages/core/agent-loop/src/agent.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-loop/src/agent.ts)：`ctx.llm.prepareCall()` | 真正的请求会走 llm 这个 capability seam，回应一个 chunk 一个 chunk 传回来；这个 seam 本身是 Section 10 的 Mechanism。 |

真正的 agent loop 在这个 section 的 Mechanism 之上，还多做了这些：

- **step 丰富得多。** 真正的 step 在开始跟 model 要回应之前，会先认领 inbox、组出 system prompt、投影出 runtime context，再跑一次 `agent/pre-step` 和 `agent/request` 这两个 waterfall。mini 的 step 只有推导，加上把回应收回来；剩下的由 Section 05 到 09 一个一个补上。
- **step 有更多种结束方式。** 真正的 step 可以用 `completed` 结束（没有 tool 调用）、用 `max-tokens` 结束（一旦是它就会一直留着），或是回 `null`（跑过 tool，再绕一圈）。而一个 turn 要收掉，得同时满足两件事：有结束理由，而且在 `agent/turn-stopping` 重新确认过之后 `inbox.nextStep` 是空的。tool 的结果上如果标了 `concludesTurn`，turn 会提早结束。在 Section 05 之前，mini 只有一条分支。
- **整个 loop 都可以换掉。** `Agent` 是一个 seam 接口，`ReactLoopAgent` 只住在包内部，外面只能通过 factory 拿到它，所以要换掉整个 loop，不必动到任何一个拿着 agent handle 的地方。
- **生命周期都在 bus 上。** `agent/created`、`agent/disposed`、`agent/status` 加上 inbox 那几个事件，让在旁边实时盯着的人跟得上进度，另外还有一个取消用的 token 贯穿全部。mini 这边是靠写进 log 的那些边界标记来说故事；取消要等到 Section 06 的 scheduler 才会出现。

---

## Failure modes

- **缓存一份消息列表，等于把真相抄了第二份。** 历史一旦存在一份活的列表里，其他每个 Mechanism 都会变成同步问题：compaction 在它背后改 surface，重放 session 的时候根本不会理它。每个 step 都从 log 推导，就代表从头到尾没有东西需要同步。
- **step 中途崩掉，不需要任何修补。** 死掉的 step 会留下一个没有 `step/end` 的 `step/start`，可能还有几个没下文的 chunk。因为 chunk 只进 log，下一次推导出来的东西本来就是干净的；检查会先让 model 吐一个 chunk，再把它弄死，然后证明下一个 turn 送出去的历史刚刚好正确。
- **一个 turn 不等于一次 model 调用。** 如果把“送出去、回一句、结束”写死，tool 跑完之后就没有地方可以绕回来。有一个 while-step 的形状，加上一个讲明白的结束理由，Section 05 才能在不动 turn 的情况下把 tool 加进来。
- **没有 `request/header`，“model 看到了 X”就只是猜的。** 这一行 header 把每个 step 送出去了什么，直接写进 log 里。检查会在两个 turn 之间做一次 compact，然后直接从 log 上读数字：1，然后 3，compact 之后是 2。不用去翻 stand-in 的内部，看记录就好。
- **同一份 log 上跑两个 turn，故事会交错在一起。** 一个 turn 还在跑的时候又调用 `send()`，会直接丢出异常，而不是把两套 turn/step 标记编在同一条时间在线。真正的 dsh 会把那条消息排进 inbox，等到 step 的边界再认领；那是 Section 07 的 Mechanism。
- **少了边界标记，重放就分不清楚了。** 没有 `turn/start` 和 `step/end` 这两行，重放的人分不出来一个 turn 是好好结束的，还是跑到一半崩掉的。这些边界是数据，不是随手打印出来 debug 用的东西：有它们，log 才是一个故事，而不是一堆散掉的消息。

---

## 跑跑看

[`src/`](src/) 把 03 原封不动搬过来，再加上：

- [`agent_loop.py`](src/agent_loop.py)（新增）：带着 `send()` 和 `_step()` 的 `Agent`、`AgentRegistry`，还有提供 `agents` service 的 plugin。
- [`test.py`](src/test.py)：整个 turn 的故事会照顺序落在 log 上；`request/header` 上的数字证明每一步都重新推导，跨过一次 compaction 也一样（1、3、2）；把 log 重放一遍再配一个新的 Agent，接下去写的东西一模一样；step 中途崩掉，下一次推导还是干净的；turn 中途再调用一次 `send()` 会被拒绝。
- [`demo.py`](src/demo.py)（新增）：第一个 Live demo。同一个 loop，把真正的 Anthropic API 接到 Model seam 上，跑几个写好的 turn，中间插一次 compaction，最后把 log 自己的故事打印出来。SDK 和 mini-Message 之间的转换只住在这里（ADR 0001）。

```bash
python sections/04-agent-loop/src/test.py   # offline check, no key
```

Live demo 需要根目录的 `requirements.txt` 和一把 key；没有 key 的话，它会安静地跳过：

```bash
pip install -r requirements.txt             # anthropic + python-dotenv
cp .env.example .env                        # then set ANTHROPIC_API_KEY
python sections/04-agent-loop/src/demo.py
```

---

## 出处

- [`docs/subsystems/core.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/subsystems/core.md)：dsh 自己写的文档，讲 agent 和 agent-loop 这两个包。
- [`docs/agent-lifecycle.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/agent-lifecycle.md)：turn 和 step 的生命周期，从 kick 一路到 turn 结束。
