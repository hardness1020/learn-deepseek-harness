<!-- source: README.md @ 3705bd7 -->

# 04 · Agent loop

[English](README.md) | [繁體中文](README.zh-TW.md) | 简体中文

> agent loop 负责接收输入、调用模型，并写入回复。但如果 loop 自己也保留一份对话历史，系统就会出现第二个真相来源。因此 loop 只负责推进流程，不另外保存历史。

第 00 到 03 章已经建立了 session log。它能推导模型历史、记录流式输出 chunk，也支持 compaction，但目前还没有组件会主动推进对话。所有检查都得手动接续对话，自己把每条消息 append 到 log。

现在缺少的是 agent loop：它接收用户输入、调用模型、记录回复，并持续运行到任务完成。Mini-dsh 将一次完整交互称为 **turn**，而一个 turn 可以包含一个或多个 **step**。

最简单的做法，是在内存中维护一份消息列表。用户和模型的每则消息都追加进去，每次调用模型时再送出整份列表。

但这份列表会与 session log 重复。第 03 章的 compaction 会更新 surface，但内存列表不会自动同步；程序中断后，它也会直接消失。恢复 session 时，系统还必须另外重建这份列表，并确保它与模型当时看到的内容完全一致。

因此，本章要回答的问题是：为什么每个 step 都必须重新组装 prompt，并从 log 重新推导历史？

log 本来就是唯一可持久的状态，loop 应该以它为依据，而不是自己维护另一份真相。具体规则如下：

1. 一个 **turn** 由多个 **step** 组成。`send()` 会持续运行 step，直到某个 step 返回明确的结束原因。
2. 每个 step 都会从 session log 重新推导模型历史，通过 Model seam 调用模型，并将所有 chunk 与最终消息写回 log。
3. turn 和 step 的边界都会写成 log 事件：`turn/start`、`step/start`、`step/end` 和 `turn/end`。它们不会进入模型历史，但能完整描述运行过程。
4. 每个 step 都会记录 `request/header`，说明这次请求实际送出的内容。
5. Agent 对象不保存任何持久状态。只要重放相同的 log，新创建的 Agent 就能从同一位置继续运行。

---

## 核心机制

`agent_loop.py` 包含三个核心组件：

- **`Agent.send()`**：负责一个 turn。它会先 append 用户消息与 `turn/start`，接着持续运行 step，直到取得明确的结束原因，最后 append `turn/end`。
- **`Agent._step()`**：负责一个 step。它会推导历史、记录请求内容、调用模型、逐段接收回复并写回 log，最后记下结束原因。
- **`AgentRegistry`**：由 plugin 提供的 `agents` service，与第 02 章的 `sessions` service 是同一套做法。

turn 的主体是一个 while 循环，是否结束由 step 的返回值决定：

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

重新推导历史的关键就在 step 中：

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

`derive_messages()` 会在写入 `step/start` 后运行。step 本身不保存历史，只在每次模型调用前从 log 取得当下的推导结果。

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

上面每一行都对第 02 章的 session 运行一次 `append()`。边界标记与 header 只写入 log（`surface_op` 为 `None`），不会出现在模型历史；`derive_messages()` 仍只会取得真正的对话消息。

因为每个 step 都重新读取 log，其他机制不需额外同步。若在两个 turn 之间进行 compaction（第 03 章），下一个 `request/header` 中的消息数量自然会减少。loop 不需要接收 compaction 通知，因为它每次看到的本来就是最新投影。

模型调用中途失败时，log 可能留下 `step/start`、`request/header` 和几个尚未完成的 chunk。这些 chunk 不会进入 surface，因此下次推导出的模型历史仍然完整，不需要额外修补。离线测试会刻意让模型在产生 chunk 后失败，验证这项行为。

恢复运行也很直接。Agent 只持有 session、Model seam callable，以及表示是否正在运行 turn 的 `status`。将 log 重放到新的 session，再创建新的 Agent，后续 turn 便能从相同状态继续。

本章先完成「重新推导历史」；「重新组装 prompt」则会在第 08 章加入 system prompt 后补齐。目前 Mini-dsh 送出的请求只有推导出的消息。

### 改了什么

与第 03 章相比：

- `kernel.py`、`message.py`、`session_log.py`、`standin.py` 都完整沿用；`agent_loop.py` 是唯一添加的源文件，因此与第 03 章相比，diff 只包含本章添加的机制，不包含其他改动。
- 第 03 章测试中手动推进流程的 `stream_turn()` 辅助函数已移除；现在测试直接通过 `send()` 验证真正的 loop。
- 目前每个 turn 只会运行一个 step，因为尚未加入工具，每个 step 都以 `"completed"` 结束。第 05 章会利用同一个循环，在工具运行后继续下一个 step。
- 本章首次实际调用模型，因此加入 `demo.py`，使用相同 loop 连接 Anthropic API。

---

## 对照真正的 dsh

以下链接皆指向研究版本 [`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca)。loop 本身位于 [`packages/core/agent-loop`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-loop)，对外那层 registry 则在 [`packages/core/agent`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent)。

| Mini-dsh | 真正的 dsh | 说明 |
| --- | --- | --- |
| `Agent.send()` 和 `_step()` | [`packages/core/agent-loop/src/agent.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-loop/src/agent.ts)：`ReactLoopAgent` | 真正在跑的那一套是 `kick` -> `turn()` -> `preStep()` -> `step()` -> `buildRequest()`；每个 step 都从 log 重新推导出消息，也重新组一次 prompt。 |
| `AgentRegistry`，也就是 `agents` service | [`packages/core/agent/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent/src/index.ts)：`AgentRegistry` | `ctx.agents` 里放的是一个个 `Agent` handle，从外面看不到里面；真正在跑的那个 loop，是由一个可以换掉的 factory（`setFactory()`）做出来的，而这个 factory 由 `dsh-agent-loop` 注册。 |
| `status`：`"idle"` 或 `"running"` | [`packages/core/agent/src/runtime-types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent/src/runtime-types.ts)：`AgentStatus` | 一样是这两个状态，只是挂在一个宽得多的 `Agent` seam 接口上（`cancel`、`send`、`followup`、`steer`、`inject`）。 |
| `turn/start`、`step/start`、`step/end`、`turn/end`、`request/header` 这几行 | [`packages/core/agent-loop/src/agent.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-loop/src/agent.ts) | turn/step 这套持久的词汇，就是 loop 自己 append 进去的 session 事件，跟这里一模一样；`agent/*` 那条 bus 上只有生命周期、inbox 和拦截点。 |
| `_step()` 里那次 Model seam 调用 | [`packages/core/agent-loop/src/agent.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-loop/src/agent.ts)：`ctx.llm.prepareCall()` | 真正的请求会走 llm 这个 capability seam，回应一个 chunk 一个 chunk 传回来；这个 seam 本身是第 10 章的机制。 |

真正的 agent loop 还提供以下功能：

- **step 丰富得多。** 真正的 step 在开始跟 model 要回应之前，会先认领 inbox、组出 system prompt、投影出 runtime context，再跑一次 `agent/pre-step` 和 `agent/request` 这两个 waterfall。mini 的 step 只有推导，加上把回应收回来；剩下的由第 05 章到 09 一个一个补上。
- **step 有更多种结束方式。** 真正的 step 可以用 `completed` 结束（没有 tool 调用）、用 `max-tokens` 结束（一旦是它就会一直留着），或是回 `null`（跑过 tool，再绕一圈）。而一个 turn 要收掉，得同时满足两件事：有结束理由，而且在 `agent/turn-stopping` 重新确认过之后 `inbox.nextStep` 是空的。tool 的结果上如果标了 `concludesTurn`，turn 会提早结束。在第 05 章之前，mini 只有一条分支。
- **整个 loop 都可以换掉。** `Agent` 是一个 seam 接口，`ReactLoopAgent` 只位于软件包内部，外面只能通过 factory 拿到它，所以要换掉整个 loop，不必动到任何一个拿着 agent handle 的地方。
- **生命周期事件都位于 bus。** `agent/created`、`agent/disposed`、`agent/status` 与 inbox 事件可供外部实时追踪进度，另有取消 token 贯穿整个流程。Mini-dsh 则以 log 中的边界标记记录生命周期，第 06 章才会加入 scheduler 取消机制。

---

## 常见失败模式

- **缓存消息列表会形成第二个真相来源。** compaction 更新 surface 后，缓存内容不会自动同步；重放 session 时也无法保证一致。每个 step 都从 log 推导，就不需要维护额外副本。
- **step 中途失败不需要修补历史。** 失败的 step 可能只有 `step/start` 和几个 chunk，没有 `step/end`。由于 chunk 只进 log，下一次推导仍会得到干净的消息历史。
- **一个 turn 不一定只有一次模型调用。** 若流程固定为「送出一次、回复一次、立即结束」，工具运行后就无法回到模型。while-step 结构与明确结束原因，让第 05 章可以直接加入工具分支。
- **缺少 `request/header` 就无法确认模型实际收到什么。** header 会把每个 step 的请求摘要写入 log。测试在两个 turn 间运行 compaction，并直接从记录确认消息数量依序为 1、3、2。
- **同一份 log 同时运行两个 turn 会造成事件交错。** turn 尚未完成时再次调用 `send()` 会直接失败。真正的 dsh 会把新消息放进 inbox，等到 step 边界再认领；第 07 章会实现这项机制。
- **缺少边界标记会让重放无法判断运行状态。** 没有 `turn/start` 和 `step/end`，就无法分辨 turn 是正常结束还是中途失败。这些标记是正式数据，不只是调试输出。

---

## 动手验证

[`src/`](src/) 延续第 03 章，并加入：

- [`agent_loop.py`](src/agent_loop.py)（添加）：带着 `send()` 和 `_step()` 的 `Agent`、`AgentRegistry`，还有提供 `agents` service 的 plugin。
- [`test.py`](src/test.py)：确认 turn 事件依序写入 log；`request/header` 的数字证明每个 step 都会重新推导，跨过 compaction 后仍然正确（1、3、2）；重放 log 并创建新 Agent 后可以接续运行；step 中途失败不会污染下次推导；turn 运行期间再次调用 `send()` 会遭拒。
- [`demo.py`](src/demo.py)（添加）：第一个在线示例。同一个 loop，把真正的 Anthropic API 接到 Model seam 上，跑几个写好的 turn，中间插一次 compaction，最后把 log 中的完整运行记录印出来。SDK 和 mini-Message 之间的转换只位于这里。

```bash
python sections/04-agent-loop/src/test.py   # offline check, no key
```

在线示例需要根目录的 `requirements.txt` 和一把 key；没有设置 key 时会自动跳过：

```bash
pip install -r requirements.txt             # anthropic + python-dotenv
cp .env.example .env                        # then set ANTHROPIC_API_KEY
python sections/04-agent-loop/src/demo.py
```

---

## 参考资料

- [`docs/subsystems/core.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/subsystems/core.md)：dsh 自己写的文档，讲 agent 和 agent-loop 这两个软件包。
- [`docs/agent-lifecycle.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/agent-lifecycle.md)：turn 和 step 的生命周期，从 kick 一路到 turn 结束。
