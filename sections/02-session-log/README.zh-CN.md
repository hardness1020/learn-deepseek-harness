<!-- source: README.md @ 55e829b -->

# 02 · Session log

[English](README.md) | [繁體中文](README.zh-TW.md) | 简体中文

> 一个 turn 会有好几个读者，但一份消息列表只能顾其中一个。所以先把每件事只记一次，再从那份记录推导 model 看到什么。

一次 agent turn 产出的东西远远不只消息：流式吐出来的 chunk、tool 的调用和结果、 turn 的标记、请求头。

而且同一次 turn，会有好几个地方要用它，每个地方想看到的样子都不一样。model 要的是干净的历史，持久化要的是每一条记录，compaction 要的是把 model 看得到的东西缩小，同时又不能弄丢记录。

最直觉的做法，是共用一份 `messages` 列表，turn 跑到哪就往后追加到哪。

一份列表只能满足一种需求。流式吐出来的 chunk 不是把它弄脏，就是整个消失； compaction 只能破坏性地去改它；而程序挂掉之后，你手上就只剩列表当下刚好装着的东西，没有任何办法重建它是怎么变成这样的。

session log 把这件事翻了过来：发生过的每一件事都记一次，只能往后追加，然后 model 看到的东西是 *推导* 出来的。要做到这件事，log 得先：

1. 每个 session 留一份只能追加的 log，里面是冻结的事件；一个事件的 **seq** 就是它在 log 里的索引，永远不变。
2. 维护一份 **surface**：照顺序排的一串 seq，只收那些会产出消息的事件，其他都不收。
3. model 的历史要用的时候才从 surface 推导出来（`derive_messages()`），绝不存起来。
4. 每一个 payload 都在追加的那道边界上先验证、再复制一份，这样历史事后就改不动了。
5. 每一次成功追加都推给订阅者，这样持久化和各种观察者才能是 plugin，而不是核心里的代码。

---

## Mechanism

三个零件：

- **Log**：一份只能追加的列表，里面都是冻结的事件。一个事件长成 `{seq, type, payload}`，而它的 seq 就等于它的索引。
- **Surface**：一串照顺序排的 seq，在追加的当下就顺手维护好：刚好就是 `user/message`、`assistant/message` 和 `tool/result` 这三种事件。
- **`derive_messages()`**：把 surface 投影成一个个 `Message` 对象，每调用一次就重算一次。

追加是唯一的写入动作，所有的把关也都在这里：

```python
def append(self, event_type, payload):
    # Validate-and-copy at the boundary: the payload must be plain JSON
    # data, and the log keeps its own copy so no caller can edit history.
    payload = json.loads(json.dumps(payload))
    seq = len(self.log)
    event = _freeze({"seq": seq, "type": event_type, "payload": payload})
    self.log.append(event)
    if event_type in SURFACE_TYPES:
        self.surface.append(seq)
    if self._on_event is not None:
        self._on_event(self, event)
    return event
```

推导则是一次什么都不会动到的读取：

```python
def derive_messages(self):
    """Project the surface into model history. Never stored, always derived."""
    return [
        Message(
            role=SURFACE_TYPES[event["type"]],
            content=event["payload"]["content"],
        )
        for event in (self.log[seq] for seq in self.surface)
    ]
```

这个 store 会以 `sessions` 这个 service 的身份，挂到 Section 01 的 kernel 上，所以整份 session log 跟其他东西一样，就是一次可以反向撤销的注册：

```python
def session_log_plugin(ctx):
    ctx.provide("sessions", SessionStore(ctx))
```

```text
append(event_type, payload) ──► validate + copy ──► freeze ──► log[seq]
                                                │
                          surface type? ──► surface.append(seq)
                                                │
                                     emit("session/event", ...)

derive_messages() ──► for seq in surface ──► log[seq] ──► Message(role, content)
```

看一下这样拆开，换到了什么好处。`assistant/chunk` 是实实在在记进 log 的事件，所以流式输出可以重放；但它永远到不了 model 那里，因为它不是 surface 的类型。

也因为 model 看到的是 surface，不是 log，Section 03 才能只动 surface 就把这个视图缩小，而 log 里每一条记录都还在。

### 改了什么

跟 Section 01 比起来：

- `message.py`、`standin.py` 和 `kernel.py` 原封不动搬过来；跟 01 的 diff 就是这个 Section 的 Mechanism，多的没有。
- 新增 `session_log.py`：`Session`（log、surface、`append`、`derive_messages`）、 `SessionStore`，还有 `session_log_plugin`。
- session log 是第一个真正挂到 01 那个 kernel 上的 service：`provide("sessions")` 会把它的撤销动作放到这个 plugin 的 fiber 上，所以卸载 session log 就只是一次 `dispose()`。

---

## In real dsh

所有指过去的链接都固定在 Studied version [`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca) 上。session log 在真正的 dsh 里的位置是 [`packages/core/session`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session)。

| Mini-dsh | 真正的 dsh | 说明 |
| --- | --- | --- |
| `Session`（log、`append`） | [`packages/core/session/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session/src/index.ts)：`class Session` | `append()` 会先验证（`snapshotJsonValue`）、深层冻结、验证 surface 的转换，最后才推进去；`seq == log.length` 是一条永远成立的规则。 |
| `surface` + `derive_messages()` | [`packages/core/session/src/surface.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session/src/surface.ts)：`SurfaceManager`、`deriveEventMessage` | surface 的事件刚好就是 `user/message`、`assistant/message`、`tool/result` 三种。`SurfaceOp` 不是 `'append'`，就是 `{op: 'replace', start, end}`；replace 那个分支是 Section 03 的事。 |
| 事件字典 `{seq, type, payload}` | [`packages/core/session/src/types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session/src/types.ts)：`SessionEvent`、`SessionEventMap` | 核心事件类型有 13 种（turn 和 step 的标记、user、assistant、tool 的往来、请求头）；整个 repo 加起来 45 种（[`known-event-types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session/src/known-event-types.ts)），还能用 declaration merging 再扩展。 |
| `SessionStore`, `ctx.get("sessions")` | [`packages/core/session/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session/src/index.ts)：`class SessionStore extends Service` | ctx 上的键是 `ctx.sessions`；创建 session 会发出 `session/created`，而且抛异常就能否决这次创建。 |
| `emit("session/event", ...)` | `index.ts` 里的 `session/event` bus 事件 | 这是追加成功之后往外推的那条流。真正的 store 还会发出 `session/disposed` 和 `session/flush`，后者是一道会被等待的持久化屏障。 |

真正的 session log 在这个 Section 的 Mechanism 之上，还多做了这些：

- **一道持久化屏障。** `session/flush` 是一个可以并行跑、而且会被 *等待* 的 bus 事件：持久化先把东西写完，dsh 才往下走。我们 kernel 的 `emit` 是同步的，发出去就不管了，所以这道屏障这里只是指给你看，没有重建。
- **持久化是一个个 plugin。** 抽象的 [`SessionPersistence`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/session/session-persistence/src/index.ts) service（`ctx.sessionPersistence`）完全靠 bus 事件挂上去（[`coordinator.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/session/session-persistence/src/coordinator.ts)）：后端一路跟着 `session/event` 和 `session/flush` 走，而核心的 `Session` 从头到尾不知道世界上有硬盘这种东西。dsh 内置 [JSONL](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/session/session-persistence-jsonl) 和 [SQLite](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/session/session-persistence-sqlite) 两种后端。Ceiling：JSONL 以外的持久化后端只指给你看，不重建。
- **另一种投影，不是这里讲的这种。** [`packages/session/session-projection`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/session/session-projection) （`ctx.sessionProjections`）会把已经写进去的事件，整理成给前端看的 UI 读取模型。它跟 `deriveMessages()` 没有关系，而 UI 本身在 Ceiling 之上。
- **改写 surface。** `SurfaceOp` 的 `replace` 那个分支，让 compaction 可以把 model 看到的东西缩小，而 log 依然只能追加（[`index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session/src/index.ts)）： Section 03 做的就是这件事。

---

## Failure modes

- **会产出消息、却被 surface 漏掉的事件，等于不存在。** 你加了一个新的事件类型，它本该送到 model 面前，却没登记进 `SURFACE_TYPES`，那 `derive_messages()` 就会默默把它丢掉。真正的 dsh 也是为了同一个理由，才把这份对照集中放在 `deriveEventMessage` 里。
- **有订阅者抛错，追加就卡住。** `session/event` 这条流是同步的，所以一个坏掉的监听器会让异常一路穿过 `append()` 抛出来。真正的 dsh 在持久化的协调器里，把每个监听器的异常各自收住，这样一个后端才卡不死整份 log。
- **先验证再复制，把关的是 JSON 的形状，不是意思。** `json` 来回转一圈，会默默把 tuple 变成 list，`NaN` 也照收；一个 payload 撑过这一关，保证的只是它是纯粹的数据，不保证它就是你本来想写的那个 payload。
- **到处都指着 seq，所以原地删除本来就做不到，而且是故意的。** surface、那条事件流，还有任何一条存下来的记录，指的全是 seq。删掉或重排 log 里的记录，会把它们一起弄坏；要拿掉东西，只能改投影（Section 03），绝不能对 log 动刀。
- **log 以外的状态，重放不出来。** 只要有人把消息列表缓存起来，或是自己留着一份可以改的汇总，历史一旦重新推导，手上那份马上就对不上了。只有每一次写入都走 `append()`， log 才真的是唯一那份留得住的事实。

---

## 跑跑看

[`src/`](src/) 把 01 搬过来，再加上：

- [`session_log.py`](src/session_log.py)：`Session`（只能追加的 log、surface、 `derive_messages()`）、`SessionStore`，还有把它挂成 `sessions` service 的 `session_log_plugin`。
- [`test.py`](src/test.py)：seq 永远等于索引、surface 只挑该挑的、chunk 对 model 隐形、历史是推导出来而不是存起来的、事件真的冻结、追加边界会挡下不该进来的东西、bus 那条事件流确实会推出来，还有重复的 session id 会被挡掉。

```bash
python sections/02-session-log/src/test.py   # offline checks, no key
```

这个 Mechanism 完全不会调用 model。检查里动用 Scripted stand-in，只是为了把一次像样的 turn 流式送进 log，好让 `assistant/chunk` 事件是真的；要等 loop 出现（Section 04）才会有 `demo.py`。

---

## 出处

- [`docs/subsystems/session.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/subsystems/session.md)： dsh 自己写的 session 子系统文档。
- [`packages/core/session/README.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session/README.md)：这个包自己的 README。
