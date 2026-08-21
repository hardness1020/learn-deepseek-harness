<!-- source: README.md @ d5b8152 -->

# 03 · Compaction

[English](README.md) | 繁體中文 | [简体中文](README.zh-CN.md)

> 歷史總有一天得縮小，但 log 只會往後長，改它會弄壞建立在上面的所有東西。不過 model 從來不讀 log，它讀的是一份清單，清單列出哪些紀錄要顯示給它看。要縮小的是那份清單。

對話會長到 context window 裝不下。model 的歷史遲早得縮短：一長串舊的來回，換成一小段摘要。

但 Section 02 把 log 做成只能追加，是故意的。每一筆紀錄的 seq 就是它在 log 裡的位置，而且永遠不變；事件流和已經存下來的任何紀錄都指向這些數字，重放也會照著這些數字依序走一遍。改掉或刪掉任何一筆紀錄，這一整套都會壞掉。

所以：如果 log 只能追加，compaction 要怎麼拿掉 model 看得到的東西？

Section 02 已經先把出路做好了。model 從來看不到 log，它看到的是從 surface 推導出來的訊息，而 surface 其實就是一份清單，列出哪些紀錄算是訊息。

所以 compaction 改的是 surface，不是 log：它先像其他事件一樣追加一個新事件，事件上帶著一個 surface op，告訴 session log 要把 surface 上連續的一段換成這個新事件。

要做到這件事，session log 得先：

1. 每一次追加都帶一個 **surface op**：`"append"` 表示加進 surface，`None` 表示只進 log，或是 `{"op": "replace", "start": s, "end": e}`，把 seq 落在 `[start, end)` 之間的那些 surface 項目遮掉。
2. 把這個 op 記在寫進 log 的事件上，這樣光靠 log 就能把 surface 推導回來。
3. 事件寫進去之前，先驗證 surface 的轉換：op 不合法就什麼都不會變，log 不變， surface 也不變。
4. 來做替換的那個事件，自己也必須能推導出一則訊息：model 讀到的那段摘要，就是拿來頂替它失去的東西。
5. 絕不去改、去搬、去刪 log 裡的任何一筆紀錄：compaction 縮小的只有推導出來的那個視圖。

---

## Mechanism

兩個零件，都在 `Session` 裡面：

- **Surface op**：`append()` 的第三個參數。不傳的話，`append()` 就用 Section 02 的預設：surface 型別加進 surface，其他都只進 log。明著傳進來的話，它可以是 replace 那種形式。
- **`_surface_after()`**：先算出這次追加寫進去以後，surface 會長什麼樣子；op 不合法就直接拋錯。它順利回傳了，事件才進得了 log。

現在 `append()` 會先驗證這次轉換，才真的寫進去，而 op 本身也被凍進事件裡：

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

replace 那一支，會用新事件把 surface 上連續的一段遮起來：

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

所以 compaction 根本不是一個新的子系統。它就是一次普通的追加：一則裝著摘要的 `user/message`，外加一個 replace op，蓋掉它要收起來的那些 seq。

```text
log      0:user  1:chunk  2:assistant  3:tool  4:user  5:assistant
surface  [0, 2, 3, 4, 5]

append("user/message", {"content": "Summary: ..."},
       surface_op={"op": "replace", "start": 0, "end": 4})

log      0:user  1:chunk  2:assistant  3:tool  4:user  5:assistant  6:user
surface  [6, 4, 5]

derive_messages() ──► "Summary: ..."   "and now?"   "Now this."
```

每一筆紀錄都還在 log 裡，還在原來的 seq 上，還是凍結的。變的只有投影： `derive_messages()` 現在從那段摘要開始。

有兩個細節撐住了整件事：

- **先驗證，再寫進去。** `_surface_after()` 跑在 `self.log.append` 之前。一個被擋下來的 op 會直接從 `append()` 拋出來，而 log 和 surface 都保持原樣：不會留下一筆幽靈紀錄，上面記著一個從來沒發生過的 op。
- **op 就記在紀錄上。** 因為每個事件都帶著自己的 surface op，surface 就是 log 的純函式：把 log 裡每一次追加重放一遍，你就能把 surface 一模一樣地重建出來。 Offline check 證明這件事的方法，是拿第一個 `Session` 的紀錄去重建出第二個 `Session`。

有一個怪處值得盯著看一下：compaction 過後，surface 就不再照 seq 排序了。上面那張圖裡它是 `[6, 4, 5]`，摘要排在比較舊的 seq 前面，因為 surface 的順序是對話的順序，不是 log 的順序。

這就是為什麼之後的 replace 必須蓋住 *surface 上連續的一段*，也是為什麼 `_surface_after()` 會擋掉那種蓋起來中間有洞的 seq 區間。

### 改了什麼

跟 Section 02 比起來：

- `kernel.py`、`message.py` 和 `standin.py` 原封不動搬過來；只有 `session_log.py` 改了，所以跟 02 的 diff 就是這個 Section 的 Mechanism，多的沒有。
- `append()` 多了 `surface_op` 這個參數，會把 op 記在凍結的事件上，而且要等新的 `_surface_after()` 驗過這次轉換，才真的寫進去。
- surface 型別還是剛好三種。compaction 的摘要就是一則普通的 `user/message`；做替換的是那個 op，不是什麼新的事件型別。
- 沒有 `compaction.py` 這個檔案。compaction 就是一次 `append()` 呼叫，所以這個 Mechanism 就住在 surface 住的地方。

---

## In real dsh

所有指過去的連結都固定在 Studied version [`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca) 上。surface 和它的那些 op 住在 [`packages/core/session`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session)。

| Mini-dsh | 真正的 dsh | 說明 |
| --- | --- | --- |
| `surface_op` 這個參數：`"append"` 或 `{"op": "replace", "start", "end"}` | [`packages/core/session/src/types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session/src/types.ts)：`SurfaceOp` | `SurfaceOp = 'append' \| { op: 'replace', start, end }`，這個 Section 重建的就是這兩支一模一樣的形狀。 |
| `append()` 裡的先驗證、後寫入 | [`packages/core/session/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session/src/index.ts)：`class Session` | `append()` 會先驗證（`snapshotJsonValue`）、深層凍結、驗證 surface 的轉換，最後才推進去；compaction 靠一個 `replace` 標記改寫 surface，完全不動 log。 |
| 維護 surface 的 `_surface_after()` | [`packages/core/session/src/surface.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session/src/surface.ts)：`SurfaceManager` | 真正的 surface 是一個有專屬模組在管的物件；mini 這邊把它折成 `Session` 上的兩個方法。 |
| 摘要就是一則普通的 `user/message` | [`packages/core/session/src/known-event-types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session/src/known-event-types.ts)：`compaction/*` | 真正的 dsh 給了 compaction 自己的事件型別，用 declaration merging 加進 `SessionEventMap`；它們就在整個 repo 那 45 種事件型別裡面。 |

真正的 session log 在這個 Section 的 Mechanism 之上，還多做了這些：

- **compaction 是一個 plugin，還帶著自己的一套詞彙。** 核心的 session 套件裡一個 `compaction/*` 型別都沒有；是 plugin 用 declaration merging 加上去的，然後出現在 [`known-event-types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session/src/known-event-types.ts) 那 45 種事件型別裡。mini 這邊讓 `SURFACE_TYPES` 就維持三種，摘要直接重用 `user/message`，這樣整個 diff 就只剩那個 op。
- **總得有人來寫這段摘要。** 這個 Section 把摘要文字當成呼叫端給的資料；不管是誰寫的，replace op 的行為都一樣。要靠 model 生出摘要，得先有一個會發請求的 loop，而 mini-dsh 要到 Section 04 才拿得到。
- **另一種投影，不是這裡講的這種。** [`packages/session/session-projection`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/session/session-projection) 會把已經寫進去的事件，整理成給前端看的 UI 讀取模型，surface 的替換完全碰不到它。它跟 `deriveMessages()` 沒有關係，而 UI 本身在 Ceiling 之上：只指給你看，不重建。

---

## Failure modes

- **`end` 不含在內。** `{"start": 0, "end": 4}` 讓 seq 0 到 3 退場，seq 4 還看得到。差一個，摘要就會跟它宣稱已經替換掉的訊息並排坐在一起。檢查特別驗了這個邊界：`[4, 4)` 什麼都沒蓋到，會被擋下來。
- **蓋不到東西的 replace，會把同一段故事講兩遍。** 要是一個空的覆蓋範圍真的寫進去了，摘要會加進 surface，而被它摘要掉的那些東西還全部看得到。所以 `_surface_after()` 直接把它擋掉。
- **第一次 compaction 之後，surface 的順序就不是 seq 的順序了。** surface 是 `[6, 4, 5]` 的時候，seq 區間 `[5, 7)` 挑到 6 和 5，卻跳過 4：這一段中間破了一個洞。真讓它寫進去，摘要就會接到一些它根本沒蓋到的訊息上面，所以不連續的覆蓋範圍會被擋掉。
- **先寫進去、事後才驗證，重放就壞了。** 如果 `append()` 先把紀錄推進去、事後才驗證，一次失敗的 compaction 就會留下一個事件，上面記著一個從來沒生效的 op，之後從 log 重建出來的 surface 就會跟當下那個對不起來。真正的 dsh 也是為了同一個理由，先驗證 surface 的轉換再往裡推。
- **log 重放不了的 op，一開始就會被擋掉。** `{"op": "delete"}` 或 `"prepend"` 對 `_surface_after()` 來說什麼都不是；默默收下來，就等於在事件上凍進一筆沒有任何重放器看得懂的紀錄。
- **只進 log 的事件不能拿來做替換。** 一個帶著 replace op 的 `assistant/chunk`，會把 model 視野裡的一段刪掉，卻沒放任何讀得懂的東西進去。這個 op 只收 surface 型別：拿來替換訊息的，自己也得是一則訊息。
- **model 失去的東西，model 拿不回來。** 沒有反向的 un-replace op。compaction 之後，摘要就是 model 對那一段唯一的記憶，所以一段爛摘要對這場對話來說是永久的，即使 log 裡每一筆紀錄都還留著，還能重放、還能稽核。

---

## 跑跑看

[`src/`](src/) 把 02 搬過來，再加上：

- [`session_log.py`](src/session_log.py)（有改動）：`append()` 上的 `surface_op` 參數、記在每個凍結事件上的那個 op，還有在寫進去之前先驗每一次轉換的 `_surface_after()`。
- [`test.py`](src/test.py)：推導出來的視圖縮小了，而 log 每一筆紀錄都還在、op 確實記在紀錄上、把 log 重放一遍能一模一樣重建出 surface、不合法的 op（蓋不到東西、拿只進 log 的事件來替換、`end` 不含在內的邊界、沒聽過的 op 名稱、不連續的一段）會被擋下來，而且完全不動到 session，還有第二次 compaction 可以蓋住第一次。

```bash
python sections/03-compaction/src/test.py   # offline checks, no key
```

這個 Mechanism 完全不碰 Model seam：摘要是呼叫端給的資料。檢查裡動用 Scripted stand-in，只是為了在 compaction 之前，先把一段像樣的對話串流進 log；要等 loop 出現（Section 04）才會有 `demo.py`。

---

## 出處

- [`docs/subsystems/session.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/subsystems/session.md)： dsh 自己寫的 session 子系統文件。
- [`packages/core/session/README.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session/README.md)：這個套件自己的 README。
