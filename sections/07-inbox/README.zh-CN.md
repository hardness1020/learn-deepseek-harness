<!-- source: README.md @ 55e829b -->

# 07 · Inbox

[English](README.md) | [繁體中文](README.zh-TW.md) | 简体中文

> 没人想等机器安静下来才开口。直接写进 log，等于宣称 model 读过那些它根本没收到的话，所以输入就改成等在 step 之间。

Section 06 的 agent 到现在还是只有一扇门。`send()` 收下一条消息，跑完一整个 turn 才回来；turn 还没结束就再调用一次 `send()`，会直接抛出异常。用户想说的每一句话，都得等机器安静下来才轮得到。

真实世界的输入不会按时间表来。用户盯着 tool 的结果一条一条刷过去，想改方向就是想现在改，不是等它一路走错走到底才改。一个跑完的后台工作，想把结果塞进下一次 request。而一个真正的后续问题，应该等自己的那个 turn，不要硬闯进正在跑的这一个。

最直觉的做法，是把进来的文本直接当成一条 `user/message` 追加到 log 里。但 step 跑到一半的时候，正在飞的那次 request 早就把历史推导完了：新加的这一条会宣称 model 看过它其实没收到的字，重放的时候还会重建出一次根本没送出去过的 request。更麻烦的是，送东西进来的常常是某个 tool 的实现，而且跑在工作线程上，Section 06 又已经把 log 定成只有一个写入者。而且单一份列表讲不出一条消息到底想干什么：是要加入正在跑的这份工作，还是要自己开一个 turn。

所以：为什么 inbox 要有两个投递目标，而且只在 step 的边界认领？

因为进来的输入只能先投递、不能当场套用，而且投到哪里要由送件的人决定。要做到这件事，inbox 必须：

1. 只投递，不套用：进来的文本先进待处理列表，不进 log。放进去这个动作有锁保护，任何线程都能做，而且不会留下任何一条记录。
2. 两个目标对应两种意图：`next-turn` 是值得单独开一个 turn 的 prompt； `next-step` 是给正在跑的那份工作的输入。只有送件的人知道自己要的是哪一种。
3. 只在 step 的边界认领：待处理的输入，要等到“下一次 request 从 log 重新推导出来”的那个位置才变成 `user/message`，这是 Section 04 的规则。这样对话记录就不会宣称 model 看过它没看过的字。
4. 每一条 prompt 各拿一个 turn：开启 turn 的那次认领，会拿走所有待处理的 `next-step` 输入，外加最多一条排队中的 prompt，所以排队的 prompt 永远不会被并在一起。
5. 有新的介入就不能收掉 turn：一个带着结束原因收尾的 step，会再去看一次 `next-step`；只要那里有东西，就在同一个 turn 里再多跑一个 step。
6. 跟着它瞄准的那个 turn 一起消失：`cancel()` 会把 inbox 清空，所以被中止的 turn 不会拿取消之前排的输入重新开跑。

---

## Mechanism

一个新文件 `inbox.py`，再把 loop 的大门改道，让它走这里：

- **`Inbox`**：一把锁后面放两份有顺序的待处理列表。`insert(target, message)` 让任何线程都能把输入放进来；`claim(target)` 会清空 `next-step`，如果这个边界正要开一个 turn，就再多拿走刚好一条 `next-turn` 的 prompt。
- **`send(text, target, wakeup)`**：唯一的投递入口。`followup()`、`steer()`、 `inject()` 是它的三个现成组合。
- **`_drain()`**：负责驱动的那一段。它一个 turn 接一个 turn 跑，跑到没有排队的 prompt 才闲下来，所以唤醒一次，后面排队的每一条 prompt 都轮得到。
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

`send()` 先把东西放进去，只有在 agent 闲着的时候才去唤醒 drain 的那个 loop。 turn 跑到一半时，tool 的实现或 bus 上的 listener 调用进来，就只是排队而已：驱动的那条线程正忙在某个 step 里面，等之后的某个边界再来认领。

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

进到 `_step(target)` 之后，第一件事就是认领，位置刚好就在 Section 04 本来就会把所有东西重新推导一次的地方：

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

下面是一次真的运行，照 log 记下来的样子。`read` 的实现介入了一次，又排了两条后续 prompt，全都是从它那条工作线程发出来的：

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

那条介入在 seq 9 进到正在跑的 turn 里，跟它被送出去的时间点差了一个边界。两条后续 prompt 没有被并在一起：一条 prompt 一个 turn，唤醒一次跑出三个 turn。随便挑一个时间点把历史推导出来，每一条说 model 看过的 `user/message`，它就是真的看过。

### 改了什么

跟 Section 06 比起来：

- `kernel.py`、`message.py`、`scheduler.py`、`session_log.py`、`standin.py`、 `tools.py` 原封不动搬过来。`inbox.py` 是唯一的新源代码文件；其他改动都是把 inbox 接进 `agent_loop.py`，所以跟 06 的 diff 刚好就是这个 Section 的 Mechanism，没有别的。
- `agent_loop.py`：`send()` 改成走 inbox，不再自己追加 `user/message`，并且多了 `target` 和 `wakeup` 两个参数，还有 `followup()` / `steer()` / `inject()` 三个现成组合。那个“agent 正在跑 turn”的 RuntimeError 没了：turn 中途送进来的东西会排队，不会抛出异常。现在一次 `send()` 会把排队的 prompt 全跑完才回来。 `cancel()` 也会把 inbox 清空。
- log 的长相变了：`user/message` 现在落在认领它的那个 step 里面，接在 `step/start` 后面，而不是在 `turn/start` 之前。输入只有被认领，才进得了对话记录。
- `demo.py`：Live demo 在闲着的时候用 `inject()` 先把 context 摆着，接着在 turn 中途从 bus 上的 listener 介入，并排一条后续 prompt，所以一次 send 就能在真的 model 上把三种投递方式都演一遍。

---

## In real dsh

所有指过去的链接都固定在 Studied version [`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca) 上。inbox 住在 agent 这个包里，认领的位置则在 loop 里： [`packages/core/agent`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent)。

| Mini-dsh | 真正的 dsh | 说明 |
| --- | --- | --- |
| `inbox.py` 里的 `Inbox` | [`packages/core/agent/src/inbox.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent/src/inbox.ts)：`Inbox` | 每个 agent 两份有顺序的待处理列表；`InboxTarget = 'next-turn' \| 'next-step'` 声明在 [`types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent/src/types.ts) 里。 |
| `claim(target)` | [`inbox.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent/src/inbox.ts)：`Inbox.claim` | 规则一样：先拿走 next-step 的全部输入，如果这个边界要开一个 turn，再多拿一条排队的 prompt。它被写成 loop 在 step 边界上的操作，不是给 plugin 用的扩展点。 |
| `send(text, target, wakeup)` | [`packages/core/agent/src/runtime-types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent/src/runtime-types.ts)：`Agent.send` | 统一的投递入口；`followup`、`steer`、`inject` 是参数固定好的别名，跟 mini 那三行一模一样。 |
| 收 turn 前的再确认 | [`packages/core/agent-loop/src/agent.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-loop/src/agent.ts) | 一个 turn 要收掉，条件是某个 step 带着结束原因收尾，而且 `inbox.nextStep` 是空的；这个确认排在 `agent/turn-stopping` 这个 serial hook 之后，让它有最后一次介入的机会。 |
| `cancel()` 清空 inbox | [`runtime-types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent/src/runtime-types.ts)：`CancelOptions` | `cancel(cause)` 会把排队的和介入用的东西一起清掉，除非 `keepInbox` 要求留着；`clear()` 先清 next-step，再清 next-turn。 |
| `_drain()` | [`agent.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-loop/src/agent.ts)：`kick()` | 驱动的那一段会先把排队的工作跑完才收工，而 `running` 会横跨连续好几个排队的 turn，所以它不能拿来证明某个 turn 还开着。 |

真正的 inbox 在这个 Section 的 Mechanism 之上，还多做了这些：

- **撑得过重启。**每一次变动都会追加一条正规化的 `agent/inbox/spliced` session 事件，而内存里那两份列表，是回头读这些记录重建出来、只重放一次的投影，所以待处理的输入撑得过一次重启。mini 的 inbox 只活在内存里：它的 log 只有一个写入者（Section 06），放进来的动作又发生在工作线程上，所以只有被认领的消息才进得了 log。
- **有身份，也能改。**真正的待处理消息带着 id，在被认领之前可以 `replace()` 或 `remove()`；每一次改动都会即时发成 `agent/inbox/inserted`、`claimed` 或 `discarded`。mini 放进去的东西没有名字，一旦归档就只能等。
- **认领和 step 之间有一个 hook。**`agent/pre-step` 这个 waterfall 可以否决一个提议中的 step，也可以改写刚认领到的那一批消息；被否决的 step 会把它认领到的消息就地结束，然后一个 step 都不跑就把 turn 收掉。mini 这边只要认领到，就一定会进去。
- **唤醒有一道闩。**真正的唤醒跟放入是分开的：唤醒如果落在一段被中止的活动里，会改指向 `next-turn` 并且被闩住，等驱动的那一段收敛到闲置状态再重放一次。mini 的唤醒就一行，“闲着就 drain”，之所以安全，是因为只有驱动的那条线程会看到闲置这件事。
- **按介入键的是人。**在真正的 dsh 里，介入通常来自 UI，而 UI 在 Ceiling 之上； mini 是从 tool 的实现和 bus 上的 listener 去按 `steer()` 和 `followup()`， `inject()` 则是从脚本按的。

---

## Failure modes

- **输入一到就套用，对话记录会说谎。**正在飞的那次 request 早就把历史推导完了，所以 step 中途追加的那一条，会说 model 看过它其实没收到的字，重放的时候还会重建出一次根本没送出去过的 request。认领固定落在边界上，反正下一次 request 本来就在那里重新推导；这样 log 才留得住真正发生过的事。
- **一份列表会把两种意图压成一种。**把 prompt 全折进正在跑的 turn，一个后续问题就会把手上的工作整个抢走；把介入全延到下一个 turn，它又会在 agent 一路走错走到底之后才到。目标由送件的人指定，因为只有他知道自己要的是哪一种。
- **一次认领所有排队的 prompt，等于把好几段对话并成一段。**开启 turn 的那次认领最多只拿一条 `next-turn` 消息，所以三条排队的 prompt 会变成三个 turn、三个答案，而不是一条塞得满满的大 prompt 配一个含糊的答案。
- **收 turn 之前不再确认一次，最后一秒的介入会被晾在那里。**在最后一个 step 快收尾时才进来的输入，会躺在 `next-step` 里，等一个可能永远不会来的唤醒。收之前先看一眼列表：有新的介入，就在它原本瞄准的那个 turn 里再多跑一个 step。
- **inbox 熬得过 cancel，被取消的工作就会复活。**`cancel()` 会在中止之前把两份列表都清空，所以取消之前排的输入，跟着那个 turn 一起消失。取消之后才送的照常排队，drain 的 loop 会接手：那是一次干净的重新开始，不是被取消的那次又爬回来。
- **工作线程自己去写 user 记录，会跟 log 的写入抢成一团。**Section 06 把 log 定成只有一个写入者，inbox 也守住这件事：放进来的动作有锁保护、只动内存，而且只有驱动的那条线程，会把认领到的东西变成记录。

---

## 跑跑看

[`src/`](src/) 把 06 搬过来，然后加上：

- [`inbox.py`](src/inbox.py)（新的）：`Inbox`，一把锁后面两份待处理列表； `insert`、`claim`、`has`、`clear`。
- [`agent_loop.py`](src/agent_loop.py)：`send()` 改走 inbox，多了 `target` 和 `wakeup`；`followup()`、`steer()`、`inject()`；drain 的 loop；每个 step 边界上的认领；收 turn 前的再确认；`cancel()` 会清空 inbox。
- [`test.py`](src/test.py)：tool 的实现介入它自己所在的那个 turn，又排了两条 prompt，每一条各拿到一个 turn；闲着时 `inject()` 不会动到 log，要等下一次唤醒先来认领；介入如果落在一个已经完成的 step 期间，那个 turn 会再多开一个 step；cancel 会把所有待处理的东西丢掉，而下一次 send 从干净的状态重新开始。
- [`demo.py`](src/demo.py)：Live demo 在闲着的时候先把 context 摆进去，接着在 turn 中途从 bus 上介入、排一条后续 prompt，最后把 log 自己记下的这三种投递方式打印出来。

```bash
python sections/07-inbox/src/test.py    # offline check, no key
```

Live demo 需要根目录的 `requirements.txt` 和一把 key；没有 key 的话，它会安静地跳过：

```bash
pip install -r requirements.txt         # anthropic + python-dotenv
cp .env.example .env                    # then set ANTHROPIC_API_KEY
python sections/07-inbox/src/demo.py
```

---

## 出处

- [`docs/agent-lifecycle.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/agent-lifecycle.md)： dsh 自己画的一个 turn，连认领的位置和 inbox 事件都画进去了。
- [`docs/subsystems/core.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/subsystems/core.md)： Agent 对外的接口、三个现成的别名，还有把 inbox 当成一整套投递词汇来介绍的那一段。
- [`.agents/notes/implemented/architecture/2026-07-30-followup-enqueue-and-owned-runs.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/.agents/notes/implemented/architecture/2026-07-30-followup-enqueue-and-owned-runs.md)：那份设计笔记，讲的是为什么 `followup()` 不返回任何 handle。
