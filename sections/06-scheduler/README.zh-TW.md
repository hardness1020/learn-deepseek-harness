<!-- source: README.md @ 8d86583 -->

# 06 · Scheduler

[English](README.md) | 繁體中文 | [简体中文](README.zh-CN.md)

> 安全的呼叫疊在一起跑，互斥的呼叫自己站一邊。現實世界愛照什麼順序結束，就照什麼順序，log 永遠不會。

Section 05 是用一個 for 迴圈把回覆裡的呼叫跑完的：一個呼叫，一個答案，換下一個。以前每個回覆只帶一個呼叫，所以看不出差別。但真正的回覆會一次要一批：你叫 model 去讀三則筆記，它會一次全部要，而那個一個一個跑的迴圈，會把三次各一秒的讀取變成三秒的等待。

最直覺的做法，是把每個呼叫全部丟給一個執行緒池，誰先回來就先 append 誰的結果。但這樣一來，log 的順序就要看執行緒的快慢了：同一個 turn 跑兩次會得到兩份不一樣的對話紀錄，重放也就不再是重建，而是在賭誰先誰後。一個寫入如果跟餵資料給它的那個讀取疊在一起跑，它會讀到一半新一半舊的東西。至於一個 turn 在半路被取消的時候，那些根本沒開始的呼叫，正是 assistant 訊息已經問出口的問題：Section 05 那份被撕開的對話紀錄，換一條路又走回來了。

所以：為什麼可以平行跑的呼叫會疊在一起跑，互斥的呼叫會卡成一道關卡，而還沒開始就被中止的呼叫會拿到一個合成出來的結果？

因為速度是要付代價的，但這個代價不能是 Section 05 那份契約，也就是對話紀錄。要做到這件事，scheduler 必須：

1. 先寫 log，再開跑：任何東西送出去跑之前，每個呼叫的 `tool/call` 那一行都已經 append 好了；而每個 `tool/result` 落下的順序都是 model 給的順序，不管執行緒是照什麼順序結束的。
2. 安不安全這件事，由 tool 自己宣告：要用 `is_concurrency_safe` 主動表態，預設一律互斥，因為只有寫這個 tool 的人才知道它的實作碰了什麼。
3. 只在同一批裡面才疊著跑：連在一起的安全呼叫會一起送出去；互斥的呼叫自己就是一批，也就是一道關卡：排在它前面的要先跑完，排在它後面的要等。
4. 已經開跑的事情絕不半途丟下：取消是在兩批之間才生效，已經送出去的實作會讓它跑到自己結束。
5. 連跳過的呼叫也要回答：還沒開始就被中止的呼叫會拿到一個合成出來的錯誤結果，因為重放出來的對話紀錄裡，每一個問題都得有它的答案。
6. 只留一個寫入者：只有在跑 loop 的那條執行緒能 append 進 session log；工作執行緒負責跑 pipeline，然後把結果交回來。

---

## Mechanism

一個新檔案 `scheduler.py`，另外把 loop 處理 tool 的那條分支改道，繞過它走：

- **`execute_tool_calls(session, tools, calls, aborted)`**：整件事就是它在推。四個階段：prepare、dispatch、finalize、finish。
- **`_batches(plan)`**：分批的規則。連在一起的安全呼叫共用一批；互斥的呼叫自己站一邊。
- `ToolDefinition` 上的 **`is_concurrency_safe`**，透過 registry 和 scope 上的 `is_safe()` 查出來，所以同名蓋掉這件事，套在安全與否上，跟套在其他東西上一模一樣。
- **`Agent.cancel()`**：每個 turn 一個 `threading.Event`。scheduler 在每一批開始之前都會看它一眼；被砍掉的 step 會以 `"aborted"` 這個理由結束，turn 也跟著收掉。

每一個呼叫都走同樣的四個階段：

1. **prepare**：照 model 給的順序，每個呼叫先拿到自己那一行 `tool/call`（只進 log，而且在任何東西開跑之前），再從 `is_safe()` 拿到一個安全判定。名字查不到的一律算互斥。
2. **dispatch**：一批一批送給工作執行緒。每一批開始前只問一句：這個 turn 有沒有被中止？有的話就不再送了。
3. **finalize**：關卡就在這裡。跑 loop 的那條執行緒會等這一批的每一個 future；已經開跑的事情絕不半途丟下，就算取消了也一樣。
4. **finish**：每個呼叫一行 `tool/result`，照 model 給的順序寫。從來沒被送出去的呼叫會拿到一個合成出來的結果：`{"is_error": true, "content": "aborted before dispatch"}`。

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

Section 05 那條 pipeline 完全沒動：工作執行緒照樣呼叫 `tools.execute(call)`，每個出口照樣是一個 result。變的是誰負責 append。scheduler 跑在 loop 那條執行緒上，是 log 唯一的寫入者；工作執行緒只算出 result dict，其他什麼都不做，所以這份只能追加的 log 永遠不需要上鎖。

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

sibling 已經送出去了，所以它一路跑到自己結束。關卡後面那兩個呼叫從來沒開始，finish 還是替它們回答了。把歷史推導出來，每一個問題都有它的答案：重放出來的還是同一個故事，連取消都一起還原。

### 改了什麼

跟 Section 05 比起來：

- `kernel.py`、`message.py`、`session_log.py`、`standin.py` 都原封不動搬過來。`scheduler.py` 是唯一新增的原始檔；其他改動都是把 scheduler 這條線穿過原本就有的檔案，所以跟 05 的 diff 就是這個 section 的 Mechanism，沒有別的。
- `tools.py`：`ToolDefinition` 多了 `is_concurrency_safe`（預設 `False`），registry 和 scope 多了 `is_safe()`。pipeline 本身完全沒動。
- `agent_loop.py`：原本一個一個跑完回覆裡呼叫的那個 for 迴圈，變成呼叫一次 `execute_tool_calls`。Agent 多了 `cancel()` 和每個 turn 一個的中止事件，而一個 step 現在可以用 `"aborted"` 這個理由結束。
- 一個回覆帶多個呼叫的時候，log 的形狀變了：現在所有 `tool/call` 都會落在第一個 `tool/result` 之前（送出去跑之前就寫好），而不是像以前那樣一個呼叫配一個結果交錯著寫。
- `demo.py`：Live demo 會註冊一個可以平行跑的讀取和一個互斥的寫入，兩個都故意跑得很慢，再把每個實作實際開始和結束的時間印出來，讓你在時鐘上就看得到它們疊在一起。

---

## In real dsh

所有指過去的連結都固定在 Studied version [`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca) 上。scheduler 住在 loop 那個套件裡，不在 tool runtime 裡：[`packages/core/agent-loop`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-loop)。

| Mini-dsh | 真正的 dsh | 說明 |
| --- | --- | --- |
| `scheduler.py` 裡的 `execute_tool_calls` | [`packages/core/agent-loop/src/tool-calls.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-loop/src/tool-calls.ts)：`executeToolCalls` | loop 不會直接拿回覆裡的呼叫去跑 `ctx.tools.execute()`；推動它們的是 `executeToolCalls`，跑的一樣是 `prepare / dispatch / finalize / finish` 這個四階段的 scheduler。 |
| `is_concurrency_safe` | [`packages/core/tools/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/tools/src/index.ts)：`ToolDefinition` | `ToolDefinition.isConcurrencySafe`，每個 tool 自己宣告；tool 沒說話就是互斥。 |
| 那個合成出來的結果 | [`packages/core/tools/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/tools/src/index.ts)：`TOOL_ABORTED_BEFORE_DISPATCH` | 這是跟 `TOOL_ABORTED` 不一樣的錯誤碼，這樣光看對話紀錄就分得出來，一個呼叫是被跳過的，還是跑到一半被打斷的。 |
| `Agent.cancel()` + `threading.Event` | [`packages/core/agent/src/runtime-types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent/src/runtime-types.ts)：`Agent.cancel` | 真正的取消，是把一串 abort signal 融在一起，穿過整個 runtime；mini 只留每個 turn 一個事件，在每一批的邊界上檢查。 |
| finish 照 model 給的順序 append | [`packages/core/agent-loop/src/tool-calls.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-loop/src/tool-calls.ts) | 結果是在 loop 裡變成 session 事件的，不是在 registry 裡；`tool/result` 事件還會帶 `sourceEventSeqs`，把每個答案接回它對應的那幾行，而 mini 靠的是 `call_id`。 |
| 那個 `ThreadPoolExecutor` | [`packages/core/tools/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/tools/src/index.ts)：`TOOL_RUNTIME_SCHEDULER` | runtime 是透過一個具名的 seam 去拿它的 scheduler，而不是寫死一個 pool。 |

真正的 scheduler 在這個 section 的 Mechanism 之上，還多做了這些：

- **用合作的方式中止已經開跑的呼叫。** `TOOL_ABORTED` 是給送出去之後才被打斷的呼叫用的：融在一起的 signal 會傳進實作裡面，而 timeout policy（[`packages/guard/timeout-policy`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/guard/timeout-policy)）會幫 `tools/execute` 加上一個期限，同時不會把 tool 的 promise 丟在那裡不管。mini 根本不會去打斷已經開跑的實作，所以它只有送出去之前的那一種中止碼。
- **提早結束的方式更多。** 一個 result 可以帶 `concludesTurn`，讓 turn 提早結束。mini 唯一的提早出口是 `cancel()`。
- **從頭到尾都是 async。** dsh 的 tool 實作是 async 的，所以疊在一起跑這件事，是在同一條執行緒裡靠 promise 完成的；mini 的實作是普通的 Python callable，所以它是用一個執行緒池換到同樣的重疊。
- **按下取消的是人。** 在真正的 dsh 裡，取消通常是從 UI 來的，而 UI 落在 Ceiling 之上；mini 就把 `cancel()` 開成一個普通的方法，而 Offline check 是從一個 tool 的實作裡面按下去的。

---

## Failure modes

- **誰跑完誰就 append，會讓 log 的順序變成一場搶快比賽。** 讓工作執行緒各自跑完就 append，同一個 turn 每跑一次就生出一份不一樣的對話紀錄，重放也就不再是重建了。finish 是在同一條執行緒上照 model 給的順序 append 的，所以疊著跑這件事永遠不會出現在故事裡，只會出現在時鐘上。
- **要自己標互斥的話，責任就反過來了。** 如果 tool 預設安全、要自己標成互斥，那每個忘記標的人都是在拿共用狀態賭一把。預設互斥的意思是，忘記標的人最多只是慢一點；檢查裡那個 `solo` 就證明了一個沒標的 tool 真的會自己一個人跑。
- **半途丟下已經開跑的事情，弄壞的比救回來的多。** 一個實作寫到一半被砍掉，會留下半個檔案，和一個沒人敢信的結果。scheduler 只會拒絕開新的一批；已經送出去的，會跑完，然後把結果交回來。取消是在關卡上做的決定，不是半路衝上去把東西砍斷。
- **被跳過的呼叫如果沒有那一行，對話紀錄就破了一個洞。** assistant 訊息上四個呼叫都寫著；把沒開始的那兩個丟掉，推導出來的歷史就會問了問題卻永遠不回答。那個合成出來的結果，就是 Section 05 的規則在取消之後還活著：不管一個呼叫最後怎麼樣，它都會拿到一行回答。
- **工作執行緒如果會寫 log，到處都得上鎖。** session log 從設計上就只有一個寫入者：prepare 和 finish 跑在 loop 那條執行緒上，工作執行緒只負責算。疊著跑這件事被關在一個階段裡面，不會漏到每一個資料結構上。
- **安全判定就算過時了，也還是安全的。** 判定在 prepare 就定死了，所以一個 tool 就算在一批跑到一半時被卸載，位子還是留著；它的實作要嘛已經跑過，要嘛結果會說清楚發生了什麼。跑到一半再重新查一次，等於讓計畫在關卡底下偷偷變動。

---

## 跑跑看

[`src/`](src/) 把 05 原封不動搬過來，再加上：

- [`scheduler.py`](src/scheduler.py)（新增）：`execute_tool_calls`，把四個階段一路推完的那支函式，還有 `_batches`，分批的規則。
- [`tools.py`](src/tools.py)：`ToolDefinition` 上的 `is_concurrency_safe`，registry 和 scope 上的 `is_safe()`。
- [`agent_loop.py`](src/agent_loop.py)：處理 tool 的那條分支改走 scheduler；Agent 多了 `cancel()` 和每個 turn 一個的中止事件；一個 step 現在可以用 `"aborted"` 結束。
- [`test.py`](src/test.py)：兩個安全的呼叫要一起通過一道關卡，而那道關卡只有真的疊著跑才過得了，用這個證明它們真的重疊了；一個沒標的 tool 夾在它們中間，自己一個人跑；就算快的那個先跑完，結果還是照 model 給的順序落下；而一批跑到一半按下取消，已經開跑的會跑完，沒開始的會拿到合成出來的結果，下一個 turn 從乾淨的狀態重新開始。
- [`demo.py`](src/demo.py)：Live demo 會先要兩次可以平行跑的查詢，再要一次互斥的儲存，並且把每個實作實際開始和結束的時間，連同 log 自己的故事一起印出來。

```bash
python sections/06-scheduler/src/test.py    # offline check, no key
```

Live demo 需要根目錄的 `requirements.txt` 和一把 key；沒有 key 的話，它會安靜地跳過：

```bash
pip install -r requirements.txt             # anthropic + python-dotenv
cp .env.example .env                        # then set ANTHROPIC_API_KEY
python sections/06-scheduler/src/demo.py
```

---

## 出處

- [`docs/tool-execution-pipeline.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/tool-execution-pipeline.md)：dsh 自己寫的文件，講 scheduler 推動的那條執行 pipeline。
- [`docs/subsystems/core.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/subsystems/core.md)：`executeToolCalls` 所在的那個 loop 套件。
- [`docs/agent-lifecycle.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/agent-lifecycle.md)：tool 的執行在一個 turn 裡面坐在什麼位置，取消也一起講。
