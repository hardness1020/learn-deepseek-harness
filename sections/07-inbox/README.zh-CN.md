<!-- source: README.md @ 3705bd7 -->

# 07 · Inbox

[English](README.md) | [繁體中文](README.zh-TW.md) | 简体中文

> 用户不应该等 agent 完全停下来才能补充输入。但若在 step 中途直接写入 log，记录就会误以为模型看过它实际上没收到的内容。因此，新输入要先进 inbox，再于 step 边界套用。

第 06 章的 agent 只有一个输入入口。`send()` 收到消息后，会等整个 turn 运行完才返回；如果 turn 尚未结束就再次调用 `send()`，系统会直接抛出例外。

真实交互不会这么完整切割。用户可能在看到工具结果后立即调整方向，背景工作也可能在 turn 中途完成，并希望将结果加入下一次请求。另一方面，真正的后续问题应该打开新 turn，而不是强行加入当前任务。

最直觉的做法，是将新输入直接以 `user/message` 写入 log。但 step 中途时，当前请求早已完成历史推导并送出。此时写入的消息会让 log 误以为模型已经看过，重放时也会重建出一个从未发生的请求。此外，新输入常常来自 worker thread 中的工具实现，不应直接与 loop 争用 log 写入权。而且，一个列表也无法表达输入是要介入当前 turn，还是打开下一个 turn。

因此，本章要回答的问题是：为什么 inbox 需要两种投递目标，并且只在 step 边界认领？

新输入只能先排入队列，不能在到达当下直接套用；而它应该介入当前 turn 或打开新 turn，必须由发送端决定。inbox 因此遵守以下规则：

1. 只投递，不立即套用：新文本先进待处理列表，不直接写入 log。插入操作有锁保护，任何线程都能安全调用。
2. 两种目标代表两种意图：`next-turn` 会单独打开新 turn；`next-step` 则补充目前正在进行的工作。由发送端决定该使用哪一种。
3. 只在 step 边界认领：待处理输入必须等到下一次从 log 推导 request 时，才转成 `user/message`。如此一来，log 不会误记模型从未收到的内容。
4. 每个 prompt 各自使用一个 turn：打开 turn 时，系统会取得所有 `next-step` 输入，以及最多一则 `next-turn` prompt，因此排队中的 prompt 不会被合并。
5. 有新的介入时不能结束 turn：step 即使已有结束原因，也会再次检查 `next-step`；只要还有输入，就在同一个 turn 中继续下一个 step。
6. 取消时清空对应输入：`cancel()` 会清空 inbox，避免已取消的 turn 因先前排入的消息再次启动。

---

## 核心机制

本章添加 `inbox.py`，并让 loop 的所有输入先经过 inbox：

- **`Inbox`**：在锁的保护下维护两份有顺序的待处理列表。`insert(target, message)` 可由任何线程安全加入输入；`claim(target)` 会取走所有 `next-step`，若正要打开 turn，则再取一则 `next-turn` prompt。
- **`send(text, target, wakeup)`**：唯一的投递入口。`followup()`、`steer()`、 `inject()` 是它的三个现成组合。
- **`_drain()`**：负责持续驱动 turn，直到没有排队中的 prompt。一次唤醒就能依序处理当时累积的所有 prompt。
- **收 turn 前的再确认**：一个 turn 要结束，条件是某个 step 带着结束原因收尾，而且就在那一刻 `next-step` 是空的。

这三个现成组合的差别，只在投到哪里：

```python
def followup(self, text):
    """Queue a prompt that gets a turn of its own."""
    self.send(text, "next-turn", True)

def steer(self, text):
    """Steer the nearest step: input for the work already underway."""
    self.send(text, "next-step", True)

def inject(self, text):
    """Park model-facing context for the next step, without waking."""
    self.send(text, "next-step", False)
```

`send()` 会先将消息放入 inbox，只有 agent 闲置时才唤醒 drain loop。若 turn 正在运行，工具实现或 bus listener 送来的消息只会排队，等 loop 到达下一个 step 边界再认领。

```python
def _turn(self):
    self.session.append("turn/start", {})
    target = "next-turn"  # only a turn's first boundary consumes a queued prompt
    while True:
        reason = self._step(target)
        target = "next-step"
        if reason == "aborted":
            break  # cancelled: pending input is already gone
        if reason is not None and not self.inbox.has("next-step"):
            break  # fresh steering spends another step in this turn
    self.session.append("turn/end", {})
```

进到 `_step(target)` 之后，第一件事就是认领，位置刚好就在第 04 章本来就会把所有东西重新推导一次的地方：

```python
self.session.append("step/start", {})
for message in self.inbox.claim(target):
    self.session.append("user/message", message)
messages = self.session.derive_messages()  # re-derived, never cached
```

放进来随时都行；认领只发生在边界：

```text
insert: any thread, any time          claim: loop thread, boundaries only

steer("s") ──► next-step [ s ]        every step: all of next-step
followup("B") ──► next-turn [ B ]     turn-opening step: plus one prompt

turn A    step 1             step 2             step 3
          claim: [A]         claim: [s]         claim: []
          user/message A     user/message s     model -> "done"
          model -> calls     model -> "ok"      completed, next-step
          tool rows   ▲      completed, but     empty: turn closes
                      │      next-step refilled
          s inserted here,   mid-step: another
          mid-step: parked   step, same turn
turn B    step 1  claim: [B]              one queued prompt, one turn
```

以下是实际运行时的 log。`read` 的实现送出一则介入消息，并排入两则后续 prompt，这些操作都来自 worker thread：

```text
send("read my note")
  │   0  turn/start
  │   1  step/start
  │   2  user/message   "read my note"       ◄ claimed at the boundary
  │   3  request/header
  │   4  assistant/message {"tool_calls": [read]}
  │   5  tool/call     read
  │        ...the body steers and queues two prompts, mid-step...
  │   6  tool/result   read
  │   7  step/end      {"reason": null}
  │   8  step/start
  │   9  user/message   "while reading: also check the dates"  ◄ the steer
  │  10  request/header
  │  14  assistant/message
  │  15  step/end      {"reason": "completed"}
  │  16  turn/end                             ◄ next-step empty: close
  │  17  turn/start                           ◄ first queued prompt
  │  19  user/message   "queued: summarize everything"
  │  26  turn/end
  │  27  turn/start                           ◄ second queued prompt
  │  29  user/message   "queued: then say goodbye"
  │  36  turn/end
```

介入消息会在下一个边界以 seq 9 进入当前 turn。两则后续 prompt 不会合并，而是各自打开 turn，因此一次唤醒共运行三个 turn。任何时间重建历史时，log 中的每笔 `user/message` 都确实曾送给模型。

### 改了什么

与第 06 章相比：

- `kernel.py`、`message.py`、`scheduler.py`、`session_log.py`、`standin.py`、 `tools.py` 完整沿用。`inbox.py` 是唯一的新源代码文件；其他改动都是把 inbox 接进 `agent_loop.py`，因此与第 06 章相比，diff 只包含本章添加的机制，不包含其他改动。
- `agent_loop.py`：`send()` 改成走 inbox，不再自己追加 `user/message`，并且多了 `target` 和 `wakeup` 两个参数，还有 `followup()` / `steer()` / `inject()` 三个现成组合。那个「agent 正在跑 turn」的 RuntimeError 没了：turn 中途送进来的东西会排队，不会丢出例外。现在一次 `send()` 会把排队的 prompt 全跑完才回来。 `cancel()` 也会把 inbox 清空。
- log 的长相变了：`user/message` 现在落在认领它的那个 step 里面，接在 `step/start` 后面，而不是在 `turn/start` 之前。输入只有被认领，才进得了对话记录。
- `demo.py`：在线示例在闲着的时候用 `inject()` 先把 context 摆着，接着在 turn 中途从 bus 上的 listener 介入，并排一则后续 prompt，所以一次 send 就能在实际模型上把三种投递方式都演一遍。

---

## 对照真正的 dsh

以下链接皆指向研究版本 [`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca)。inbox 位于 agent 这个软件包里，认领的位置则在 loop 里： [`packages/core/agent`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent)。

| Mini-dsh | 真正的 dsh | 说明 |
| --- | --- | --- |
| `inbox.py` 里的 `Inbox` | [`packages/core/agent/src/inbox.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent/src/inbox.ts)：`Inbox` | 每个 agent 两份有顺序的待处理列表；`InboxTarget = 'next-turn' \| 'next-step'` 声明在 [`types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent/src/types.ts) 里。 |
| `claim(target)` | [`inbox.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent/src/inbox.ts)：`Inbox.claim` | 规则一样：先拿走 next-step 的全部输入，如果这个边界要开一个 turn，再多拿一则排队的 prompt。它被写成 loop 在 step 边界上的操作，不是给 plugin 用的扩充点。 |
| `send(text, target, wakeup)` | [`packages/core/agent/src/runtime-types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent/src/runtime-types.ts)：`Agent.send` | 统一的投递入口；`followup`、`steer`、`inject` 是参数固定好的别名，跟 mini 那三行一模一样。 |
| 收 turn 前的再确认 | [`packages/core/agent-loop/src/agent.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-loop/src/agent.ts) | 一个 turn 要收掉，条件是某个 step 带着结束原因收尾，而且 `inbox.nextStep` 是空的；这个确认排在 `agent/turn-stopping` 这个 serial hook 之后，让它有最后一次介入的机会。 |
| `cancel()` 清空 inbox | [`runtime-types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent/src/runtime-types.ts)：`CancelOptions` | `cancel(cause)` 会把排队的和介入用的东西一起清掉，除非 `keepInbox` 要求留着；`clear()` 先清 next-step，再清 next-turn。 |
| `_drain()` | [`agent.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-loop/src/agent.ts)：`kick()` | 驱动的那一段会先把排队的工作跑完才收工，而 `running` 会横跨连续好几个排队的 turn，所以它不能拿来证明某个 turn 还开着。 |

真正的 inbox 还提供以下功能：

- **撑得过重启。**每一次变动都会追加一笔范式的 `agent/inbox/spliced` session 事件，而内存里那两份列表，是回头读这些记录重建出来、只重放一次的投影，所以待处理的输入撑得过一次重启。mini 的 inbox 只活在内存里：它的 log 只有一个写入者（第 06 章），放进来的动作又发生在工作线程上，所以只有被认领的消息才进得了 log。
- **待处理消息有 id，也能修改。** 真正的 dsh 会为消息分配 id，在认领前可通过 `replace()` 或 `remove()` 修改；每次变动都会实时发布 `agent/inbox/inserted`、`claimed` 或 `discarded` 事件。Mini-dsh 没有为待处理消息命名，加入后只能等待认领。
- **认领和 step 之间有一个 hook。**`agent/pre-step` 这个 waterfall 可以否决一个提议中的 step，也可以改写刚认领到的那一批消息；被否决的 step 会把它认领到的消息就地结束，然后一个 step 都不跑就把 turn 收掉。mini 这边只要认领到，就一定会进去。
- **唤醒有一道闩。**真正的唤醒跟放入是分开的：唤醒如果落在一段被中止的活动里，会改指向 `next-turn` 并且被闩住，等驱动的那一段收敛到闲置状态再重放一次。mini 的唤醒就一行，「闲着就 drain」，之所以安全，是因为只有驱动的那条线程会看到闲置这件事。
- **按介入键的是人。**在真正的 dsh 里，介入通常来自 UI，而 UI 超出本教学的实现范围； mini 是从 tool 的实现和 bus 上的 listener 去按 `steer()` 和 `followup()`， `inject()` 则是从脚本按的。

---

## 常见失败模式

- **输入到达时立即写入，会让 log 与实际请求不一致。** step 中途时，当前 request 已完成历史推导；此时添加消息，重放后会像是模型曾看过它。固定在边界认领，才能确保 log 只记录真正送出的内容。
- **单一列表无法区分两种意图。** 后续问题应开新 turn，介入消息则应影响目前工作。发送端最清楚自己的意图，因此必须明确指定目标。
- **一次认领所有 prompt 会把多段对话合并。** turn 开始时最多取得一则 `next-turn`，所以三则排队 prompt 会产生三个 turn 与三个回答，而不是被合成一个过长输入。
- **结束 turn 前若不再次检查，最后到达的介入可能永远等待。** 收尾前查看 `next-step`，只要有新输入，就在原 turn 中再运行一个 step。
- **取消后保留旧 inbox，可能让已取消工作再次启动。** `cancel()` 会先清空两份列表；取消后才送达的消息则正常排队，形成一次新的运行。
- **worker thread 直接写入 user 事件会破坏单一写入者原则。** inbox 的插入只修改受锁保护的内存，只有 loop 线程会在认领后将消息写入 log。

---

## 动手验证

[`src/`](src/) 延续第 06 章，并加入：

- [`inbox.py`](src/inbox.py)（新的）：`Inbox`，一把锁后面两份待处理列表； `insert`、`claim`、`has`、`clear`。
- [`agent_loop.py`](src/agent_loop.py)：`send()` 改走 inbox，多了 `target` 和 `wakeup`；`followup()`、`steer()`、`inject()`；drain 的 loop；每个 step 边界上的认领；收 turn 前的再确认；`cancel()` 会清空 inbox。
- [`test.py`](src/test.py)：tool 的实现介入它自己所在的那个 turn，又排了两则 prompt，每一则各拿到一个 turn；闲着时 `inject()` 不会动到 log，要等下一次唤醒先来认领；介入如果落在一个已经完成的 step 期间，那个 turn 会再多开一个 step；cancel 会把所有待处理的东西丢掉，而下一次 send 从干净的状态重新开始。
- [`demo.py`](src/demo.py)：在线示例在闲着的时候先把 context 摆进去，接着在 turn 中途从 bus 上介入、排一则后续 prompt，最后把 log 自己记下的这三种投递方式印出来。

```bash
python sections/07-inbox/src/test.py    # offline check, no key
```

在线示例需要根目录的 `requirements.txt` 和一把 key；没有设置 key 时会自动跳过：

```bash
pip install -r requirements.txt         # anthropic + python-dotenv
cp .env.example .env                    # then set ANTHROPIC_API_KEY
python sections/07-inbox/src/demo.py
```

---

## 参考资料

- [`docs/agent-lifecycle.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/agent-lifecycle.md)： dsh 自己画的一个 turn，连认领的位置和 inbox 事件都画进去了。
- [`docs/subsystems/core.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/subsystems/core.md)： Agent 对外的接口、三个现成的别名，还有把 inbox 当成一整套投递词汇来介绍的那一段。
- [`.agents/notes/implemented/architecture/2026-07-30-followup-enqueue-and-owned-runs.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/.agents/notes/implemented/architecture/2026-07-30-followup-enqueue-and-owned-runs.md)：那份设计笔记，讲的是为什么 `followup()` 不返回任何 handle。
