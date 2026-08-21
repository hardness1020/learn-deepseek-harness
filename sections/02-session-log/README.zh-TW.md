<!-- source: README.md @ d5b8152 -->

# 02 · Session log

[English](README.md) | 繁體中文 | [简体中文](README.zh-CN.md)

> model 要乾淨的歷史，存到磁碟要每一筆紀錄，compaction 要縮小 model 看得到的內容。一份清單沒辦法同時服務三種需求，所以先把發生過的事都只記一次，再從那份紀錄整理出各自需要的內容。

一次 agent turn 產出的東西遠遠不只訊息：model 串流回來的一段段 chunk、tool 的呼叫和結果、turn 的標記、request 標頭。

同一次 turn 會被拿來做三種不同用途。model 要的是乾淨的歷史，存到磁碟要的是每一筆紀錄，compaction 要的是縮小 model 看得到的內容，但不能弄丟原本的紀錄。

最直覺的做法，是共用一份 `messages` 清單，turn 跑到哪就往後追加到哪。

一份清單只能滿足一種需求。串流出來的 chunk 不是把它弄髒，就是整個消失；compaction 只能破壞性地去改它；而程式掛掉之後，你手上就只剩清單當下剛好裝著的東西，沒有任何辦法重建它是怎麼變成這樣的。

session log 把這件事翻了過來：發生過的每一件事都記一次，只能往後追加，然後 model 看到的東西是 *推導* 出來的。要做到這件事，log 得先：

1. 每個 session 留一份只能追加的 log，裡面是凍結的事件；一個事件的 **seq** 就是它在 log 裡的索引，永遠不變。
2. 維護一份 **surface**：照順序排的一串 seq，只收那些會產出訊息的事件，其他都不收。
3. model 的歷史要用的時候才從 surface 推導出來（`derive_messages()`），絕不存起來。
4. 每一個 payload 都在追加的那道邊界上先驗證、再複製一份，這樣歷史事後就改不動了。
5. 每一次成功追加都推給訂閱者，這樣持久化和各種觀察者才能是 plugin，而不是核心裡的程式碼。

---

## Mechanism

三個零件：

- **Log**：一份只能追加的清單，裡面都是凍結的事件。一個事件長成 `{seq, type, payload}`，而它的 seq 就等於它的索引。
- **Surface**：一串照順序排的 seq，在追加的當下就順手維護好：剛好就是 `user/message`、`assistant/message` 和 `tool/result` 這三種事件。
- **`derive_messages()`**：把 surface 投影成一個個 `Message` 物件，每呼叫一次就重算一次。

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

這個 store 會以 `sessions` 這個 service 的身分，掛到 Section 01 的 kernel 上，所以整份 session log 跟其他東西一樣，就是一次可以反向撤銷的註冊：

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

看一下這樣拆開，換到了什麼好處。`assistant/chunk` 是實實在在記進 log 的事件，所以串流可以重放；但它永遠到不了 model 那裡，因為它不是 surface 的型別。

也因為 model 看到的是 surface，不是 log，Section 03 才能只動 surface 就把這個視圖縮小，而 log 裡每一筆紀錄都還在。

### 改了什麼

跟 Section 01 比起來：

- `message.py`、`standin.py` 和 `kernel.py` 原封不動搬過來；跟 01 的 diff 就是這個 Section 的 Mechanism，多的沒有。
- 新增 `session_log.py`：`Session`（log、surface、`append`、`derive_messages`）、 `SessionStore`，還有 `session_log_plugin`。
- session log 是第一個真正掛到 01 那個 kernel 上的 service：`provide("sessions")` 會把它的撤銷動作放到這個 plugin 的 fiber 上，所以卸載 session log 就只是一次 `dispose()`。

---

## In real dsh

所有指過去的連結都固定在 Studied version [`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca) 上。session log 在真正的 dsh 裡的位置是 [`packages/core/session`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session)。

| Mini-dsh | 真正的 dsh | 說明 |
| --- | --- | --- |
| `Session`（log、`append`） | [`packages/core/session/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session/src/index.ts)：`class Session` | `append()` 會先驗證（`snapshotJsonValue`）、深層凍結、驗證 surface 的轉換，最後才推進去；`seq == log.length` 是一條永遠成立的規則。 |
| `surface` + `derive_messages()` | [`packages/core/session/src/surface.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session/src/surface.ts)：`SurfaceManager`、`deriveEventMessage` | surface 的事件剛好就是 `user/message`、`assistant/message`、`tool/result` 三種。`SurfaceOp` 不是 `'append'`，就是 `{op: 'replace', start, end}`；replace 那一支是 Section 03 的事。 |
| 事件字典 `{seq, type, payload}` | [`packages/core/session/src/types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session/src/types.ts)：`SessionEvent`、`SessionEventMap` | 核心事件型別有 13 種（turn 和 step 的標記、user、assistant、tool 的往來、請求標頭）；整個 repo 加起來 45 種（[`known-event-types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session/src/known-event-types.ts)），還能用 declaration merging 再擴充。 |
| `SessionStore`, `ctx.get("sessions")` | [`packages/core/session/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session/src/index.ts)：`class SessionStore extends Service` | ctx 上的鍵是 `ctx.sessions`；建立 session 會發出 `session/created`，而且丟例外就能否決這次建立。 |
| `emit("session/event", ...)` | `index.ts` 裡的 `session/event` bus 事件 | 這是追加成功之後往外推的那條流。真正的 store 還會發出 `session/disposed` 和 `session/flush`，後者是一道會被等待的持久化屏障。 |

真正的 session log 在這個 Section 的 Mechanism 之上，還多做了這些：

- **一道持久化屏障。** `session/flush` 是一個可以平行跑、而且會被 *等待* 的 bus 事件：持久化先把東西寫完，dsh 才往下走。我們 kernel 的 `emit` 是同步的，發出去就不管了，所以這道屏障這裡只是指給你看，沒有重建。
- **持久化是一個個 plugin。** 抽象的 [`SessionPersistence`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/session/session-persistence/src/index.ts) service（`ctx.sessionPersistence`）完全靠 bus 事件掛上去（[`coordinator.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/session/session-persistence/src/coordinator.ts)）：後端一路跟著 `session/event` 和 `session/flush` 走，而核心的 `Session` 從頭到尾不知道世界上有硬碟這種東西。dsh 內建 [JSONL](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/session/session-persistence-jsonl) 和 [SQLite](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/session/session-persistence-sqlite) 兩種後端。Ceiling：JSONL 以外的持久化後端只指給你看，不重建。
- **另一種投影，不是這裡講的這種。** [`packages/session/session-projection`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/session/session-projection) （`ctx.sessionProjections`）會把已經寫進去的事件，整理成給前端看的 UI 讀取模型。它跟 `deriveMessages()` 沒有關係，而 UI 本身在 Ceiling 之上。
- **改寫 surface。** `SurfaceOp` 的 `replace` 那一支，讓 compaction 可以把 model 看到的東西縮小，而 log 依然只能追加（[`index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session/src/index.ts)）： Section 03 做的就是這件事。

---

## Failure modes

- **會產出訊息、卻被 surface 漏掉的事件，等於不存在。** 你加了一個新的事件型別，它本該送到 model 面前，卻沒登記進 `SURFACE_TYPES`，那 `derive_messages()` 就會默默把它丟掉。真正的 dsh 也是為了同一個理由，才把這份對照集中放在 `deriveEventMessage` 裡。
- **有訂閱者拋錯，追加就卡住。** `session/event` 這條流是同步的，所以一個壞掉的監聽器會讓例外一路穿過 `append()` 拋出來。真正的 dsh 在持久化的協調器裡，把每個監聽器的例外各自收住，這樣一個後端才卡不死整份 log。
- **先驗證再複製，把關的是 JSON 的形狀，不是意思。** `json` 來回轉一圈，會默默把 tuple 變成 list，`NaN` 也照收；一個 payload 撐過這一關，保證的只是它是純粹的資料，不保證它就是你本來想寫的那個 payload。
- **到處都指著 seq，所以原地刪除本來就做不到，而且是故意的。** surface、那條事件流，還有任何一筆存下來的紀錄，指的全是 seq。刪掉或重排 log 裡的紀錄，會把它們一起弄壞；要拿掉東西，只能改投影（Section 03），絕不能對 log 動刀。
- **log 以外的狀態，重放不出來。** 只要有人把訊息清單快取起來，或是自己留著一份可以改的彙總，歷史一旦重新推導，手上那份馬上就對不上了。只有每一次寫入都走 `append()`， log 才真的是唯一那份留得住的事實。

---

## 跑跑看

[`src/`](src/) 把 01 搬過來，再加上：

- [`session_log.py`](src/session_log.py)：`Session`（只能追加的 log、surface、 `derive_messages()`）、`SessionStore`，還有把它掛成 `sessions` service 的 `session_log_plugin`。
- [`test.py`](src/test.py)：seq 永遠等於索引、surface 只挑該挑的、chunk 對 model 隱形、歷史是推導出來而不是存起來的、事件真的凍結、追加邊界會擋下不該進來的東西、bus 那條事件流確實會推出來，還有重複的 session id 會被擋掉。

```bash
python sections/02-session-log/src/test.py   # offline checks, no key
```

這個 Mechanism 完全不會呼叫 model。檢查裡動用 Scripted stand-in，只是為了把一次像樣的 turn 串流進 log，好讓 `assistant/chunk` 事件是真的；要等 loop 出現（Section 04）才會有 `demo.py`。

---

## 出處

- [`docs/subsystems/session.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/subsystems/session.md)： dsh 自己寫的 session 子系統文件。
- [`packages/core/session/README.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session/README.md)：這個套件自己的 README。
