<!-- source: README.md @ 55e829b -->

# 04 · Agent loop

[English](README.md) | 繁體中文 | [简体中文](README.zh-CN.md)

> 這段負責推進流程的程式碼，要接住輸入、問 model、把答案寫下來。它一記對話，就會多出第二份真相，所以自己什麼都不記。

Section 00 到 03 做出了一份 session log：它能推導出 model 看到的歷史，能一個 chunk 一個 chunk 接住回應，也能 compact。但沒有東西在推動它。到目前為止，每次檢查都得自己手動把對話一則一則接下去，每一則訊息都自己 append 進去。

還缺的是那台機器：接住使用者打的字，呼叫 model，把回應記下來，一直重複到事情做完為止。這台機器就是 agent loop，mini-dsh 把它跑一次叫做一個 **turn**，一個 turn 由一個或多個 **step** 組成。

最直覺的做法，是在記憶體裡留一份活的訊息清單。使用者說了什麼就 append 進去，model 回了什麼也 append 進去，每次要問 model 就把整份清單交出去。不用推導，不用投影，就是一個會愈長愈大的 Python list。

但那份清單等於把真相又抄了一份。compaction（Section 03）會在它背後偷偷改 surface。程式一崩，那份清單就沒了。要接續一個 session，得先把它重建出來，然後祈禱重建的內容跟 model 當初真的看到的一樣。

所以：為什麼每一個 step 都要重新組一次 prompt、重新推一次歷史？

因為 log 本來就是唯一持久的狀態，loop 應該靠著它，而不是跟它搶著當真相。要做到這件事，loop 必須：

1. 把一個 **turn** 跑成一連串 **step**：`send()` 會一直往下 step，直到某個 step 交出的是一個結束理由，而不是還有事要做。
2. 每個 step 一開始就先從 session log 重新推導出 model 的歷史，一次都不留舊的，接著透過 Model seam 呼叫 model 一次，把吐回來的每個 chunk 和最後那則訊息都 append 回去。
3. 把 turn 和 step 的邊界寫成 log 事件（`turn/start`、`step/start`、`step/end`、`turn/end`），這些只進 log，這樣光看 log 就知道整個故事。
4. 每個 step 都寫一行 `request/header`，記下這次送出去了什麼，這樣 log 自己就能證明 model 當時被餵了什麼。
5. Agent 這個物件上不留任何持久的東西：任何一個 Agent 只要接到同一份 log，都能接得一模一樣，所以接續就等於把 log 重放一遍，再配一個新的 Agent。

---

## Mechanism

一個新檔案 `agent_loop.py`，裡面三個零件：

- **`Agent.send()`**：一個 turn。先 append 使用者的訊息和 `turn/start`，然後一直 step，直到某個 step 交出結束理由，最後 append `turn/end`。
- **`Agent._step()`**：一個 step。推導歷史，記下自己準備送出去的東西，呼叫 model 並把回應一段一段收回來，全部 append 回去，再交代自己是怎麼結束的。
- **`AgentRegistry`**：由 plugin 提供的 `agents` service，跟 Section 02 的 `sessions` service 是同一套做法。

一個 turn 就是一個 while 迴圈，離開的條件就是 step 給的答案：

```python
def send(self, text):
    """One turn: the user's message in, steps until one ends with a reason."""
    if self.status == "running":
        raise RuntimeError("agent is mid-turn; the log allows one story at a time")
    self.status = "running"
    try:
        self.session.append("user/message", {"content": text})
        self.session.append("turn/start", {})
        while self._step() is None:
            pass
        self.session.append("turn/end", {})
    finally:
        self.status = "idle"
```

而設計問題的答案就在 step 裡面，一行就講完了：

```python
def _step(self):
    """One step: re-derive history, one model call, append it all back."""
    self.session.append("step/start", {})
    messages = self.session.derive_messages()  # re-derived, never cached
    self.session.append("request/header", {"messages": len(messages)})
    for kind, value in self.model(messages):
        if kind == "chunk":
            self.session.append("assistant/chunk", {"text": value})
        else:
            self.session.append("assistant/message", {"content": value.content})
    reason = "completed"
    self.session.append("step/end", {"reason": reason})
    return reason
```

`derive_messages()` 是在 step 裡面跑的，跑在 `step/start` 寫進去之後。step 自己不持有歷史，它只是跟 log 借一份，而且借來只夠用在一次 model 呼叫上。

下面是一段對話的第二個 turn，log 是這樣記的：

```text
send("and now?")
  │  10  user/message      {"content": "and now?"}
  │  11  turn/start
  │
  ├─ step ─────────────────────────────────────────────
  │  12  step/start
  │      derive_messages()          ◄── read the log, fresh
  │  13  request/header    {"messages": 3}
  │  14  assistant/chunk   ┐
  │  15  assistant/chunk   │ streamed through the Model seam
  │  16  assistant/chunk   ┘
  │  17  assistant/message {"content": "Now this."}
  │  18  step/end          {"reason": "completed"}
  ├─ reason is "completed" ► leave the loop
  │
  │  19  turn/end
```

每一行都是在 Section 02 那個 session 上做一次 `append()`。那些邊界標記和 header 都只進 log（`surface_op` 是 `None`），所以 model 永遠看不到它們；`derive_messages()` 拿回來的還是只有真正的訊息。

因為 step 每次都重讀 log，其他 Mechanism 不用特別做什麼就搭得起來。在兩個 turn 之間做一次 compact（Section 03），下一行 `request/header` 記下的數字就會變小：step 推導出來的是壓縮過的視角，因為 log 現在就是投影成那樣。沒有人去通知 loop 發生過 compaction。也不需要。

出事的時候，這一招一樣划算。model 呼叫跑到一半死掉，log 上會留下 `step/start`、一行 `request/header`、幾個沒下文的 chunk，然後就沒了。不需要任何修補步驟：chunk 只進 log，所以下一次推導出來的歷史本來就是乾淨的，而 Offline check 就是故意在 chunk 還在往回吐的時候把 model 弄死，用這個來證明。

接續的時候也划算。Agent 身上就只有一個 session、一個 Model seam 的 callable，還有一個 `status` 旗標，而那個旗標只表示「現在正在一個 turn 中間」。把 log 重放進一個新的 session，交給一個全新的 Agent，接下來那個 turn 寫進去的每一行，會跟原本那個 Agent 會寫的一模一樣。

有一件事要老實說：這個 section 做到的是重新推導歷史，設計問題裡「重新組 prompt」那一半還在後面。在 Section 08 把 system prompt 做出來之前，mini 送出去的請求就只有推導出來的訊息而已。

### 改了什麼

跟 Section 03 比起來：

- `kernel.py`、`message.py`、`session_log.py`、`standin.py` 都原封不動搬過來；`agent_loop.py` 是唯一新增的原始檔，所以跟 03 的 diff 就是這個 section 的 Mechanism，沒有別的。
- 03 的檢查裡那個要手動一步步推的 `stream_turn()` 輔助函式不見了。現在 loop 是真的被測到的程式碼，檢查是透過 `send()` 來推動它。
- 今天這個 while-step 迴圈每個 turn 只會跑一次，因為現在還沒有 tool，每個 step 都以 `"completed"` 結束。這個迴圈的形狀和結束理由，就是 Section 05 要接進來的地方。
- 這是第一個會碰到 model 的 Section，所以 `demo.py` 出現了：同一個 loop，只是把真正的 Anthropic API 接到 Model seam 上（ADR 0001）。

---

## In real dsh

所有指過去的連結都固定在 Studied version [`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca) 上。loop 本身住在 [`packages/core/agent-loop`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-loop)，對外那層 registry 則在 [`packages/core/agent`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent)。

| Mini-dsh | 真正的 dsh | 說明 |
| --- | --- | --- |
| `Agent.send()` 和 `_step()` | [`packages/core/agent-loop/src/agent.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-loop/src/agent.ts)：`ReactLoopAgent` | 真正在跑的那一套是 `kick` -> `turn()` -> `preStep()` -> `step()` -> `buildRequest()`；每個 step 都從 log 重新推導出訊息，也重新組一次 prompt。 |
| `AgentRegistry`，也就是 `agents` service | [`packages/core/agent/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent/src/index.ts)：`AgentRegistry` | `ctx.agents` 裡放的是一個個 `Agent` handle，從外面看不到裡面；真正在跑的那個 loop，是由一個可以換掉的 factory（`setFactory()`）做出來的，而這個 factory 由 `dsh-agent-loop` 註冊。 |
| `status`：`"idle"` 或 `"running"` | [`packages/core/agent/src/runtime-types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent/src/runtime-types.ts)：`AgentStatus` | 一樣是這兩個狀態，只是掛在一個寬得多的 `Agent` seam 介面上（`cancel`、`send`、`followup`、`steer`、`inject`）。 |
| `turn/start`、`step/start`、`step/end`、`turn/end`、`request/header` 這幾行 | [`packages/core/agent-loop/src/agent.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-loop/src/agent.ts) | turn/step 這套持久的詞彙，就是 loop 自己 append 進去的 session 事件，跟這裡一模一樣；`agent/*` 那條 bus 上只有生命週期、inbox 和攔截點。 |
| `_step()` 裡那次 Model seam 呼叫 | [`packages/core/agent-loop/src/agent.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-loop/src/agent.ts)：`ctx.llm.prepareCall()` | 真正的請求會走 llm 這個 capability seam，回應一個 chunk 一個 chunk 傳回來；這個 seam 本身是 Section 10 的 Mechanism。 |

真正的 agent loop 在這個 section 的 Mechanism 之上，還多做了這些：

- **step 豐富得多。** 真正的 step 在開始跟 model 要回應之前，會先認領 inbox、組出 system prompt、投影出 runtime context，再跑一次 `agent/pre-step` 和 `agent/request` 這兩個 waterfall。mini 的 step 只有推導，加上把回應收回來；剩下的由 Section 05 到 09 一個一個補上。
- **step 有更多種結束方式。** 真正的 step 可以用 `completed` 結束（沒有 tool 呼叫）、用 `max-tokens` 結束（一旦是它就會一直留著），或是回 `null`（跑過 tool，再繞一圈）。而一個 turn 要收掉，得同時滿足兩件事：有結束理由，而且在 `agent/turn-stopping` 重新確認過之後 `inbox.nextStep` 是空的。tool 的結果上如果標了 `concludesTurn`，turn 會提早結束。在 Section 05 之前，mini 只有一條分支。
- **整個 loop 都可以換掉。** `Agent` 是一個 seam 介面，`ReactLoopAgent` 只住在套件內部，外面只能透過 factory 拿到它，所以要換掉整個 loop，不必動到任何一個拿著 agent handle 的地方。
- **生命週期都在 bus 上。** `agent/created`、`agent/disposed`、`agent/status` 加上 inbox 那幾個事件，讓在旁邊即時盯著的人跟得上進度，另外還有一個取消用的 token 貫穿全部。mini 這邊是靠寫進 log 的那些邊界標記來說故事；取消要等到 Section 06 的 scheduler 才會出現。

---

## Failure modes

- **快取一份訊息清單，等於把真相抄了第二份。** 歷史一旦存在一份活的清單裡，其他每個 Mechanism 都會變成同步問題：compaction 在它背後改 surface，重放 session 的時候根本不會理它。每個 step 都從 log 推導，就代表從頭到尾沒有東西需要同步。
- **step 中途崩掉，不需要任何修補。** 死掉的 step 會留下一個沒有 `step/end` 的 `step/start`，可能還有幾個沒下文的 chunk。因為 chunk 只進 log，下一次推導出來的東西本來就是乾淨的；檢查會先讓 model 吐一個 chunk，再把它弄死，然後證明下一個 turn 送出去的歷史剛剛好正確。
- **一個 turn 不等於一次 model 呼叫。** 如果把「送出去、回一句、結束」寫死，tool 跑完之後就沒有地方可以繞回來。有一個 while-step 的形狀，加上一個講明白的結束理由，Section 05 才能在不動 turn 的情況下把 tool 加進來。
- **沒有 `request/header`，「model 看到了 X」就只是猜的。** 這一行 header 把每個 step 送出去了什麼，直接寫進 log 裡。檢查會在兩個 turn 之間做一次 compact，然後直接從 log 上讀數字：1，然後 3，compact 之後是 2。不用去翻 stand-in 的內部，看紀錄就好。
- **同一份 log 上跑兩個 turn，故事會交錯在一起。** 一個 turn 還在跑的時候又呼叫 `send()`，會直接丟出例外，而不是把兩套 turn/step 標記編在同一條時間線上。真正的 dsh 會把那則訊息排進 inbox，等到 step 的邊界再認領；那是 Section 07 的 Mechanism。
- **少了邊界標記，重放就分不清楚了。** 沒有 `turn/start` 和 `step/end` 這兩行，重放的人分不出來一個 turn 是好好結束的，還是跑到一半崩掉的。這些邊界是資料，不是隨手印出來 debug 用的東西：有它們，log 才是一個故事，而不是一堆散掉的訊息。

---

## 跑跑看

[`src/`](src/) 把 03 原封不動搬過來，再加上：

- [`agent_loop.py`](src/agent_loop.py)（新增）：帶著 `send()` 和 `_step()` 的 `Agent`、`AgentRegistry`，還有提供 `agents` service 的 plugin。
- [`test.py`](src/test.py)：整個 turn 的故事會照順序落在 log 上；`request/header` 上的數字證明每一步都重新推導，跨過一次 compaction 也一樣（1、3、2）；把 log 重放一遍再配一個新的 Agent，接下去寫的東西一模一樣；step 中途崩掉，下一次推導還是乾淨的；turn 中途再呼叫一次 `send()` 會被拒絕。
- [`demo.py`](src/demo.py)（新增）：第一支 Live demo。同一個 loop，把真正的 Anthropic API 接到 Model seam 上，跑幾個寫好的 turn，中間插一次 compaction，最後把 log 自己的故事印出來。SDK 和 mini-Message 之間的轉換只住在這裡（ADR 0001）。

```bash
python sections/04-agent-loop/src/test.py   # offline check, no key
```

Live demo 需要根目錄的 `requirements.txt` 和一把 key；沒有 key 的話，它會安靜地跳過：

```bash
pip install -r requirements.txt             # anthropic + python-dotenv
cp .env.example .env                        # then set ANTHROPIC_API_KEY
python sections/04-agent-loop/src/demo.py
```

---

## 出處

- [`docs/subsystems/core.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/subsystems/core.md)：dsh 自己寫的文件，講 agent 和 agent-loop 這兩個套件。
- [`docs/agent-lifecycle.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/agent-lifecycle.md)：turn 和 step 的生命週期，從 kick 一路到 turn 結束。
