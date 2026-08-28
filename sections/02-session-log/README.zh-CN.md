<!-- source: README.md @ 3705bd7 -->

# 02 · Session log

[English](README.md) | [繁體中文](README.zh-TW.md) | 简体中文

> 模型需要干净的对话历史，持久化需要完整记录，compaction 则需要缩小模型看到的内容。一份可修改的消息列表无法同时满足这三种需求。解法是先完整记下发生过的事，再根据用途推导出不同视图。

一次 agent turn 产生的不只是消息，还包括模型流式返回的 chunk、工具调用与结果、turn 边界标记，以及 request header。

这些数据会被用在三个不同场景。模型调用只需要对话内容；写入磁盘时希望保留所有事件；compaction 要缩短模型可见的历史，但又不能删掉原始记录。

最直觉的做法，是维护一份共用的 `messages` 列表，turn 运行到哪里就追加到哪里。

但单一列表无法兼顾所有需求。如果保留 chunk，模型历史就会混入中间数据；如果不保留，流式输出过程就无法重放。compaction 只能直接改写列表，而程序中断后，也无法追溯这份列表是如何形成的。

session log 改用另一种设计：发生过的每件事只记录一次，而且 log 只能追加；模型看到的历史则在需要时从 log *推导*。这需要以下规则：

1. 每个 session 拥有一份只能追加的 log，其中每个事件都是不可变的。事件的 **seq** 就是它在 log 中的索引，一旦产生就不会改变。
2. 维护一份 **surface**：一组有顺序的 seq，只指向会转成消息的事件。
3. 模型历史不另外保存，而是在每次需要时通过 `derive_messages()` 从 surface 推导。
4. 所有 payload 都在追加边界先验证、再拷贝，避免调用端事后修改历史。
5. 每次成功追加都会通知订阅者，让持久化和监看功能可以以 plugin 形式实现，不需写死在核心中。

---

## 核心机制

本章有三个核心组件：

- **Log**：只能追加的事件列表。每个事件的格式为 `{seq, type, payload}`，且 seq 与列表索引相同。
- **Surface**：一组有顺序的 seq，在事件追加时同步更新。目前只包含 `user/message`、`assistant/message` 和 `tool/result`。
- **`derive_messages()`**：将 surface 投影成 `Message` 列表，每次调用都会重新计算。

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

这个 store 会以 `sessions` service 的形式挂到第 01 章的 kernel，因此 session log 也遵循相同生命周期，卸载时可以完整撤销注册：

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

这样拆分后，`assistant/chunk` 仍会完整写入 log，方便日后重放流式输出；但因为它不属于 surface 类型，所以不会出现在模型历史中。

也因为模型看到的是 surface，而不是 log，第 03 章才能只修改 surface 就缩小这份视图，同时保留 log 中的每笔原始记录。

### 改了什么

与第 01 章相比：

- `message.py`、`standin.py` 和 `kernel.py` 完整沿用，因此 diff 只会显示本章添加的 session log 机制。
- 添加 `session_log.py`：`Session`（log、surface、`append`、`derive_messages`）、 `SessionStore`，还有 `session_log_plugin`。
- session log 是第一个真正挂到 01 那个 kernel 上的 service：`provide("sessions")` 会把它的撤销动作放到这个 plugin 的 fiber 上，所以卸载 session log 就只是一次 `dispose()`。

---

## 对照真正的 dsh

以下链接皆指向研究版本 [`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca)。session log 在真正的 dsh 里的位置是 [`packages/core/session`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session)。

| Mini-dsh | 真正的 dsh | 说明 |
| --- | --- | --- |
| `Session`（log、`append`） | [`packages/core/session/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session/src/index.ts)：`class Session` | `append()` 会先验证（`snapshotJsonValue`）、深层冻结、验证 surface 的转换，最后才推进去；`seq == log.length` 是一条永远成立的规则。 |
| `surface` + `derive_messages()` | [`packages/core/session/src/surface.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session/src/surface.ts)：`SurfaceManager`、`deriveEventMessage` | surface 的事件刚好就是 `user/message`、`assistant/message`、`tool/result` 三种。`SurfaceOp` 不是 `'append'`，就是 `{op: 'replace', start, end}`；replace 对应的 replace 分支会在第 03 章实现。 |
| 事件字典 `{seq, type, payload}` | [`packages/core/session/src/types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session/src/types.ts)：`SessionEvent`、`SessionEventMap` | 核心事件类型有 13 种（turn 和 step 的标记、user、assistant、tool 的往来、请求标头）；整个 repo 加起来 45 种（[`known-event-types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session/src/known-event-types.ts)），还能用 declaration merging 再扩充。 |
| `SessionStore`, `ctx.get("sessions")` | [`packages/core/session/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session/src/index.ts)：`class SessionStore extends Service` | ctx 上的键是 `ctx.sessions`；创建 session 会发出 `session/created`，而且丢例外就能否决这次创建。 |
| `emit("session/event", ...)` | `index.ts` 里的 `session/event` bus 事件 | 这是追加成功之后往外推的那条流。真正的 store 还会发出 `session/disposed` 和 `session/flush`，后者是一道会被等待的持久化屏障。 |

真正的 session log 还提供以下功能：

- **持久化屏障。** `session/flush` 是可平行运行、而且调用端会等待完成的 bus 事件：dsh 会等持久化写入完成后才继续。Mini-dsh 的 `emit` 是同步且不等待后续工作，因此本教学只说明这项设计，没有实现屏障。
- **持久化由 plugin 提供。** 抽象的 [`SessionPersistence`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/session/session-persistence/src/index.ts) service（`ctx.sessionPersistence`）通过 bus 事件接入（[`coordinator.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/session/session-persistence/src/coordinator.ts)）。后端监听 `session/event` 与 `session/flush`，核心 `Session` 不需知道数据如何写入磁盘。dsh 内置 [JSONL](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/session/session-persistence-jsonl) 和 [SQLite](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/session/session-persistence-sqlite) 后端；本教学只示范 JSONL。
- **另一种投影，不是这里讲的这种。** [`packages/session/session-projection`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/session/session-projection) （`ctx.sessionProjections`）会把已经写进去的事件，整理成给前端看的 UI 读取模型。它跟 `deriveMessages()` 没有关系，而 UI 本身不在本教学的实现范围内。
- **改写 surface。** `SurfaceOp` 的 `replace` 对应分支，让 compaction 可以把 model 看到的东西缩小，而 log 依然只能追加（[`index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session/src/index.ts)）：第 03 章做的就是这件事。

---

## 常见失败模式

- **该进入模型历史的事件若未加入 surface，就会被忽略。** 新事件类型如果没有登记在 `SURFACE_TYPES`，`derive_messages()` 不会将它转成消息。真正的 dsh 也基于相同原因，将这份对照集中在 `deriveEventMessage` 中。
- **订阅者抛错会中断追加。** `session/event` 是同步事件，因此监听器的例外会从 `append()` 继续向外抛。真正的 dsh 会在持久化协调器中隔离各监听器的错误，避免单一后端阻塞整份 log。
- **先验证再拷贝，把关的是 JSON 的形状，不是意思。** `json` 来回转一圈，会默默把 tuple 变成 list，`NaN` 也照收；一个 payload 撑过这一关，保证的只是它是纯粹的数据，不保证它就是你本来想写的那个 payload。
- **seq 被多处引用，因此不能原地删除事件。** surface、事件流与持久化数据都依赖固定 seq。删除或重排 log 会破坏这些参考；若要隐藏内容，只能修改投影（第 03 章），不能改动原始 log。
- **log 以外的状态无法重放。** 如果程序另外缓存消息列表或维护可修改的摘要，重新推导历史时就可能不一致。所有写入都必须经过 `append()`，才能让 log 成为唯一可持久化的真相来源。

---

## 动手验证

[`src/`](src/) 延续第 01 章，并加入：

- [`session_log.py`](src/session_log.py)：`Session`（只能追加的 log、surface、 `derive_messages()`）、`SessionStore`，还有把它挂成 `sessions` service 的 `session_log_plugin`。
- [`test.py`](src/test.py)：确认 seq 永远等于索引、surface 只包含应出现在模型历史中的事件、chunk 不会进入模型视图、历史会实时推导、事件不可变、追加边界会拒绝不合法数据、bus 事件确实送出，以及重复的 session id 会遭拒。

```bash
python sections/02-session-log/src/test.py   # offline checks, no key
```

这项机制不会调用模型。测试使用 Scripted stand-in，只是为了产生真实的 `assistant/chunk` 事件并写入 log；第 04 章加入 loop 后才会提供 `demo.py`。

---

## 参考资料

- [`docs/subsystems/session.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/subsystems/session.md)： dsh 自己写的 session 子系统文档。
- [`packages/core/session/README.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session/README.md)：这个软件包自己的 README。
