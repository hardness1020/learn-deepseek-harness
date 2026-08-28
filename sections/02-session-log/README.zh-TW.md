<!-- source: README.md @ 3705bd7 -->

# 02 · Session log

[English](README.md) | 繁體中文 | [简体中文](README.zh-CN.md)

> 模型需要乾淨的對話歷史，持久化需要完整紀錄，compaction 則需要縮小模型看到的內容。一份可修改的訊息清單無法同時滿足這三種需求。解法是先完整記下發生過的事，再根據用途推導出不同視圖。

一次 agent turn 產生的不只是訊息，還包括模型串流回傳的 chunk、工具呼叫與結果、turn 邊界標記，以及 request header。

這些資料會被用在三個不同場景。模型呼叫只需要對話內容；寫入磁碟時希望保留所有事件；compaction 要縮短模型可見的歷史，但又不能刪掉原始紀錄。

最直覺的做法，是維護一份共用的 `messages` 清單，turn 執行到哪裡就追加到哪裡。

但單一清單無法兼顧所有需求。如果保留 chunk，模型歷史就會混入中間資料；如果不保留，串流過程就無法重放。compaction 只能直接改寫清單，而程式中斷後，也無法追溯這份清單是如何形成的。

session log 改用另一種設計：發生過的每件事只記錄一次，而且 log 只能追加；模型看到的歷史則在需要時從 log *推導*。這需要以下規則：

1. 每個 session 擁有一份只能追加的 log，其中每個事件都是不可變的。事件的 **seq** 就是它在 log 中的索引，一旦產生就不會改變。
2. 維護一份 **surface**：一組有順序的 seq，只指向會轉成訊息的事件。
3. 模型歷史不另外儲存，而是在每次需要時通過 `derive_messages()` 從 surface 推導。
4. 所有 payload 都在追加邊界先驗證、再複製，避免呼叫端事後修改歷史。
5. 每次成功追加都會通知訂閱者，讓持久化和監看功能可以以 plugin 形式實作，不需寫死在核心中。

---

## 核心機制

本章有三個核心元件：

- **Log**：只能追加的事件清單。每個事件的格式為 `{seq, type, payload}`，且 seq 與清單索引相同。
- **Surface**：一組有順序的 seq，在事件追加時同步更新。目前只包含 `user/message`、`assistant/message` 和 `tool/result`。
- **`derive_messages()`**：將 surface 投影成 `Message` 清單，每次呼叫都會重新計算。

追加是唯一的寫入動作，所有的把關也都在這裡：

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

推導則是一次什麼都不會動到的讀取：

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

這個 store 會以 `sessions` service 的形式掛到第 01 章的 kernel，因此 session log 也遵循相同生命週期，卸載時可以完整撤銷註冊：

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

這樣拆分後，`assistant/chunk` 仍會完整寫入 log，方便日後重放串流；但因為它不屬於 surface 型別，所以不會出現在模型歷史中。

也因為模型看到的是 surface，而不是 log，第 03 章才能只修改 surface 就縮小這份視圖，同時保留 log 中的每筆原始紀錄。

### 改了什麼

與第 01 章相比：

- `message.py`、`standin.py` 和 `kernel.py` 完整沿用，因此 diff 只會顯示本章新增的 session log 機制。
- 新增 `session_log.py`：`Session`（log、surface、`append`、`derive_messages`）、 `SessionStore`，還有 `session_log_plugin`。
- session log 是第一個真正掛到 01 那個 kernel 上的 service：`provide("sessions")` 會把它的撤銷動作放到這個 plugin 的 fiber 上，所以卸載 session log 就只是一次 `dispose()`。

---

## 對照真正的 dsh

以下連結皆指向研究版本 [`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca)。session log 在真正的 dsh 裡的位置是 [`packages/core/session`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session)。

| Mini-dsh | 真正的 dsh | 說明 |
| --- | --- | --- |
| `Session`（log、`append`） | [`packages/core/session/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session/src/index.ts)：`class Session` | `append()` 會先驗證（`snapshotJsonValue`）、深層凍結、驗證 surface 的轉換，最後才推進去；`seq == log.length` 是一條永遠成立的規則。 |
| `surface` + `derive_messages()` | [`packages/core/session/src/surface.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session/src/surface.ts)：`SurfaceManager`、`deriveEventMessage` | surface 的事件剛好就是 `user/message`、`assistant/message`、`tool/result` 三種。`SurfaceOp` 不是 `'append'`，就是 `{op: 'replace', start, end}`；replace 對應的 replace 分支會在第 03 章實作。 |
| 事件字典 `{seq, type, payload}` | [`packages/core/session/src/types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session/src/types.ts)：`SessionEvent`、`SessionEventMap` | 核心事件型別有 13 種（turn 和 step 的標記、user、assistant、tool 的往來、請求標頭）；整個 repo 加起來 45 種（[`known-event-types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session/src/known-event-types.ts)），還能用 declaration merging 再擴充。 |
| `SessionStore`, `ctx.get("sessions")` | [`packages/core/session/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session/src/index.ts)：`class SessionStore extends Service` | ctx 上的鍵是 `ctx.sessions`；建立 session 會發出 `session/created`，而且丟例外就能否決這次建立。 |
| `emit("session/event", ...)` | `index.ts` 裡的 `session/event` bus 事件 | 這是追加成功之後往外推的那條流。真正的 store 還會發出 `session/disposed` 和 `session/flush`，後者是一道會被等待的持久化屏障。 |

真正的 session log 還提供以下功能：

- **持久化屏障。** `session/flush` 是可平行執行、而且呼叫端會等待完成的 bus 事件：dsh 會等持久化寫入完成後才繼續。Mini-dsh 的 `emit` 是同步且不等待後續工作，因此本教學只說明這項設計，沒有實作屏障。
- **持久化由 plugin 提供。** 抽象的 [`SessionPersistence`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/session/session-persistence/src/index.ts) service（`ctx.sessionPersistence`）透過 bus 事件接入（[`coordinator.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/session/session-persistence/src/coordinator.ts)）。後端監聽 `session/event` 與 `session/flush`，核心 `Session` 不需知道資料如何寫入磁碟。dsh 內建 [JSONL](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/session/session-persistence-jsonl) 和 [SQLite](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/session/session-persistence-sqlite) 後端；本教學只示範 JSONL。
- **另一種投影，不是這裡講的這種。** [`packages/session/session-projection`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/session/session-projection) （`ctx.sessionProjections`）會把已經寫進去的事件，整理成給前端看的 UI 讀取模型。它跟 `deriveMessages()` 沒有關係，而 UI 本身不在本教學的實作範圍內。
- **改寫 surface。** `SurfaceOp` 的 `replace` 對應分支，讓 compaction 可以把 model 看到的東西縮小，而 log 依然只能追加（[`index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session/src/index.ts)）：第 03 章做的就是這件事。

---

## 常見失敗模式

- **該進入模型歷史的事件若未加入 surface，就會被忽略。** 新事件型別如果沒有登記在 `SURFACE_TYPES`，`derive_messages()` 不會將它轉成訊息。真正的 dsh 也基於相同原因，將這份對照集中在 `deriveEventMessage` 中。
- **訂閱者拋錯會中斷追加。** `session/event` 是同步事件，因此監聽器的例外會從 `append()` 繼續向外拋。真正的 dsh 會在持久化協調器中隔離各監聽器的錯誤，避免單一後端阻塞整份 log。
- **先驗證再複製，把關的是 JSON 的形狀，不是意思。** `json` 來回轉一圈，會默默把 tuple 變成 list，`NaN` 也照收；一個 payload 撐過這一關，保證的只是它是純粹的資料，不保證它就是你本來想寫的那個 payload。
- **seq 被多處引用，因此不能原地刪除事件。** surface、事件流與持久化資料都依賴固定 seq。刪除或重排 log 會破壞這些參考；若要隱藏內容，只能修改投影（第 03 章），不能改動原始 log。
- **log 以外的狀態無法重放。** 如果程式另外快取訊息清單或維護可修改的摘要，重新推導歷史時就可能不一致。所有寫入都必須經過 `append()`，才能讓 log 成為唯一可持久化的真相來源。

---

## 動手驗證

[`src/`](src/) 延續第 01 章，並加入：

- [`session_log.py`](src/session_log.py)：`Session`（只能追加的 log、surface、 `derive_messages()`）、`SessionStore`，還有把它掛成 `sessions` service 的 `session_log_plugin`。
- [`test.py`](src/test.py)：確認 seq 永遠等於索引、surface 只包含應出現在模型歷史中的事件、chunk 不會進入模型視圖、歷史會即時推導、事件不可變、追加邊界會拒絕不合法資料、bus 事件確實送出，以及重複的 session id 會遭拒。

```bash
python sections/02-session-log/src/test.py   # offline checks, no key
```

這項機制不會呼叫模型。測試使用 Scripted stand-in，只是為了產生真實的 `assistant/chunk` 事件並寫入 log；第 04 章加入 loop 後才會提供 `demo.py`。

---

## 參考資料

- [`docs/subsystems/session.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/subsystems/session.md)： dsh 自己寫的 session 子系統文件。
- [`packages/core/session/README.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session/README.md)：這個套件自己的 README。
