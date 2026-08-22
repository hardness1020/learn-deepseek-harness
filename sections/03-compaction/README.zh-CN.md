<!-- source: README.md @ d5b8152 -->

# 03 · Compaction

[English](README.md) | [繁體中文](README.zh-TW.md) | 简体中文

> 历史总有一天得缩小，但 log 只会往后长，改它会弄坏建立在上面的所有东西。不过 model 从来不读 log，它读的是一份列表，列表列出哪些记录要显示给它看。要缩小的是那份列表。

对话会长到 context window 装不下。model 的历史迟早得缩短：一长串旧的来回，换成一小段摘要。

但 Section 02 把 log 做成只能追加，是故意的。每一条记录的 seq 就是它在 log 里的位置，而且永远不变；事件流和已经存下来的任何记录都指向这些数字，重放也会照着这些数字依序走一遍。改掉或删掉任何一条记录，这一整套都会坏掉。

所以：如果 log 只能追加，compaction 要怎么拿掉 model 看得到的东西？

Section 02 已经先把出路做好了。model 从来看不到 log，它看到的是从 surface 推导出来的消息，而 surface 其实就是一份列表，列出哪些记录算是消息。

所以 compaction 改的是 surface，不是 log：它先像其他事件一样追加一个新事件，事件上带着一个 surface op，告诉 session log 要把 surface 上连续的一段换成这个新事件。

要做到这件事，session log 得先：

1. 每一次追加都带一个 **surface op**：`"append"` 表示加进 surface，`None` 表示只进 log，或是 `{"op": "replace", "start": s, "end": e}`，把 seq 落在 `[start, end)` 之间的那些 surface 项目遮掉。
2. 把这个 op 记在写进 log 的事件上，这样光靠 log 就能把 surface 推导回来。
3. 事件写进去之前，先验证 surface 的转换：op 不合法就什么都不会变，log 不变， surface 也不变。
4. 来做替换的那个事件，自己也必须能推导出一条消息：model 读到的那段摘要，就是拿来顶替它失去的东西。
5. 绝不去改、去搬、去删 log 里的任何一条记录：compaction 缩小的只有推导出来的那个视图。

---

## Mechanism

两个零件，都在 `Session` 里面：

- **Surface op**：`append()` 的第三个参数。不传的话，`append()` 就用 Section 02 的默认：surface 类型加进 surface，其他都只进 log。明着传进来的话，它可以是 replace 那种形式。
- **`_surface_after()`**：先算出这次追加写进去以后，surface 会长什么样子；op 不合法就直接抛错。它顺利返回了，事件才进得了 log。

现在 `append()` 会先验证这次转换，才真的写进去，而 op 本身也被冻进事件里：

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

replace 那个分支，会用新事件把 surface 上连续的一段遮起来：

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

所以 compaction 根本不是一个新的子系统。它就是一次普通的追加：一条装着摘要的 `user/message`，外加一个 replace op，盖掉它要收起来的那些 seq。

```text
log      0:user  1:chunk  2:assistant  3:tool  4:user  5:assistant
surface  [0, 2, 3, 4, 5]

append("user/message", {"content": "Summary: ..."},
       surface_op={"op": "replace", "start": 0, "end": 4})

log      0:user  1:chunk  2:assistant  3:tool  4:user  5:assistant  6:user
surface  [6, 4, 5]

derive_messages() ──► "Summary: ..."   "and now?"   "Now this."
```

每一条记录都还在 log 里，还在原来的 seq 上，还是冻结的。变的只有投影： `derive_messages()` 现在从那段摘要开始。

有两个细节撑住了整件事：

- **先验证，再写进去。** `_surface_after()` 跑在 `self.log.append` 之前。一个被挡下来的 op 会直接从 `append()` 抛出来，而 log 和 surface 都保持原样：不会留下一条幽灵记录，上面记着一个从来没发生过的 op。
- **op 就记在记录上。** 因为每个事件都带着自己的 surface op，surface 就是 log 的纯函数：把 log 里每一次追加重放一遍，你就能把 surface 一模一样地重建出来。 Offline check 证明这件事的方法，是拿第一个 `Session` 的记录去重建出第二个 `Session`。

有一个怪处值得盯着看一下：compaction 过后，surface 就不再照 seq 排序了。上面那张图里它是 `[6, 4, 5]`，摘要排在比较旧的 seq 前面，因为 surface 的顺序是对话的顺序，不是 log 的顺序。

这就是为什么之后的 replace 必须盖住 *surface 上连续的一段*，也是为什么 `_surface_after()` 会挡掉那种盖起来中间有洞的 seq 区间。

### 改了什么

跟 Section 02 比起来：

- `kernel.py`、`message.py` 和 `standin.py` 原封不动搬过来；只有 `session_log.py` 改了，所以跟 02 的 diff 就是这个 Section 的 Mechanism，多的没有。
- `append()` 多了 `surface_op` 这个参数，会把 op 记在冻结的事件上，而且要等新的 `_surface_after()` 验过这次转换，才真的写进去。
- surface 类型还是刚好三种。compaction 的摘要就是一条普通的 `user/message`；做替换的是那个 op，不是什么新的事件类型。
- 没有 `compaction.py` 这个文件。compaction 就是一次 `append()` 调用，所以这个 Mechanism 就住在 surface 住的地方。

---

## In real dsh

所有指过去的链接都固定在 Studied version [`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca) 上。surface 和它的那些 op 住在 [`packages/core/session`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session)。

| Mini-dsh | 真正的 dsh | 说明 |
| --- | --- | --- |
| `surface_op` 这个参数：`"append"` 或 `{"op": "replace", "start", "end"}` | [`packages/core/session/src/types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session/src/types.ts)：`SurfaceOp` | `SurfaceOp = 'append' \| { op: 'replace', start, end }`，这个 Section 重建的就是这两支一模一样的形状。 |
| `append()` 里的先验证、后写入 | [`packages/core/session/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session/src/index.ts)：`class Session` | `append()` 会先验证（`snapshotJsonValue`）、深层冻结、验证 surface 的转换，最后才推进去；compaction 靠一个 `replace` 标记改写 surface，完全不动 log。 |
| 维护 surface 的 `_surface_after()` | [`packages/core/session/src/surface.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session/src/surface.ts)：`SurfaceManager` | 真正的 surface 是一个有专属模块在管的对象；mini 这边把它折成 `Session` 上的两个方法。 |
| 摘要就是一条普通的 `user/message` | [`packages/core/session/src/known-event-types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session/src/known-event-types.ts)：`compaction/*` | 真正的 dsh 给了 compaction 自己的事件类型，用 declaration merging 加进 `SessionEventMap`；它们就在整个 repo 那 45 种事件类型里面。 |

真正的 session log 在这个 Section 的 Mechanism 之上，还多做了这些：

- **compaction 是一个 plugin，还带着自己的一套词汇。** 核心的 session 包里一个 `compaction/*` 类型都没有；是 plugin 用 declaration merging 加上去的，然后出现在 [`known-event-types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session/src/known-event-types.ts) 那 45 种事件类型里。mini 这边让 `SURFACE_TYPES` 就维持三种，摘要直接重用 `user/message`，这样整个 diff 就只剩那个 op。
- **总得有人来写这段摘要。** 这个 Section 把摘要文本当成调用端给的数据；不管是谁写的，replace op 的行为都一样。要靠 model 生出摘要，得先有一个会发请求的 loop，而 mini-dsh 要到 Section 04 才拿得到。
- **另一种投影，不是这里讲的这种。** [`packages/session/session-projection`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/session/session-projection) 会把已经写进去的事件，整理成给前端看的 UI 读取模型，surface 的替换完全碰不到它。它跟 `deriveMessages()` 没有关系，而 UI 本身在 Ceiling 之上：只指给你看，没有做。

---

## Failure modes

- **`end` 不含在内。** `{"start": 0, "end": 4}` 让 seq 0 到 3 退场，seq 4 还看得到。差一个，摘要就会跟它宣称已经替换掉的消息并排坐在一起。检查特别验了这个边界：`[4, 4)` 什么都没盖到，会被挡下来。
- **盖不到东西的 replace，会把同一段故事讲两遍。** 要是一个空的覆盖范围真的写进去了，摘要会加进 surface，而被它摘要掉的那些东西还全部看得到。所以 `_surface_after()` 直接把它挡掉。
- **第一次 compaction 之后，surface 的顺序就不是 seq 的顺序了。** surface 是 `[6, 4, 5]` 的时候，seq 区间 `[5, 7)` 挑到 6 和 5，却跳过 4：这一段中间破了一个洞。真让它写进去，摘要就会接到一些它根本没盖到的消息上面，所以不连续的覆盖范围会被挡掉。
- **先写进去、事后才验证，重放就坏了。** 如果 `append()` 先把记录推进去、事后才验证，一次失败的 compaction 就会留下一个事件，上面记着一个从来没生效的 op，之后从 log 重建出来的 surface 就会跟当下那个对不起来。真正的 dsh 也是为了同一个理由，先验证 surface 的转换再往里推。
- **log 重放不了的 op，一开始就会被挡掉。** `{"op": "delete"}` 或 `"prepend"` 对 `_surface_after()` 来说什么都不是；默默收下来，就等于在事件上冻进一条没有任何重放器看得懂的记录。
- **只进 log 的事件不能拿来做替换。** 一个带着 replace op 的 `assistant/chunk`，会把 model 视野里的一段删掉，却没放任何读得懂的东西进去。这个 op 只收 surface 类型：拿来替换消息的，自己也得是一条消息。
- **model 失去的东西，model 拿不回来。** 没有反向的 un-replace op。compaction 之后，摘要就是 model 对那一段唯一的记忆，所以一段烂摘要对这场对话来说是永久的，即使 log 里每一条记录都还留着，还能重放、还能审计。

---

## 跑跑看

[`src/`](src/) 把 02 搬过来，再加上：

- [`session_log.py`](src/session_log.py)（有改动）：`append()` 上的 `surface_op` 参数、记在每个冻结事件上的那个 op，还有在写进去之前先验每一次转换的 `_surface_after()`。
- [`test.py`](src/test.py)：推导出来的视图缩小了，而 log 每一条记录都还在、op 确实记在记录上、把 log 重放一遍能一模一样重建出 surface、不合法的 op（盖不到东西、拿只进 log 的事件来替换、`end` 不含在内的边界、没听过的 op 名称、不连续的一段）会被挡下来，而且完全不动到 session，还有第二次 compaction 可以盖住第一次。

```bash
python sections/03-compaction/src/test.py   # offline checks, no key
```

这个 Mechanism 完全不碰 Model seam：摘要是调用端给的数据。检查里动用 Scripted stand-in，只是为了在 compaction 之前，先把一段像样的对话流式送进 log；要等 loop 出现（Section 04）才会有 `demo.py`。

---

## 出处

- [`docs/subsystems/session.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/subsystems/session.md)： dsh 自己写的 session 子系统文档。
- [`packages/core/session/README.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session/README.md)：这个包自己的 README。
