<!-- source: README.md @ 3705bd7 -->

# 03 · Compaction

[English](README.md) | 繁體中文 | [简体中文](README.zh-CN.md)

> 對話歷史總會長到需要壓縮，但 append-only log 不應該被修改，否則依賴它的索引、重放與稽核都會失效。好在模型讀的不是 log 本身，而是一份「哪些事件需要顯示」的清單。因此，compaction 真正要改的是這份清單。

當對話長度超過 context window，就必須縮短模型可見的歷史，例如用一段摘要取代較早的多輪對話。

第 02 章刻意將 log 設計成只能追加。每個事件的 seq 都對應 log 中的固定位置，事件流、持久化資料和重放機制都依賴這些索引。任意刪除或重排事件，都會破壞這些假設。

因此，本章要回答的問題是：在 log 只能追加的前提下，compaction 要如何移除模型可見的舊內容？

第 02 章已經留好了解法。模型不會直接讀取 log，而是讀取從 surface 推導出來的訊息。surface 本質上就是一份有順序的索引清單，用來決定哪些事件會進入模型歷史。

所以 compaction 只修改 surface，不修改 log。它先追加一個包含摘要的新事件，再透過 surface op 將 surface 中一段連續的舊事件替換成這個新事件。

這個設計需要以下規則：

1. 每次追加都可以帶一個 **surface op**：`"append"` 表示加入 surface，`None` 表示只寫入 log，`{"op": "replace", "start": s, "end": e}` 則會替換 seq 位於 `[start, end)` 的 surface 項目。
2. surface op 會與事件一起寫入 log，因此只要重放 log 就能重建 surface。
3. 系統必須在寫入事件前驗證 surface 轉換。如果 op 不合法，log 和 surface 都保持不變。
4. 用來替換舊內容的新事件，本身也必須能轉成模型訊息。在 compaction 中，這就是摘要訊息。
5. log 中的原始事件永遠不移動、不刪除。compaction 只會縮小推導出來的視圖。

---

## 核心機制

核心只有兩個元件，都位於 `Session` 中：

- **Surface op**：`append()` 的第三個參數。未指定時，沿用第 02 章的預設行為：可轉成訊息的事件加入 surface，其他事件只寫入 log。也可明確傳入 replace op。
- **`_surface_after()`**：在實際寫入前，先計算這次追加後的 surface。如果 op 不合法就拋出錯誤；只有驗證成功後，事件才會寫入 log。

現在 `append()` 會先驗證這次轉換，成功後才寫入，而 op 本身也會成為不可變事件的一部分：

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

replace 分支會用新事件取代 surface 中一段連續項目：

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

因此，compaction 不需要獨立的子系統。它只是一次普通的追加：新增一則包含摘要的 `user/message`，再用 replace op 取代對應的舊 seq。

```text
log      0:user  1:chunk  2:assistant  3:tool  4:user  5:assistant
surface  [0, 2, 3, 4, 5]

append("user/message", {"content": "Summary: ..."},
       surface_op={"op": "replace", "start": 0, "end": 4})

log      0:user  1:chunk  2:assistant  3:tool  4:user  5:assistant  6:user
surface  [6, 4, 5]

derive_messages() ──► "Summary: ..."   "and now?"   "Now this."
```

每筆原始紀錄都保留在 log 中，seq 與不可變狀態也沒有改變。唯一改變的是投影結果：`derive_messages()` 現在會從摘要開始。

有兩個細節撐住了整件事：

- **先驗證，再寫入。** `_surface_after()` 會在 `self.log.append` 前執行。不合法的 op 直接讓 `append()` 失敗，log 與 surface 都維持原狀，不會留下實際未生效的幽靈紀錄。
- **把 op 記在事件上。** 每個事件都帶有自己的 surface op，因此 surface 可以完全由 log 重建。離線測試會將第一個 `Session` 的紀錄逐筆重放到第二個 `Session`，確認兩者結果一致。

有個容易忽略的細節：compaction 後，surface 不一定依照 seq 排序。上例中的結果是 `[6, 4, 5]`，因為 surface 表示的是對話順序，而不是事件寫入 log 的順序。

因此，後續 replace 必須對應到 *surface 中連續的一段*；若指定的 seq 區間在 surface 上不連續，`_surface_after()` 就會拒絕操作。

### 改了什麼

與第 02 章相比：

- `kernel.py`、`message.py` 和 `standin.py` 完整沿用；只有 `session_log.py` 改了，因此與第 02 章相比，diff 只包含本章新增的機制，不包含其他改動。
- `append()` 多了 `surface_op` 這個參數，會把 op 記在凍結的事件上，而且要等新的 `_surface_after()` 驗過這次轉換，才真的寫進去。
- surface 型別還是剛好三種。compaction 的摘要就是一則普通的 `user/message`；做替換的是那個 op，不是什麼新的事件型別。
- 沒有獨立的 `compaction.py`。compaction 本質上只是一次 `append()`，因此實作直接放在管理 surface 的 `Session` 中。

---

## 對照真正的 dsh

以下連結皆指向研究版本 [`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca)。surface 和它的那些 op 位於 [`packages/core/session`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session)。

| Mini-dsh | 真正的 dsh | 說明 |
| --- | --- | --- |
| `surface_op` 這個參數：`"append"` 或 `{"op": "replace", "start", "end"}` | [`packages/core/session/src/types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session/src/types.ts)：`SurfaceOp` | `SurfaceOp = 'append' \| { op: 'replace', start, end }`，本章重建的就是這兩種操作一模一樣的形狀。 |
| `append()` 裡的先驗證、後寫入 | [`packages/core/session/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session/src/index.ts)：`class Session` | `append()` 會先驗證（`snapshotJsonValue`）、深層凍結、驗證 surface 的轉換，最後才推進去；compaction 靠一個 `replace` 標記改寫 surface，完全不動 log。 |
| 維護 surface 的 `_surface_after()` | [`packages/core/session/src/surface.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session/src/surface.ts)：`SurfaceManager` | 真正的 surface 是一個有專屬模組在管的物件；mini 這邊把它折成 `Session` 上的兩個方法。 |
| 摘要就是一則普通的 `user/message` | [`packages/core/session/src/known-event-types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session/src/known-event-types.ts)：`compaction/*` | 真正的 dsh 給了 compaction 自己的事件型別，用 declaration merging 加進 `SessionEventMap`；它們就在整個 repo 那 45 種事件型別裡面。 |

真正的 session log 還提供以下功能：

- **compaction 是一個 plugin，還帶著自己的一套詞彙。** 核心的 session 套件裡一個 `compaction/*` 型別都沒有；是 plugin 用 declaration merging 加上去的，然後出現在 [`known-event-types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session/src/known-event-types.ts) 那 45 種事件型別裡。mini 這邊讓 `SURFACE_TYPES` 就維持三種，摘要直接重用 `user/message`，這樣整個 diff 就只剩那個 op。
- **總得有人來寫這段摘要。** 本章把摘要文字當成呼叫端給的資料；不管是誰寫的，replace op 的行為都一樣。要靠 model 生出摘要，得先有一個會發請求的 loop，而 mini-dsh 要到第 04 章才拿得到。
- **另一種獨立投影。** [`packages/session/session-projection`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/session/session-projection) 會將已寫入的事件整理成前端使用的讀取模型，不受 surface replace 影響，也與 `deriveMessages()` 無關。UI 不在本教學的實作範圍內。

---

## 常見失敗模式

- **`end` 採用不包含上界的規則。** `{"start": 0, "end": 4}` 會取代 seq 0 到 3，seq 4 仍會保留。邊界若算錯，摘要可能與原本應被取代的訊息同時出現。測試也會確認 `[4, 4)` 因未涵蓋任何項目而遭拒。
- **未涵蓋任何項目的 replace 會造成內容重複。** 如果空範圍也能寫入，摘要會加入 surface，但原始內容仍全部保留。因此 `_surface_after()` 會直接拒絕這種操作。
- **第一次 compaction 後，surface 順序可能不同於 seq 順序。** surface 為 `[6, 4, 5]` 時，seq 區間 `[5, 7)` 會選到 6 與 5，卻跳過中間的 4。這不是連續的 surface 範圍，因此系統必須拒絕。
- **先寫進去、事後才驗證，重放就壞了。** 如果 `append()` 先把紀錄推進去、事後才驗證，一次失敗的 compaction 就會留下一個事件，上面記著一個從來沒生效的 op，之後從 log 重建出來的 surface 就會跟當下那個對不起來。真正的 dsh 也是為了同一個理由，先驗證 surface 的轉換再往裡推。
- **無法重放的 op 必須立即拒絕。** `_surface_after()` 不支援 `{"op": "delete"}` 或 `"prepend"`；若仍寫入事件，日後就沒有重放器能正確解讀。
- **只進 log 的事件不能拿來做替換。** 一個帶著 replace op 的 `assistant/chunk`，會把 model 視野裡的一段刪掉，卻沒放任何讀得懂的東西進去。這個 op 只收 surface 型別：拿來替換訊息的，自己也得是一則訊息。
- **模型無法自行取回被 compaction 隱藏的內容。** 系統沒有反向的 un-replace op。compaction 後，摘要就是模型能看到的唯一版本；即使原始 log 仍可重放與稽核，品質不佳的摘要仍會持續影響後續對話。

---

## 動手驗證

[`src/`](src/) 延續第 02 章，並加入：

- [`session_log.py`](src/session_log.py)（有改動）：`append()` 上的 `surface_op` 參數、記在每個凍結事件上的那個 op，還有在寫進去之前先驗每一次轉換的 `_surface_after()`。
- [`test.py`](src/test.py)：推導出來的視圖縮小了，而 log 每一筆紀錄都還在、op 確實記在紀錄上、把 log 重放一遍能一模一樣重建出 surface、不合法的 op（蓋不到東西、拿只進 log 的事件來替換、`end` 不含在內的邊界、沒聽過的 op 名稱、不連續的一段）會被擋下來，而且完全不動到 session，還有第二次 compaction 可以蓋住第一次。

```bash
python sections/03-compaction/src/test.py   # offline checks, no key
```

這項機制完全不碰 Model seam：摘要是呼叫端給的資料。檢查裡動用 Scripted stand-in，只是為了在 compaction 之前，先把一段像樣的對話串流進 log；要等 loop 出現（第 04 章）才會有 `demo.py`。

---

## 參考資料

- [`docs/subsystems/session.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/subsystems/session.md)： dsh 自己寫的 session 子系統文件。
- [`packages/core/session/README.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session/README.md)：這個套件自己的 README。
