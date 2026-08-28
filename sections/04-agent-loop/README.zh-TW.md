<!-- source: README.md @ 3705bd7 -->

# 04 · Agent loop

[English](README.md) | 繁體中文 | [简体中文](README.zh-CN.md)

> agent loop 負責接收輸入、呼叫模型，並寫入回覆。但如果 loop 自己也保留一份對話歷史，系統就會出現第二個真相來源。因此 loop 只負責推進流程，不另外儲存歷史。

第 00 到 03 章已經建立了 session log。它能推導模型歷史、記錄串流 chunk，也支援 compaction，但目前還沒有元件會主動推進對話。所有檢查都得手動接續對話，自己把每則訊息 append 到 log。

現在缺少的是 agent loop：它接收使用者輸入、呼叫模型、記錄回覆，並持續執行到任務完成。Mini-dsh 將一次完整互動稱為 **turn**，而一個 turn 可以包含一個或多個 **step**。

最簡單的做法，是在記憶體中維護一份訊息清單。使用者和模型的每則訊息都追加進去，每次呼叫模型時再送出整份清單。

但這份清單會與 session log 重複。第 03 章的 compaction 會更新 surface，但記憶體清單不會自動同步；程式中斷後，它也會直接消失。恢復 session 時，系統還必須另外重建這份清單，並確保它與模型當時看到的內容完全一致。

因此，本章要回答的問題是：為什麼每個 step 都必須重新組裝 prompt，並從 log 重新推導歷史？

log 本來就是唯一可持久的狀態，loop 應該以它為依據，而不是自己維護另一份真相。具體規則如下：

1. 一個 **turn** 由多個 **step** 組成。`send()` 會持續執行 step，直到某個 step 回傳明確的結束原因。
2. 每個 step 都會從 session log 重新推導模型歷史，透過 Model seam 呼叫模型，並將所有 chunk 與最終訊息寫回 log。
3. turn 和 step 的邊界都會寫成 log 事件：`turn/start`、`step/start`、`step/end` 和 `turn/end`。它們不會進入模型歷史，但能完整描述執行過程。
4. 每個 step 都會記錄 `request/header`，說明這次請求實際送出的內容。
5. Agent 物件不儲存任何持久狀態。只要重放相同的 log，新建立的 Agent 就能從同一位置繼續執行。

---

## 核心機制

`agent_loop.py` 包含三個核心元件：

- **`Agent.send()`**：負責一個 turn。它會先 append 使用者訊息與 `turn/start`，接著持續執行 step，直到取得明確的結束原因，最後 append `turn/end`。
- **`Agent._step()`**：負責一個 step。它會推導歷史、記錄請求內容、呼叫模型、逐段接收回覆並寫回 log，最後記下結束原因。
- **`AgentRegistry`**：由 plugin 提供的 `agents` service，與第 02 章的 `sessions` service 是同一套做法。

turn 的主體是一個 while 迴圈，是否結束由 step 的回傳值決定：

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

重新推導歷史的關鍵就在 step 中：

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

`derive_messages()` 會在寫入 `step/start` 後執行。step 本身不保存歷史，只在每次模型呼叫前從 log 取得當下的推導結果。

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

上面每一行都對第 02 章的 session 執行一次 `append()`。邊界標記與 header 只寫入 log（`surface_op` 為 `None`），不會出現在模型歷史；`derive_messages()` 仍只會取得真正的對話訊息。

因為每個 step 都重新讀取 log，其他機制不需額外同步。若在兩個 turn 之間進行 compaction（第 03 章），下一個 `request/header` 中的訊息數量自然會減少。loop 不需要接收 compaction 通知，因為它每次看到的本來就是最新投影。

模型呼叫中途失敗時，log 可能留下 `step/start`、`request/header` 和幾個尚未完成的 chunk。這些 chunk 不會進入 surface，因此下次推導出的模型歷史仍然完整，不需要額外修補。離線測試會刻意讓模型在產生 chunk 後失敗，驗證這項行為。

恢復執行也很直接。Agent 只持有 session、Model seam callable，以及表示是否正在執行 turn 的 `status`。將 log 重放到新的 session，再建立新的 Agent，後續 turn 便能從相同狀態繼續。

本章先完成「重新推導歷史」；「重新組裝 prompt」則會在第 08 章加入 system prompt 後補齊。目前 Mini-dsh 送出的請求只有推導出的訊息。

### 改了什麼

與第 03 章相比：

- `kernel.py`、`message.py`、`session_log.py`、`standin.py` 都完整沿用；`agent_loop.py` 是唯一新增的原始檔，因此與第 03 章相比，diff 只包含本章新增的機制，不包含其他改動。
- 第 03 章測試中手動推進流程的 `stream_turn()` 輔助函式已移除；現在測試直接透過 `send()` 驗證真正的 loop。
- 目前每個 turn 只會執行一個 step，因為尚未加入工具，每個 step 都以 `"completed"` 結束。第 05 章會利用同一個迴圈，在工具執行後繼續下一個 step。
- 本章首次實際呼叫模型，因此加入 `demo.py`，使用相同 loop 連接 Anthropic API。

---

## 對照真正的 dsh

以下連結皆指向研究版本 [`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca)。loop 本身位於 [`packages/core/agent-loop`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-loop)，對外那層 registry 則在 [`packages/core/agent`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent)。

| Mini-dsh | 真正的 dsh | 說明 |
| --- | --- | --- |
| `Agent.send()` 和 `_step()` | [`packages/core/agent-loop/src/agent.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-loop/src/agent.ts)：`ReactLoopAgent` | 真正在跑的那一套是 `kick` -> `turn()` -> `preStep()` -> `step()` -> `buildRequest()`；每個 step 都從 log 重新推導出訊息，也重新組一次 prompt。 |
| `AgentRegistry`，也就是 `agents` service | [`packages/core/agent/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent/src/index.ts)：`AgentRegistry` | `ctx.agents` 裡放的是一個個 `Agent` handle，從外面看不到裡面；真正在跑的那個 loop，是由一個可以換掉的 factory（`setFactory()`）做出來的，而這個 factory 由 `dsh-agent-loop` 註冊。 |
| `status`：`"idle"` 或 `"running"` | [`packages/core/agent/src/runtime-types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent/src/runtime-types.ts)：`AgentStatus` | 一樣是這兩個狀態，只是掛在一個寬得多的 `Agent` seam 介面上（`cancel`、`send`、`followup`、`steer`、`inject`）。 |
| `turn/start`、`step/start`、`step/end`、`turn/end`、`request/header` 這幾行 | [`packages/core/agent-loop/src/agent.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-loop/src/agent.ts) | turn/step 這套持久的詞彙，就是 loop 自己 append 進去的 session 事件，跟這裡一模一樣；`agent/*` 那條 bus 上只有生命週期、inbox 和攔截點。 |
| `_step()` 裡那次 Model seam 呼叫 | [`packages/core/agent-loop/src/agent.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-loop/src/agent.ts)：`ctx.llm.prepareCall()` | 真正的請求會走 llm 這個 capability seam，回應一個 chunk 一個 chunk 傳回來；這個 seam 本身是第 10 章的機制。 |

真正的 agent loop 還提供以下功能：

- **step 豐富得多。** 真正的 step 在開始跟 model 要回應之前，會先認領 inbox、組出 system prompt、投影出 runtime context，再跑一次 `agent/pre-step` 和 `agent/request` 這兩個 waterfall。mini 的 step 只有推導，加上把回應收回來；剩下的由第 05 章到 09 一個一個補上。
- **step 有更多種結束方式。** 真正的 step 可以用 `completed` 結束（沒有 tool 呼叫）、用 `max-tokens` 結束（一旦是它就會一直留著），或是回 `null`（跑過 tool，再繞一圈）。而一個 turn 要收掉，得同時滿足兩件事：有結束理由，而且在 `agent/turn-stopping` 重新確認過之後 `inbox.nextStep` 是空的。tool 的結果上如果標了 `concludesTurn`，turn 會提早結束。在第 05 章之前，mini 只有一條分支。
- **整個 loop 都可以換掉。** `Agent` 是一個 seam 介面，`ReactLoopAgent` 只位於套件內部，外面只能透過 factory 拿到它，所以要換掉整個 loop，不必動到任何一個拿著 agent handle 的地方。
- **生命週期事件都位於 bus。** `agent/created`、`agent/disposed`、`agent/status` 與 inbox 事件可供外部即時追蹤進度，另有取消 token 貫穿整個流程。Mini-dsh 則以 log 中的邊界標記記錄生命週期，第 06 章才會加入 scheduler 取消機制。

---

## 常見失敗模式

- **快取訊息清單會形成第二個真相來源。** compaction 更新 surface 後，快取內容不會自動同步；重放 session 時也無法保證一致。每個 step 都從 log 推導，就不需要維護額外副本。
- **step 中途失敗不需要修補歷史。** 失敗的 step 可能只有 `step/start` 和幾個 chunk，沒有 `step/end`。由於 chunk 只進 log，下一次推導仍會得到乾淨的訊息歷史。
- **一個 turn 不一定只有一次模型呼叫。** 若流程固定為「送出一次、回覆一次、立即結束」，工具執行後就無法回到模型。while-step 結構與明確結束原因，讓第 05 章可以直接加入工具分支。
- **缺少 `request/header` 就無法確認模型實際收到什麼。** header 會把每個 step 的請求摘要寫入 log。測試在兩個 turn 間執行 compaction，並直接從紀錄確認訊息數量依序為 1、3、2。
- **同一份 log 同時執行兩個 turn 會造成事件交錯。** turn 尚未完成時再次呼叫 `send()` 會直接失敗。真正的 dsh 會把新訊息放進 inbox，等到 step 邊界再認領；第 07 章會實作這項機制。
- **缺少邊界標記會讓重放無法判斷執行狀態。** 沒有 `turn/start` 和 `step/end`，就無法分辨 turn 是正常結束還是中途失敗。這些標記是正式資料，不只是除錯輸出。

---

## 動手驗證

[`src/`](src/) 延續第 03 章，並加入：

- [`agent_loop.py`](src/agent_loop.py)（新增）：帶著 `send()` 和 `_step()` 的 `Agent`、`AgentRegistry`，還有提供 `agents` service 的 plugin。
- [`test.py`](src/test.py)：確認 turn 事件依序寫入 log；`request/header` 的數字證明每個 step 都會重新推導，跨過 compaction 後仍然正確（1、3、2）；重放 log 並建立新 Agent 後可以接續執行；step 中途失敗不會污染下次推導；turn 執行期間再次呼叫 `send()` 會遭拒。
- [`demo.py`](src/demo.py)（新增）：第一個實機示範。同一個 loop，把真正的 Anthropic API 接到 Model seam 上，跑幾個寫好的 turn，中間插一次 compaction，最後把 log 中的完整執行紀錄印出來。SDK 和 mini-Message 之間的轉換只位於這裡。

```bash
python sections/04-agent-loop/src/test.py   # offline check, no key
```

實機示範需要根目錄的 `requirements.txt` 和一把 key；沒有設定 key 時會自動跳過：

```bash
pip install -r requirements.txt             # anthropic + python-dotenv
cp .env.example .env                        # then set ANTHROPIC_API_KEY
python sections/04-agent-loop/src/demo.py
```

---

## 參考資料

- [`docs/subsystems/core.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/subsystems/core.md)：dsh 自己寫的文件，講 agent 和 agent-loop 這兩個套件。
- [`docs/agent-lifecycle.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/agent-lifecycle.md)：turn 和 step 的生命週期，從 kick 一路到 turn 結束。
