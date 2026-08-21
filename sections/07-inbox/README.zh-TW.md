<!-- source: README.md @ 55e829b -->

# 07 · Inbox

[English](README.md) | 繁體中文 | [简体中文](README.zh-CN.md)

> 沒有人想等機器安靜下來才開口。直接寫進 log，等於宣稱 model 讀過那些它根本沒收到的話，所以輸入就改成等在 step 之間。

Section 06 的 agent 到現在還是只有一扇門。`send()` 收下一則訊息，跑完一整個 turn 才回來；turn 還沒結束就再呼叫一次 `send()`，會直接丟出例外。使用者想說的每一句話，都得等機器安靜下來才輪得到。

真實世界的輸入不會照表操課。使用者盯著 tool 的結果一則一則刷過去，想改方向就是想現在改，不是等它一路走錯走到底才改。一個跑完的背景工作，想把結果塞進下一次 request。而一個真正的後續問題，應該等自己的那個 turn，不要硬闖進正在跑的這一個。

最直覺的做法，是把進來的文字直接當成一筆 `user/message` 追加到 log 裡。但 step 跑到一半的時候，正在飛的那次 request 早就把歷史推導完了：新加的這一筆會宣稱 model 看過它其實沒收到的字，重放的時候還會重建出一次根本沒送出去過的 request。更麻煩的是，送東西進來的常常是某個 tool 的實作，而且跑在工作執行緒上，Section 06 又已經把 log 定成只有一個寫入者。而且單一份清單講不出一則訊息到底想幹嘛：是要加入正在跑的這份工作，還是要自己開一個 turn。

所以：為什麼 inbox 要有兩個投遞目標，而且只在 step 的邊界認領？

因為進來的輸入只能先投遞、不能當場套用，而且投到哪裡要由送件的人決定。要做到這件事，inbox 必須：

1. 只投遞，不套用：進來的文字先進待處理清單，不進 log。放進去這個動作有鎖保護，任何執行緒都能做，而且不會留下任何一筆紀錄。
2. 兩個目標對應兩種意圖：`next-turn` 是值得單獨開一個 turn 的 prompt； `next-step` 是給正在跑的那份工作的輸入。只有送件的人知道自己要的是哪一種。
3. 只在 step 的邊界認領：待處理的輸入，要等到「下一次 request 從 log 重新推導出來」的那個位置才變成 `user/message`，這是 Section 04 的規則。這樣對話紀錄就不會宣稱 model 看過它沒看過的字。
4. 每一則 prompt 各拿一個 turn：開啟 turn 的那次認領，會拿走所有待處理的 `next-step` 輸入，外加最多一則排隊中的 prompt，所以排隊的 prompt 永遠不會被併在一起。
5. 有新的介入就不能收掉 turn：一個帶著結束原因收尾的 step，會再去看一次 `next-step`；只要那裡有東西，就在同一個 turn 裡再多跑一個 step。
6. 跟著它瞄準的那個 turn 一起消失：`cancel()` 會把 inbox 清空，所以被中止的 turn 不會拿取消之前排的輸入重新開跑。

---

## Mechanism

一個新檔案 `inbox.py`，再把 loop 的大門改道，讓它走這裡：

- **`Inbox`**：一把鎖後面放兩份有順序的待處理清單。`insert(target, message)` 讓任何執行緒都能把輸入放進來；`claim(target)` 會清空 `next-step`，如果這個邊界正要開一個 turn，就再多拿走剛好一則 `next-turn` 的 prompt。
- **`send(text, target, wakeup)`**：唯一的投遞入口。`followup()`、`steer()`、 `inject()` 是它的三個現成組合。
- **`_drain()`**：負責驅動的那一段。它一個 turn 接一個 turn 跑，跑到沒有排隊的 prompt 才閒下來，所以喚醒一次，後面排隊的每一則 prompt 都輪得到。
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

`send()` 先把東西放進去，只有在 agent 閒著的時候才去喚醒 drain 的那個 loop。 turn 跑到一半時，tool 的實作或 bus 上的 listener 呼叫進來，就只是排隊而已：驅動的那條執行緒正忙在某個 step 裡面，等之後的某個邊界再來認領。

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

進到 `_step(target)` 之後，第一件事就是認領，位置剛好就在 Section 04 本來就會把所有東西重新推導一次的地方：

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

下面是一次真的執行，照 log 記下來的樣子。`read` 的實作介入了一次，又排了兩則後續 prompt，全都是從它那條工作執行緒發出來的：

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

那則介入在 seq 9 進到正在跑的 turn 裡，跟它被送出去的時間點差了一個邊界。兩則後續 prompt 沒有被併在一起：一則 prompt 一個 turn，喚醒一次跑出三個 turn。隨便挑一個時間點把歷史推導出來，每一筆說 model 看過的 `user/message`，它就是真的看過。

### 改了什麼

跟 Section 06 比起來：

- `kernel.py`、`message.py`、`scheduler.py`、`session_log.py`、`standin.py`、 `tools.py` 原封不動搬過來。`inbox.py` 是唯一的新原始碼檔案；其他改動都是把 inbox 接進 `agent_loop.py`，所以跟 06 的 diff 剛好就是這個 Section 的 Mechanism，沒有別的。
- `agent_loop.py`：`send()` 改成走 inbox，不再自己追加 `user/message`，並且多了 `target` 和 `wakeup` 兩個參數，還有 `followup()` / `steer()` / `inject()` 三個現成組合。那個「agent 正在跑 turn」的 RuntimeError 沒了：turn 中途送進來的東西會排隊，不會丟出例外。現在一次 `send()` 會把排隊的 prompt 全跑完才回來。 `cancel()` 也會把 inbox 清空。
- log 的長相變了：`user/message` 現在落在認領它的那個 step 裡面，接在 `step/start` 後面，而不是在 `turn/start` 之前。輸入只有被認領，才進得了對話紀錄。
- `demo.py`：Live demo 在閒著的時候用 `inject()` 先把 context 擺著，接著在 turn 中途從 bus 上的 listener 介入，並排一則後續 prompt，所以一次 send 就能在真的 model 上把三種投遞方式都演一遍。

---

## In real dsh

所有指過去的連結都固定在 Studied version [`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca) 上。inbox 住在 agent 這個套件裡，認領的位置則在 loop 裡： [`packages/core/agent`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent)。

| Mini-dsh | 真正的 dsh | 說明 |
| --- | --- | --- |
| `inbox.py` 裡的 `Inbox` | [`packages/core/agent/src/inbox.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent/src/inbox.ts)：`Inbox` | 每個 agent 兩份有順序的待處理清單；`InboxTarget = 'next-turn' \| 'next-step'` 宣告在 [`types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent/src/types.ts) 裡。 |
| `claim(target)` | [`inbox.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent/src/inbox.ts)：`Inbox.claim` | 規則一樣：先拿走 next-step 的全部輸入，如果這個邊界要開一個 turn，再多拿一則排隊的 prompt。它被寫成 loop 在 step 邊界上的操作，不是給 plugin 用的擴充點。 |
| `send(text, target, wakeup)` | [`packages/core/agent/src/runtime-types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent/src/runtime-types.ts)：`Agent.send` | 統一的投遞入口；`followup`、`steer`、`inject` 是參數固定好的別名，跟 mini 那三行一模一樣。 |
| 收 turn 前的再確認 | [`packages/core/agent-loop/src/agent.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-loop/src/agent.ts) | 一個 turn 要收掉，條件是某個 step 帶著結束原因收尾，而且 `inbox.nextStep` 是空的；這個確認排在 `agent/turn-stopping` 這個 serial hook 之後，讓它有最後一次介入的機會。 |
| `cancel()` 清空 inbox | [`runtime-types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent/src/runtime-types.ts)：`CancelOptions` | `cancel(cause)` 會把排隊的和介入用的東西一起清掉，除非 `keepInbox` 要求留著；`clear()` 先清 next-step，再清 next-turn。 |
| `_drain()` | [`agent.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-loop/src/agent.ts)：`kick()` | 驅動的那一段會先把排隊的工作跑完才收工，而 `running` 會橫跨連續好幾個排隊的 turn，所以它不能拿來證明某個 turn 還開著。 |

真正的 inbox 在這個 Section 的 Mechanism 之上，還多做了這些：

- **撐得過重啟。**每一次變動都會追加一筆正規化的 `agent/inbox/spliced` session 事件，而記憶體裡那兩份清單，是回頭讀這些紀錄重建出來、只重放一次的投影，所以待處理的輸入撐得過一次重啟。mini 的 inbox 只活在記憶體裡：它的 log 只有一個寫入者（Section 06），放進來的動作又發生在工作執行緒上，所以只有被認領的訊息才進得了 log。
- **有身分，也能改。**真正的待處理訊息帶著 id，在被認領之前可以 `replace()` 或 `remove()`；每一次改動都會即時發成 `agent/inbox/inserted`、`claimed` 或 `discarded`。mini 放進去的東西沒有名字，一旦歸檔就只能等。
- **認領和 step 之間有一個 hook。**`agent/pre-step` 這個 waterfall 可以否決一個提議中的 step，也可以改寫剛認領到的那一批訊息；被否決的 step 會把它認領到的訊息就地結束，然後一個 step 都不跑就把 turn 收掉。mini 這邊只要認領到，就一定會進去。
- **喚醒有一道閂。**真正的喚醒跟放入是分開的：喚醒如果落在一段被中止的活動裡，會改指向 `next-turn` 並且被閂住，等驅動的那一段收斂到閒置狀態再重放一次。mini 的喚醒就一行，「閒著就 drain」，之所以安全，是因為只有驅動的那條執行緒會看到閒置這件事。
- **按介入鍵的是人。**在真正的 dsh 裡，介入通常來自 UI，而 UI 在 Ceiling 之上； mini 是從 tool 的實作和 bus 上的 listener 去按 `steer()` 和 `followup()`， `inject()` 則是從腳本按的。

---

## Failure modes

- **輸入一到就套用，對話紀錄會說謊。**正在飛的那次 request 早就把歷史推導完了，所以 step 中途追加的那一筆，會說 model 看過它其實沒收到的字，重放的時候還會重建出一次根本沒送出去過的 request。認領固定落在邊界上，反正下一次 request 本來就在那裡重新推導；這樣 log 才留得住真正發生過的事。
- **一份清單會把兩種意圖壓成一種。**把 prompt 全折進正在跑的 turn，一個後續問題就會把手上的工作整個搶走；把介入全延到下一個 turn，它又會在 agent 一路走錯走到底之後才到。目標由送件的人指定，因為只有他知道自己要的是哪一種。
- **一次認領所有排隊的 prompt，等於把好幾段對話併成一段。**開啟 turn 的那次認領最多只拿一則 `next-turn` 訊息，所以三則排隊的 prompt 會變成三個 turn、三個答案，而不是一則塞得滿滿的大 prompt 配一個含糊的答案。
- **收 turn 之前不再確認一次，最後一秒的介入會被晾在那裡。**在最後一個 step 快收尾時才進來的輸入，會躺在 `next-step` 裡，等一個可能永遠不會來的喚醒。收之前先看一眼清單：有新的介入，就在它原本瞄準的那個 turn 裡再多跑一個 step。
- **inbox 熬得過 cancel，被取消的工作就會復活。**`cancel()` 會在中止之前把兩份清單都清空，所以取消之前排的輸入，跟著那個 turn 一起消失。取消之後才送的照常排隊，drain 的 loop 會接手：那是一次乾淨的重新開始，不是被取消的那次又爬回來。
- **工作執行緒自己去寫 user 紀錄，會跟 log 的寫入搶成一團。**Section 06 把 log 定成只有一個寫入者，inbox 也守住這件事：放進來的動作有鎖保護、只動記憶體，而且只有驅動的那條執行緒，會把認領到的東西變成紀錄。

---

## 跑跑看

[`src/`](src/) 把 06 搬過來，然後加上：

- [`inbox.py`](src/inbox.py)（新的）：`Inbox`，一把鎖後面兩份待處理清單； `insert`、`claim`、`has`、`clear`。
- [`agent_loop.py`](src/agent_loop.py)：`send()` 改走 inbox，多了 `target` 和 `wakeup`；`followup()`、`steer()`、`inject()`；drain 的 loop；每個 step 邊界上的認領；收 turn 前的再確認；`cancel()` 會清空 inbox。
- [`test.py`](src/test.py)：tool 的實作介入它自己所在的那個 turn，又排了兩則 prompt，每一則各拿到一個 turn；閒著時 `inject()` 不會動到 log，要等下一次喚醒先來認領；介入如果落在一個已經完成的 step 期間，那個 turn 會再多開一個 step；cancel 會把所有待處理的東西丟掉，而下一次 send 從乾淨的狀態重新開始。
- [`demo.py`](src/demo.py)：Live demo 在閒著的時候先把 context 擺進去，接著在 turn 中途從 bus 上介入、排一則後續 prompt，最後把 log 自己記下的這三種投遞方式印出來。

```bash
python sections/07-inbox/src/test.py    # offline check, no key
```

Live demo 需要根目錄的 `requirements.txt` 和一把 key；沒有 key 的話，它會安靜地跳過：

```bash
pip install -r requirements.txt         # anthropic + python-dotenv
cp .env.example .env                    # then set ANTHROPIC_API_KEY
python sections/07-inbox/src/demo.py
```

---

## 出處

- [`docs/agent-lifecycle.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/agent-lifecycle.md)： dsh 自己畫的一個 turn，連認領的位置和 inbox 事件都畫進去了。
- [`docs/subsystems/core.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/subsystems/core.md)： Agent 對外的介面、三個現成的別名，還有把 inbox 當成一整套投遞詞彙來介紹的那一段。
- [`.agents/notes/implemented/architecture/2026-07-30-followup-enqueue-and-owned-runs.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/.agents/notes/implemented/architecture/2026-07-30-followup-enqueue-and-owned-runs.md)：那份設計筆記，講的是為什麼 `followup()` 不回傳任何 handle。
