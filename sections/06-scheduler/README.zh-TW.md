<!-- source: README.md @ 3705bd7 -->

# 06 · Scheduler

[English](README.md) | 繁體中文 | [简体中文](README.zh-CN.md)

> 逐一執行工具呼叫會浪費等待時間，但若依照完成先後寫入 log，同一個 turn 每次執行都可能產生不同的紀錄。因此，工具可以並行執行，但 log 必須保持固定的寫入順序。

第 05 章使用 for loop 依序執行回覆中的工具呼叫。當每次回覆只有一個呼叫時，這個限制不明顯；但模型往往會一次要求多個工具。例如同時讀取三份筆記，若每次讀取需要一秒，串行執行就必須等待三秒。

把所有呼叫丟進執行緒池並不難，但如果哪個先完成就先 append，log 順序就會受排程影響。同一個 turn 重複執行可能產生不同紀錄，重放也失去確定性。此外，具有依賴關係的讀寫操作不能隨意重疊，而 turn 取消時，那些尚未開始的呼叫也仍然需要對應結果，否則對話歷史會再次出現缺口。

因此，本章要回答的問題是：為什麼可並行的呼叫會重疊執行，互斥呼叫會形成 barrier，而在 dispatch 前中止的呼叫也會收到合成結果？

並行化不能犧牲第 05 章建立的對話完整性。scheduler 必須遵守以下原則：

1. 先寫 log，再開始執行：所有 `tool/call` 都要在 dispatch 前 append；`tool/result` 則固定依模型給出的呼叫順序寫入，不受執行緒完成順序影響。
2. 是否可並行由工具自行宣告：工具必須透過 `is_concurrency_safe` 主動標示，未標示時一律視為互斥，因為只有實作者知道它會碰觸哪些共享資源。
3. 只有同一批安全呼叫會並行：連續的安全呼叫會一起 dispatch；互斥呼叫各自形成一批，也就是一道 barrier，前一批完成後才能開始下一批。
4. 已開始的工作不會半途取消：取消只在兩批之間生效，已 dispatch 的實作仍會執行完畢。
5. 被略過的呼叫也必須有結果：在 dispatch 前遭中止的呼叫會取得合成錯誤，確保對話歷史中的每個呼叫都有對應回覆。
6. session log 只有一個寫入者：loop 執行緒負責 append，worker thread 只執行 pipeline 並回傳結果。

---

## 核心機制

本章新增 `scheduler.py`，並讓 loop 的工具分支改由 scheduler 處理：

- **`execute_tool_calls(session, tools, calls, aborted)`**：負責推進 prepare、dispatch、finalize、finish 四個階段。
- **`_batches(plan)`**：實作分批規則。連續的安全呼叫放在同一批，互斥呼叫則各自成批。
- `ToolDefinition` 的 **`is_concurrency_safe`**：registry 與 scope 透過 `is_safe()` 查詢，因此同名工具覆寫後，安全屬性也會一起套用新的定義。
- **`Agent.cancel()`**：每個 turn 都有一個 `threading.Event`。scheduler 在每批開始前檢查它；遭中止的 step 以 `"aborted"` 結束，turn 也隨之結束。

每一個呼叫都走同樣的四個階段：

1. **prepare**：依模型給出的順序先為每個呼叫寫入 `tool/call`，再透過 `is_safe()` 取得安全判定。找不到名稱的工具一律視為互斥。
2. **dispatch**：逐批交給 worker thread。每批開始前先檢查 turn 是否已中止；若已中止，就不再派送後續工作。
3. **finalize**：loop 執行緒等待該批所有 future 完成。即使 turn 此時被取消，已開始的工作仍會執行完畢。
4. **finish**：依模型給出的順序，為每個呼叫寫入一筆 `tool/result`。從未 dispatch 的呼叫會取得合成結果：`{"is_error": true, "content": "aborted before dispatch"}`。

```text
reply: a (safe)   b (safe)   c (exclusive)   d (safe)

prepare   tool/call a, b, c, d   ◄ four rows, model order, nothing running
dispatch  batch [a b]   a ═══════════╗
                        b ═══════╗   ║   safe calls overlap
finalize                ── barrier ──┘
dispatch  batch [c]     c ═══════╗       exclusive: a batch of one
finalize                ── barrier
dispatch  batch [d]     d ═══╗
finalize                ── barrier
finish    tool/result a, b, c, d ◄ model order, though b finished before a
```

寫成程式碼，四個階段讀起來也是同一個順序：

```python
def execute_tool_calls(session, tools, calls, aborted):
    # prepare: a log row and a safety verdict per call, before anything runs
    plan = [(index, call, tools.is_safe(call)) for index, call in enumerate(calls)]
    for _index, call, _safe in plan:
        session.append("tool/call", call)  # log-only: before dispatch
    outcomes = {}  # index -> result dict, filled as batches finalize
    with ThreadPoolExecutor(max_workers=max(1, len(plan))) as pool:
        for batch in _batches(plan):
            # dispatch: a batch starts only if nothing has aborted the turn
            if aborted.is_set():
                break
            futures = [
                (index, pool.submit(tools.execute, call))
                for index, call, _safe in batch
            ]
            # finalize: the barrier; started work is never abandoned
            for index, future in futures:
                outcomes[index] = future.result()
    # finish: one result per call, model order; skipped calls answer too
    for index, call, _safe in plan:
        if index not in outcomes:  # never dispatched: answer anyway
            outcomes[index] = {
                "call_id": call.get("id"),
                "name": call.get("name"),
                "is_error": True,
                "content": ABORTED_BEFORE_DISPATCH,
            }
        session.append("tool/result", outcomes[index])
```

第 05 章那條 pipeline 完全沒動：工作執行緒照樣呼叫 `tools.execute(call)`，每個出口照樣是一個 result。變的是誰負責 append。scheduler 跑在 loop 那條執行緒上，是 log 唯一的寫入者；工作執行緒只算出 result dict，其他什麼都不做，所以這份只能追加的 log 永遠不需要上鎖。

下面是一個被取消的 turn，log 是這樣記的。`stop` 的實作是在一批跑到一半的時候，從自己的工作執行緒裡呼叫 `agent.cancel()`：

```text
send("stop everything")
  │   7  assistant/message {"tool_calls": [stop, sibling, late, last]}
  │   8  tool/call    stop       ◄ all four rows before dispatch
  │   9  tool/call    sibling
  │  10  tool/call    late
  │  11  tool/call    last
  │  12  tool/result  stop     {"is_error": false, "content": "stopping"}
  │  13  tool/result  sibling  {"is_error": false, "content": "kept running"}
  │  14  tool/result  late     {"is_error": true,
  │                             "content": "aborted before dispatch"}
  │  15  tool/result  last     {"is_error": true,
  │                             "content": "aborted before dispatch"}
  │  16  step/end     {"reason": "aborted"}
  │  17  turn/end
```

`sibling` 已經 dispatch，因此仍會執行完畢。barrier 後的兩個呼叫從未開始，但 finish 仍會為它們產生結果。如此一來，推導出的歷史中每個呼叫都有回覆，重放時也能還原取消狀態。

### 改了什麼

與第 05 章相比：

- `kernel.py`、`message.py`、`session_log.py`、`standin.py` 都完整沿用。`scheduler.py` 是唯一新增的原始檔；其他改動都是把 scheduler 這條線穿過原本就有的檔案，因此與第 05 章相比，diff 只包含本章新增的機制，不包含其他改動。
- `tools.py`：`ToolDefinition` 多了 `is_concurrency_safe`（預設 `False`），registry 和 scope 多了 `is_safe()`。pipeline 本身完全沒動。
- `agent_loop.py`：原本一個一個跑完回覆裡呼叫的那個 for 迴圈，變成呼叫一次 `execute_tool_calls`。Agent 多了 `cancel()` 和每個 turn 一個的中止事件，而一個 step 現在可以用 `"aborted"` 這個理由結束。
- 一個回覆帶多個呼叫的時候，log 的形狀變了：現在所有 `tool/call` 都會落在第一個 `tool/result` 之前（送出去跑之前就寫好），而不是像以前那樣一個呼叫配一個結果交錯著寫。
- `demo.py`：實機示範會註冊一個可以平行跑的讀取和一個互斥的寫入，兩個都故意跑得很慢，再把每個實作實際開始和結束的時間印出來，讓你在時鐘上就看得到它們疊在一起。

---

## 對照真正的 dsh

以下連結皆指向研究版本 [`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca)。scheduler 位於 loop 那個套件裡，不在 tool runtime 裡：[`packages/core/agent-loop`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-loop)。

| Mini-dsh | 真正的 dsh | 說明 |
| --- | --- | --- |
| `scheduler.py` 裡的 `execute_tool_calls` | [`packages/core/agent-loop/src/tool-calls.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-loop/src/tool-calls.ts)：`executeToolCalls` | loop 不會直接拿回覆裡的呼叫去跑 `ctx.tools.execute()`；推動它們的是 `executeToolCalls`，跑的一樣是 `prepare / dispatch / finalize / finish` 這個四階段的 scheduler。 |
| `is_concurrency_safe` | [`packages/core/tools/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/tools/src/index.ts)：`ToolDefinition` | `ToolDefinition.isConcurrencySafe`，每個 tool 自己宣告；tool 沒說話就是互斥。 |
| 那個合成出來的結果 | [`packages/core/tools/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/tools/src/index.ts)：`TOOL_ABORTED_BEFORE_DISPATCH` | 這是跟 `TOOL_ABORTED` 不一樣的錯誤碼，這樣光看對話紀錄就分得出來，一個呼叫是被跳過的，還是跑到一半被打斷的。 |
| `Agent.cancel()` + `threading.Event` | [`packages/core/agent/src/runtime-types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent/src/runtime-types.ts)：`Agent.cancel` | 真正的取消，是把一串 abort signal 融在一起，穿過整個 runtime；mini 只留每個 turn 一個事件，在每一批的邊界上檢查。 |
| finish 照 model 給的順序 append | [`packages/core/agent-loop/src/tool-calls.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-loop/src/tool-calls.ts) | 結果是在 loop 裡變成 session 事件的，不是在 registry 裡；`tool/result` 事件還會帶 `sourceEventSeqs`，把每個答案接回它對應的那幾行，而 mini 靠的是 `call_id`。 |
| 那個 `ThreadPoolExecutor` | [`packages/core/tools/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/tools/src/index.ts)：`TOOL_RUNTIME_SCHEDULER` | runtime 是透過一個具名的 seam 去拿它的 scheduler，而不是寫死一個 pool。 |

真正的 scheduler 還提供以下功能：

- **用合作的方式中止已經開跑的呼叫。** `TOOL_ABORTED` 是給送出去之後才被打斷的呼叫用的：融在一起的 signal 會傳進實作裡面，而 timeout policy（[`packages/guard/timeout-policy`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/guard/timeout-policy)）會幫 `tools/execute` 加上一個期限，同時不會把 tool 的 promise 丟在那裡不管。mini 根本不會去打斷已經開跑的實作，所以它只有送出去之前的那一種中止碼。
- **提早結束的方式更多。** 一個 result 可以帶 `concludesTurn`，讓 turn 提早結束。mini 唯一的提早出口是 `cancel()`。
- **從頭到尾都是 async。** dsh 的 tool 實作是 async 的，所以疊在一起跑這件事，是在同一條執行緒裡靠 promise 完成的；mini 的實作是普通的 Python callable，所以它是用一個執行緒池換到同樣的重疊。
- **按下取消的是人。** 在真正的 dsh 裡，取消通常是從 UI 來的，而 UI 不在本教學的實作範圍內；mini 就把 `cancel()` 開成一個普通的方法，而 離線測試是從一個 tool 的實作裡面按下去的。

---

## 常見失敗模式

- **依完成順序 append 會讓 log 不穩定。** 如果 worker thread 完成後自行寫入，同一個 turn 每次執行都可能得到不同的事件順序。finish 改由 loop 執行緒依模型原始順序 append，並行差異只反映在時間上，不會改變紀錄。
- **若要求工具自行標示互斥，預設值就不安全。** 實作者一旦忘記標示，共享狀態便可能同時被修改。預設互斥最多只會犧牲效能；測試中的 `solo` 會確認未標示工具確實單獨執行。
- **強制中斷已開始的工作可能留下不完整副作用。** 寫檔工具若在中途被終止，可能留下半成品。scheduler 只阻止新批次開始；已 dispatch 的工作會完成並回傳結果。
- **被略過的呼叫若沒有結果，對話歷史就不完整。** assistant 訊息已列出所有呼叫，因此即使某些呼叫未開始，也必須產生合成結果。這延續第 05 章「每個呼叫都有回覆」的規則。
- **讓 worker thread 寫 log 會增加同步複雜度。** prepare 與 finish 都在 loop 執行緒執行，worker thread 只負責計算結果，因此 session log 不需要為並行工具額外加鎖。
- **安全判定在 prepare 後固定不變。** 即使工具在批次執行期間卸載，原計畫中的位置仍保留；它不是已執行完畢，就是會得到明確結果。若執行中途重新查詢，反而會讓計畫受到掛載時序影響。

---

## 動手驗證

[`src/`](src/) 延續第 05 章，並加入：

- [`scheduler.py`](src/scheduler.py)（新增）：`execute_tool_calls`，把四個階段一路推完的那支函式，還有 `_batches`，分批的規則。
- [`tools.py`](src/tools.py)：`ToolDefinition` 上的 `is_concurrency_safe`，registry 和 scope 上的 `is_safe()`。
- [`agent_loop.py`](src/agent_loop.py)：處理 tool 的那條分支改走 scheduler；Agent 多了 `cancel()` 和每個 turn 一個的中止事件；一個 step 現在可以用 `"aborted"` 結束。
- [`test.py`](src/test.py)：兩個安全的呼叫要一起通過一道關卡，而那道關卡只有真的疊著跑才過得了，用這個證明它們真的重疊了；一個沒標的 tool 夾在它們中間，自己一個人跑；就算快的那個先跑完，結果還是照 model 給的順序落下；而一批跑到一半按下取消，已經開跑的會跑完，沒開始的會拿到合成出來的結果，下一個 turn 從乾淨的狀態重新開始。
- [`demo.py`](src/demo.py)：實機示範會先要兩次可以平行跑的查詢，再要一次互斥的儲存，並且把每個實作實際開始和結束的時間，連同 log 中的完整執行紀錄一起印出來。

```bash
python sections/06-scheduler/src/test.py    # offline check, no key
```

實機示範需要根目錄的 `requirements.txt` 和一把 key；沒有設定 key 時會自動跳過：

```bash
pip install -r requirements.txt             # anthropic + python-dotenv
cp .env.example .env                        # then set ANTHROPIC_API_KEY
python sections/06-scheduler/src/demo.py
```

---

## 參考資料

- [`docs/tool-execution-pipeline.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/tool-execution-pipeline.md)：dsh 自己寫的文件，講 scheduler 推動的那條執行 pipeline。
- [`docs/subsystems/core.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/subsystems/core.md)：`executeToolCalls` 所在的那個 loop 套件。
- [`docs/agent-lifecycle.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/agent-lifecycle.md)：tool 的執行在一個 turn 裡面坐在什麼位置，取消也一起講。
