<!-- source: README.md @ 3705bd7 -->

# 03 · Compaction

[English](README.md) | [繁體中文](README.zh-TW.md) | 简体中文

> 对话历史总会长到需要压缩，但 append-only log 不应该被修改，否则依赖它的索引、重放与稽核都会失效。好在模型读的不是 log 本身，而是一份「哪些事件需要显示」的列表。因此，compaction 真正要改的是这份列表。

当对话长度超过 context window，就必须缩短模型可见的历史，例如用一段摘要取代较早的多轮对话。

第 02 章刻意将 log 设计成只能追加。每个事件的 seq 都对应 log 中的固定位置，事件流、持久化数据和重放机制都依赖这些索引。任意删除或重排事件，都会破坏这些假设。

因此，本章要回答的问题是：在 log 只能追加的前提下，compaction 要如何移除模型可见的旧内容？

第 02 章已经留好了解法。模型不会直接读取 log，而是读取从 surface 推导出来的消息。surface 本质上就是一份有顺序的索引列表，用来决定哪些事件会进入模型历史。

所以 compaction 只修改 surface，不修改 log。它先追加一个包含摘要的新事件，再通过 surface op 将 surface 中一段连续的旧事件替换成这个新事件。

这个设计需要以下规则：

1. 每次追加都可以带一个 **surface op**：`"append"` 表示加入 surface，`None` 表示只写入 log，`{"op": "replace", "start": s, "end": e}` 则会替换 seq 位于 `[start, end)` 的 surface 项目。
2. surface op 会与事件一起写入 log，因此只要重放 log 就能重建 surface。
3. 系统必须在写入事件前验证 surface 转换。如果 op 不合法，log 和 surface 都保持不变。
4. 用来替换旧内容的新事件，本身也必须能转成模型消息。在 compaction 中，这就是摘要消息。
5. log 中的原始事件永远不移动、不删除。compaction 只会缩小推导出来的视图。

---

## 核心机制

核心只有两个组件，都位于 `Session` 中：

- **Surface op**：`append()` 的第三个参数。未指定时，沿用第 02 章的默认行为：可转成消息的事件加入 surface，其他事件只写入 log。也可明确传入 replace op。
- **`_surface_after()`**：在实际写入前，先计算这次追加后的 surface。如果 op 不合法就抛出错误；只有验证成功后，事件才会写入 log。

现在 `append()` 会先验证这次转换，成功后才写入，而 op 本身也会成为不可变事件的一部分：

```python
def append(self, event_type, payload, surface_op=None):
    # Validate-and-copy at the boundary: the payload must be plain JSON
    # data, and the log keeps its own copy so no caller can edit history.
    payload = json.loads(json.dumps(payload))
    if surface_op is None and event_type in SURFACE_TYPES:
        surface_op = "append"
    seq = len(self.log)
    # Validate the surface transition before committing: a bad op must
    # leave both the log and the surface untouched.
    surface = self._surface_after(event_type, seq, surface_op)
    event = _freeze(
        {"seq": seq, "type": event_type, "payload": payload, "surface_op": surface_op}
    )
    self.log.append(event)
    self.surface = surface
    if self._on_event is not None:
        self._on_event(self, event)
    return event
```

replace 分支会用新事件取代 surface 中一段连续项目：

```python
def _surface_after(self, event_type, seq, surface_op):
    """The surface as it will be once this append commits. Raises if invalid."""
    if surface_op is None:
        return self.surface
    if event_type not in SURFACE_TYPES:
        raise ValueError(f"'{event_type}' derives no message; it cannot join the surface")
    if surface_op == "append":
        return self.surface + [seq]
    if not isinstance(surface_op, dict) or surface_op.get("op") != "replace":
        raise ValueError(f"unknown surface op: {surface_op!r}")
    # {"op": "replace", "start": s, "end": e}: this event shadows the
    # surface entries whose seq falls in [start, end), half-open.
    start, end = surface_op["start"], surface_op["end"]
    covered = [i for i, s in enumerate(self.surface) if start <= s < end]
    if not covered:
        raise ValueError(f"replace [{start}, {end}) covers no surface entry")
    if covered != list(range(covered[0], covered[-1] + 1)):
        raise ValueError(f"replace [{start}, {end}) covers a non-contiguous surface run")
    return self.surface[: covered[0]] + [seq] + self.surface[covered[-1] + 1 :]
```

因此，compaction 不需要独立的子系统。它只是一次普通的追加：添加一则包含摘要的 `user/message`，再用 replace op 取代对应的旧 seq。

```text
log      0:user  1:chunk  2:assistant  3:tool  4:user  5:assistant
surface  [0, 2, 3, 4, 5]

append("user/message", {"content": "Summary: ..."},
       surface_op={"op": "replace", "start": 0, "end": 4})

log      0:user  1:chunk  2:assistant  3:tool  4:user  5:assistant  6:user
surface  [6, 4, 5]

derive_messages() ──► "Summary: ..."   "and now?"   "Now this."
```

每笔原始记录都保留在 log 中，seq 与不可变状态也没有改变。唯一改变的是投影结果：`derive_messages()` 现在会从摘要开始。

有两个细节撑住了整件事：

- **先验证，再写入。** `_surface_after()` 会在 `self.log.append` 前运行。不合法的 op 直接让 `append()` 失败，log 与 surface 都维持原状，不会留下实际未生效的幽灵记录。
- **把 op 记在事件上。** 每个事件都带有自己的 surface op，因此 surface 可以完全由 log 重建。离线测试会将第一个 `Session` 的记录逐笔重放到第二个 `Session`，确认两者结果一致。

有个容易忽略的细节：compaction 后，surface 不一定依照 seq 排序。上例中的结果是 `[6, 4, 5]`，因为 surface 表示的是对话顺序，而不是事件写入 log 的顺序。

因此，后续 replace 必须对应到 *surface 中连续的一段*；若指定的 seq 区间在 surface 上不连续，`_surface_after()` 就会拒绝操作。

### 改了什么

与第 02 章相比：

- `kernel.py`、`message.py` 和 `standin.py` 完整沿用；只有 `session_log.py` 改了，因此与第 02 章相比，diff 只包含本章添加的机制，不包含其他改动。
- `append()` 多了 `surface_op` 这个参数，会把 op 记在冻结的事件上，而且要等新的 `_surface_after()` 验过这次转换，才真的写进去。
- surface 类型还是刚好三种。compaction 的摘要就是一则普通的 `user/message`；做替换的是那个 op，不是什么新的事件类型。
- 没有独立的 `compaction.py`。compaction 本质上只是一次 `append()`，因此实现直接放在管理 surface 的 `Session` 中。

---

## 对照真正的 dsh

以下链接皆指向研究版本 [`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca)。surface 和它的那些 op 位于 [`packages/core/session`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session)。

| Mini-dsh | 真正的 dsh | 说明 |
| --- | --- | --- |
| `surface_op` 这个参数：`"append"` 或 `{"op": "replace", "start", "end"}` | [`packages/core/session/src/types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session/src/types.ts)：`SurfaceOp` | `SurfaceOp = 'append' \| { op: 'replace', start, end }`，本章重建的就是这两种操作一模一样的形状。 |
| `append()` 里的先验证、后写入 | [`packages/core/session/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session/src/index.ts)：`class Session` | `append()` 会先验证（`snapshotJsonValue`）、深层冻结、验证 surface 的转换，最后才推进去；compaction 靠一个 `replace` 标记改写 surface，完全不动 log。 |
| 维护 surface 的 `_surface_after()` | [`packages/core/session/src/surface.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session/src/surface.ts)：`SurfaceManager` | 真正的 surface 是一个有专属模块在管的对象；mini 这边把它折成 `Session` 上的两个方法。 |
| 摘要就是一则普通的 `user/message` | [`packages/core/session/src/known-event-types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session/src/known-event-types.ts)：`compaction/*` | 真正的 dsh 给了 compaction 自己的事件类型，用 declaration merging 加进 `SessionEventMap`；它们就在整个 repo 那 45 种事件类型里面。 |

真正的 session log 还提供以下功能：

- **compaction 是一个 plugin，还带着自己的一套词汇。** 核心的 session 软件包里一个 `compaction/*` 类型都没有；是 plugin 用 declaration merging 加上去的，然后出现在 [`known-event-types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session/src/known-event-types.ts) 那 45 种事件类型里。mini 这边让 `SURFACE_TYPES` 就维持三种，摘要直接重用 `user/message`，这样整个 diff 就只剩那个 op。
- **总得有人来写这段摘要。** 本章把摘要文本当成调用端给的数据；不管是谁写的，replace op 的行为都一样。要靠 model 生出摘要，得先有一个会发请求的 loop，而 mini-dsh 要到第 04 章才拿得到。
- **另一种独立投影。** [`packages/session/session-projection`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/session/session-projection) 会将已写入的事件整理成前端使用的读取模型，不受 surface replace 影响，也与 `deriveMessages()` 无关。UI 不在本教学的实现范围内。

---

## 常见失败模式

- **`end` 采用不包含上界的规则。** `{"start": 0, "end": 4}` 会取代 seq 0 到 3，seq 4 仍会保留。边界若算错，摘要可能与原本应被取代的消息同时出现。测试也会确认 `[4, 4)` 因未涵盖任何项目而遭拒。
- **未涵盖任何项目的 replace 会造成内容重复。** 如果空范围也能写入，摘要会加入 surface，但原始内容仍全部保留。因此 `_surface_after()` 会直接拒绝这种操作。
- **第一次 compaction 后，surface 顺序可能不同于 seq 顺序。** surface 为 `[6, 4, 5]` 时，seq 区间 `[5, 7)` 会选到 6 与 5，却跳过中间的 4。这不是连续的 surface 范围，因此系统必须拒绝。
- **先写进去、事后才验证，重放就坏了。** 如果 `append()` 先把记录推进去、事后才验证，一次失败的 compaction 就会留下一个事件，上面记着一个从来没生效的 op，之后从 log 重建出来的 surface 就会跟当下那个对不起来。真正的 dsh 也是为了同一个理由，先验证 surface 的转换再往里推。
- **无法重放的 op 必须立即拒绝。** `_surface_after()` 不支持 `{"op": "delete"}` 或 `"prepend"`；若仍写入事件，日后就没有重放器能正确解读。
- **只进 log 的事件不能拿来做替换。** 一个带着 replace op 的 `assistant/chunk`，会把 model 视野里的一段删掉，却没放任何读得懂的东西进去。这个 op 只收 surface 类型：拿来替换消息的，自己也得是一则消息。
- **模型无法自行取回被 compaction 隐藏的内容。** 系统没有反向的 un-replace op。compaction 后，摘要就是模型能看到的唯一版本；即使原始 log 仍可重放与稽核，品质不佳的摘要仍会持续影响后续对话。

---

## 动手验证

[`src/`](src/) 延续第 02 章，并加入：

- [`session_log.py`](src/session_log.py)（有改动）：`append()` 上的 `surface_op` 参数、记在每个冻结事件上的那个 op，还有在写进去之前先验每一次转换的 `_surface_after()`。
- [`test.py`](src/test.py)：推导出来的视图缩小了，而 log 每一笔记录都还在、op 确实记在记录上、把 log 重放一遍能一模一样重建出 surface、不合法的 op（盖不到东西、拿只进 log 的事件来替换、`end` 不含在内的边界、没听过的 op 名称、不连续的一段）会被挡下来，而且完全不动到 session，还有第二次 compaction 可以盖住第一次。

```bash
python sections/03-compaction/src/test.py   # offline checks, no key
```

这项机制完全不碰 Model seam：摘要是调用端给的数据。检查里动用 Scripted stand-in，只是为了在 compaction 之前，先将一段完整对话的流式事件写入 log；要等 loop 出现（第 04 章）才会有 `demo.py`。

---

## 参考资料

- [`docs/subsystems/session.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/subsystems/session.md)： dsh 自己写的 session 子系统文档。
- [`packages/core/session/README.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session/README.md)：这个软件包自己的 README。
