<!-- source: README.md @ 3705bd7 -->

# 07 · Inbox

[English](README.md) | 繁體中文 | [简体中文](README.zh-CN.md)

> 使用者不應該等 agent 完全停下來才能補充輸入。但若在 step 中途直接寫入 log，紀錄就會誤以為模型看過它實際上沒收到的內容。因此，新輸入要先進 inbox，再於 step 邊界套用。

第 06 章的 agent 只有一個輸入入口。`send()` 收到訊息後，會等整個 turn 執行完才回傳；如果 turn 尚未結束就再次呼叫 `send()`，系統會直接拋出例外。

真實互動不會這麼完整切割。使用者可能在看到工具結果後立即調整方向，背景工作也可能在 turn 中途完成，並希望將結果加入下一次請求。另一方面，真正的後續問題應該開啟新 turn，而不是強行加入當前任務。

最直覺的做法，是將新輸入直接以 `user/message` 寫入 log。但 step 中途時，當前請求早已完成歷史推導並送出。此時寫入的訊息會讓 log 誤以為模型已經看過，重放時也會重建出一個從未發生的請求。此外，新輸入常常來自 worker thread 中的工具實作，不應直接與 loop 爭用 log 寫入權。而且，一個清單也無法表達輸入是要介入當前 turn，還是開啟下一個 turn。

因此，本章要回答的問題是：為什麼 inbox 需要兩種投遞目標，並且只在 step 邊界認領？

新輸入只能先排入佇列，不能在到達當下直接套用；而它應該介入當前 turn 或開啟新 turn，必須由發送端決定。inbox 因此遵守以下規則：

1. 只投遞，不立即套用：新文字先進待處理清單，不直接寫入 log。插入操作有鎖保護，任何執行緒都能安全呼叫。
2. 兩種目標代表兩種意圖：`next-turn` 會單獨開啟新 turn；`next-step` 則補充目前正在進行的工作。由發送端決定該使用哪一種。
3. 只在 step 邊界認領：待處理輸入必須等到下一次從 log 推導 request 時，才轉成 `user/message`。如此一來，log 不會誤記模型從未收到的內容。
4. 每個 prompt 各自使用一個 turn：開啟 turn 時，系統會取得所有 `next-step` 輸入，以及最多一則 `next-turn` prompt，因此排隊中的 prompt 不會被合併。
5. 有新的介入時不能結束 turn：step 即使已有結束原因，也會再次檢查 `next-step`；只要還有輸入，就在同一個 turn 中繼續下一個 step。
6. 取消時清空對應輸入：`cancel()` 會清空 inbox，避免已取消的 turn 因先前排入的訊息再次啟動。

---

## 核心機制

本章新增 `inbox.py`，並讓 loop 的所有輸入先經過 inbox：

- **`Inbox`**：在鎖的保護下維護兩份有順序的待處理清單。`insert(target, message)` 可由任何執行緒安全加入輸入；`claim(target)` 會取走所有 `next-step`，若正要開啟 turn，則再取一則 `next-turn` prompt。
- **`send(text, target, wakeup)`**：唯一的投遞入口。`followup()`、`steer()`、 `inject()` 是它的三個現成組合。
- **`_drain()`**：負責持續驅動 turn，直到沒有排隊中的 prompt。一次喚醒就能依序處理當時累積的所有 prompt。
- **收 turn 前的再確認**：一個 turn 要結束，條件是某個 step 帶著結束原因收尾，而且就在那一刻 `next-step` 是空的。

這三個現成組合的差別，只在投到哪裡：

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

`send()` 會先將訊息放入 inbox，只有 agent 閒置時才喚醒 drain loop。若 turn 正在執行，工具實作或 bus listener 送來的訊息只會排隊，等 loop 到達下一個 step 邊界再認領。

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

進到 `_step(target)` 之後，第一件事就是認領，位置剛好就在第 04 章本來就會把所有東西重新推導一次的地方：

```python
self.session.append("step/start", {})
for message in self.inbox.claim(target):
    self.session.append("user/message", message)
messages = self.session.derive_messages()  # re-derived, never cached
```

放進來隨時都行；認領只發生在邊界：

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

以下是實際執行時的 log。`read` 的實作送出一則介入訊息，並排入兩則後續 prompt，這些操作都來自 worker thread：

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

介入訊息會在下一個邊界以 seq 9 進入當前 turn。兩則後續 prompt 不會合併，而是各自開啟 turn，因此一次喚醒共執行三個 turn。任何時間重建歷史時，log 中的每筆 `user/message` 都確實曾送給模型。

### 改了什麼

與第 06 章相比：

- `kernel.py`、`message.py`、`scheduler.py`、`session_log.py`、`standin.py`、 `tools.py` 完整沿用。`inbox.py` 是唯一的新原始碼檔案；其他改動都是把 inbox 接進 `agent_loop.py`，因此與第 06 章相比，diff 只包含本章新增的機制，不包含其他改動。
- `agent_loop.py`：`send()` 改成走 inbox，不再自己追加 `user/message`，並且多了 `target` 和 `wakeup` 兩個參數，還有 `followup()` / `steer()` / `inject()` 三個現成組合。那個「agent 正在跑 turn」的 RuntimeError 沒了：turn 中途送進來的東西會排隊，不會丟出例外。現在一次 `send()` 會把排隊的 prompt 全跑完才回來。 `cancel()` 也會把 inbox 清空。
- log 的長相變了：`user/message` 現在落在認領它的那個 step 裡面，接在 `step/start` 後面，而不是在 `turn/start` 之前。輸入只有被認領，才進得了對話紀錄。
- `demo.py`：實機示範在閒著的時候用 `inject()` 先把 context 擺著，接著在 turn 中途從 bus 上的 listener 介入，並排一則後續 prompt，所以一次 send 就能在實際模型上把三種投遞方式都演一遍。

---

## 對照真正的 dsh

以下連結皆指向研究版本 [`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca)。inbox 位於 agent 這個套件裡，認領的位置則在 loop 裡： [`packages/core/agent`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent)。

| Mini-dsh | 真正的 dsh | 說明 |
| --- | --- | --- |
| `inbox.py` 裡的 `Inbox` | [`packages/core/agent/src/inbox.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent/src/inbox.ts)：`Inbox` | 每個 agent 兩份有順序的待處理清單；`InboxTarget = 'next-turn' \| 'next-step'` 宣告在 [`types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent/src/types.ts) 裡。 |
| `claim(target)` | [`inbox.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent/src/inbox.ts)：`Inbox.claim` | 規則一樣：先拿走 next-step 的全部輸入，如果這個邊界要開一個 turn，再多拿一則排隊的 prompt。它被寫成 loop 在 step 邊界上的操作，不是給 plugin 用的擴充點。 |
| `send(text, target, wakeup)` | [`packages/core/agent/src/runtime-types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent/src/runtime-types.ts)：`Agent.send` | 統一的投遞入口；`followup`、`steer`、`inject` 是參數固定好的別名，跟 mini 那三行一模一樣。 |
| 收 turn 前的再確認 | [`packages/core/agent-loop/src/agent.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-loop/src/agent.ts) | 一個 turn 要收掉，條件是某個 step 帶著結束原因收尾，而且 `inbox.nextStep` 是空的；這個確認排在 `agent/turn-stopping` 這個 serial hook 之後，讓它有最後一次介入的機會。 |
| `cancel()` 清空 inbox | [`runtime-types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent/src/runtime-types.ts)：`CancelOptions` | `cancel(cause)` 會把排隊的和介入用的東西一起清掉，除非 `keepInbox` 要求留著；`clear()` 先清 next-step，再清 next-turn。 |
| `_drain()` | [`agent.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-loop/src/agent.ts)：`kick()` | 驅動的那一段會先把排隊的工作跑完才收工，而 `running` 會橫跨連續好幾個排隊的 turn，所以它不能拿來證明某個 turn 還開著。 |

真正的 inbox 還提供以下功能：

- **撐得過重啟。**每一次變動都會追加一筆正規化的 `agent/inbox/spliced` session 事件，而記憶體裡那兩份清單，是回頭讀這些紀錄重建出來、只重放一次的投影，所以待處理的輸入撐得過一次重啟。mini 的 inbox 只活在記憶體裡：它的 log 只有一個寫入者（第 06 章），放進來的動作又發生在工作執行緒上，所以只有被認領的訊息才進得了 log。
- **待處理訊息有 id，也能修改。** 真正的 dsh 會為訊息分配 id，在認領前可透過 `replace()` 或 `remove()` 修改；每次變動都會即時發布 `agent/inbox/inserted`、`claimed` 或 `discarded` 事件。Mini-dsh 沒有為待處理訊息命名，加入後只能等待認領。
- **認領和 step 之間有一個 hook。**`agent/pre-step` 這個 waterfall 可以否決一個提議中的 step，也可以改寫剛認領到的那一批訊息；被否決的 step 會把它認領到的訊息就地結束，然後一個 step 都不跑就把 turn 收掉。mini 這邊只要認領到，就一定會進去。
- **喚醒有一道閂。**真正的喚醒跟放入是分開的：喚醒如果落在一段被中止的活動裡，會改指向 `next-turn` 並且被閂住，等驅動的那一段收斂到閒置狀態再重放一次。mini 的喚醒就一行，「閒著就 drain」，之所以安全，是因為只有驅動的那條執行緒會看到閒置這件事。
- **按介入鍵的是人。**在真正的 dsh 裡，介入通常來自 UI，而 UI 超出本教學的實作範圍； mini 是從 tool 的實作和 bus 上的 listener 去按 `steer()` 和 `followup()`， `inject()` 則是從腳本按的。

---

## 常見失敗模式

- **輸入到達時立即寫入，會讓 log 與實際請求不一致。** step 中途時，當前 request 已完成歷史推導；此時新增訊息，重放後會像是模型曾看過它。固定在邊界認領，才能確保 log 只記錄真正送出的內容。
- **單一清單無法區分兩種意圖。** 後續問題應開新 turn，介入訊息則應影響目前工作。發送端最清楚自己的意圖，因此必須明確指定目標。
- **一次認領所有 prompt 會把多段對話合併。** turn 開始時最多取得一則 `next-turn`，所以三則排隊 prompt 會產生三個 turn 與三個回答，而不是被合成一個過長輸入。
- **結束 turn 前若不再次檢查，最後到達的介入可能永遠等待。** 收尾前查看 `next-step`，只要有新輸入，就在原 turn 中再執行一個 step。
- **取消後保留舊 inbox，可能讓已取消工作再次啟動。** `cancel()` 會先清空兩份清單；取消後才送達的訊息則正常排隊，形成一次新的執行。
- **worker thread 直接寫入 user 事件會破壞單一寫入者原則。** inbox 的插入只修改受鎖保護的記憶體，只有 loop 執行緒會在認領後將訊息寫入 log。

---

## 動手驗證

[`src/`](src/) 延續第 06 章，並加入：

- [`inbox.py`](src/inbox.py)（新的）：`Inbox`，一把鎖後面兩份待處理清單； `insert`、`claim`、`has`、`clear`。
- [`agent_loop.py`](src/agent_loop.py)：`send()` 改走 inbox，多了 `target` 和 `wakeup`；`followup()`、`steer()`、`inject()`；drain 的 loop；每個 step 邊界上的認領；收 turn 前的再確認；`cancel()` 會清空 inbox。
- [`test.py`](src/test.py)：tool 的實作介入它自己所在的那個 turn，又排了兩則 prompt，每一則各拿到一個 turn；閒著時 `inject()` 不會動到 log，要等下一次喚醒先來認領；介入如果落在一個已經完成的 step 期間，那個 turn 會再多開一個 step；cancel 會把所有待處理的東西丟掉，而下一次 send 從乾淨的狀態重新開始。
- [`demo.py`](src/demo.py)：實機示範在閒著的時候先把 context 擺進去，接著在 turn 中途從 bus 上介入、排一則後續 prompt，最後把 log 自己記下的這三種投遞方式印出來。

```bash
python sections/07-inbox/src/test.py    # offline check, no key
```

實機示範需要根目錄的 `requirements.txt` 和一把 key；沒有設定 key 時會自動跳過：

```bash
pip install -r requirements.txt         # anthropic + python-dotenv
cp .env.example .env                    # then set ANTHROPIC_API_KEY
python sections/07-inbox/src/demo.py
```

---

## 參考資料

- [`docs/agent-lifecycle.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/agent-lifecycle.md)： dsh 自己畫的一個 turn，連認領的位置和 inbox 事件都畫進去了。
- [`docs/subsystems/core.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/subsystems/core.md)： Agent 對外的介面、三個現成的別名，還有把 inbox 當成一整套投遞詞彙來介紹的那一段。
- [`.agents/notes/implemented/architecture/2026-07-30-followup-enqueue-and-owned-runs.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/.agents/notes/implemented/architecture/2026-07-30-followup-enqueue-and-owned-runs.md)：那份設計筆記，講的是為什麼 `followup()` 不回傳任何 handle。
