<!-- source: README.md @ 3705bd7 -->

# 05 · Tools

[English](README.md) | 繁體中文 | [简体中文](README.zh-CN.md)

> 工具呼叫會在執行前就寫入 log。如果工具失敗後沒有寫回結果，對話中就會留下一個沒有回答的呼叫。因此，每次呼叫都必須產生對應的 `tool/result`，成功與失敗都一樣。

第 04 章的 loop 只能處理文字回覆，所以每個 step 都會以 `"completed"` 結束。加入工具後，模型可以要求 Mini-dsh 執行操作，取得結果後再繼續下一個 step。

最簡單的實作，是用一個 dict 儲存函式，依名稱查找、執行，再將回傳值寫入 log。名稱不存在、參數錯誤或政策拒絕時，就直接拋出例外。

但這些例外都發生在 turn 中間。包含工具呼叫的 assistant 訊息已經寫入 log，例外卻會直接中斷 `send()`，留下沒有結果的呼叫。之後無論推導歷史或重放 log，都只能得到一段不完整的對話。

因此，本章要回答的問題是：為什麼被拒絕或執行失敗的呼叫，仍然必須產生正常的 `tool/result`？

對話紀錄必須保持前後完整，重放時也必須能重建相同結果。所以無論呼叫成功、被拒絕還是執行失敗，都要有一筆對應結果。工具層需要滿足：

1. 工具儲存在**具有作用域的 registry** 中：除了 global 層，每個 agent 也有自己的層。agent 層可以覆寫同名 global 工具，而所有適用限制會與當前可見工具取交集。
2. 每次呼叫都固定經過 **pre -> ask -> guard -> execute -> post** 處理流程。
3. 任何階段都可以拒絕呼叫，但不允許例外穿過邊界。未知工具、參數錯誤、政策拒絕與執行例外，最後都會轉成 `{call_id, name, is_error, content}`。
4. ask 預設關閉。pre 階段的 `allow` / `ask` / `deny` 投票只能變得更嚴格；如果沒有核准者，`ask` 就視為 `deny`。
5. 工具呼叫前先將 `tool/call` 寫入 log，執行後再將 `tool/result` 加入 surface。包含工具呼叫的 step 回傳 `None`，讓 loop 繼續下一個 step。
6. 每次註冊都回傳對應的 undo，讓 plugin 卸載時可以完整撤銷。

---

## 核心機制

本章新增 `tools.py`，並調整既有元件，讓工具呼叫能完整走過 agent loop：

- **`ToolDefinition`**：model 看得到的部分（名字、說明、參數），加上真正做事的實作，`execute(args) -> content`。
- **`ToolRegistry`**：`tools` service。以作用域為 key 的層、限制條目、hook 清單，還有 `execute()` 裡那條 pipeline。`register` / `restrict` / `pre` / `guard` / `post` 每一個都會回傳自己的 undo。
- **`ToolScope`**：一個 agent 看到的 registry，也就是它自己那一層疊在 global 那一層上面。Agent 拿的是這個，永遠不是 registry 本身。
- **loop 的工具分支**：`_step()` 會將 schema 隨請求送出、執行回覆中的工具呼叫，並在 turn 需要繼續下一個 step 時回傳 `None`。

這條 pipeline 就是一個漏斗。每一關都可以把呼叫擋下來，但所有出口都走同一扇門：

```text
call {"id", "name", "args"}
  │ resolve   unknown name ────────────────────────┐
  │ pre       votes tighten: allow < ask < deny ───┤
  │ ask       no approver, no approval ────────────┤
  │ guard     deny-only reasons ───────────────────┤
  │ execute   bad args, or the body raises ────────┤
  ▼                                                ▼
{"is_error": false, "content"}      {"is_error": true, "content"}
        └───────────────┬──────────────────────────┘
                      post (review, may replace)
                        ▼
             one shape, appended as tool/result
```

寫成程式碼，這個漏斗就是一連串提早 return：

```python
def _run(self, call, scope):
    name = call.get("name")
    tool = self._visible(scope).get(name)
    if tool is None:
        return self._result(call, True, f"unknown tool '{name}'")
    decision = "allow"
    for hook in list(self._pre):
        vote = hook(call)
        if vote is not None and _RANK[vote] > _RANK[decision]:
            decision = vote  # votes only tighten, never loosen
    if decision == "deny":
        return self._result(call, True, "denied before execution")
    if decision == "ask":
        approved = self.asker is not None and self.asker(call)
        if not approved:
            return self._result(call, True, "approval was asked and not given")
    for check in list(self._guards):
        reason = check(call)
        if reason:
            return self._result(call, True, f"denied: {reason}")
    ...
    try:
        return self._result(call, False, str(tool.execute(args)))
    except Exception as exc:  # the body may fail; the pipeline may not
        return self._result(call, True, f"{type(exc).__name__}: {exc}")
```

loop 會把這條 pipeline 接進第 04 章的 step。當工具執行完成、模型還需要根據結果繼續回答時，step 便回傳 `None`：

```python
if not final.tool_calls:
    self.session.append("step/end", {"reason": "completed"})
    return "completed"
for call in final.tool_calls:
    self.session.append("tool/call", call)  # log-only: before dispatch
    result = self.tools.execute(call)
    self.session.append("tool/result", result)  # joins the surface
self.session.append("step/end", {"reason": None})
return None  # tool calls ran: go around again
```

以下是一個模型呼叫工具的 turn，以及對應的 log：

```text
send("what is the wifi password?")
  │   0  user/message
  │   1  turn/start
  ├─ step ────────────────────────────────────────────────
  │   2  step/start
  │   3  request/header    {"messages": 1, "tools": ["lookup"]}
  │   4  assistant/chunk   x 3
  │   7  assistant/message {"content": "Checking.", "tool_calls": [c1]}
  │   8  tool/call         c1: lookup {"key": "wifi"}     ◄ log-only
  │   9  tool/result       {"call_id": "c1", "is_error": false,
  │                         "content": "hunter2"}          ◄ surface
  │  10  step/end          {"reason": null}
  ├─ reason is None ► go around
  │  11  step/start
  │  12  request/header    {"messages": 3, "tools": ["lookup"]}
  │      ...
  │  17  step/end          {"reason": "completed"}
  │  18  turn/end
```

結果會加入 surface，因此第二次推導出的歷史依序包含 `user`、帶有呼叫資訊的 `assistant`，以及 `tool`。模型可以像閱讀一般對話一樣，看到自己提出的呼叫與工具回傳結果。第 02 章已將 `tool/result` 納入 `SURFACE_TYPES`，所以不需再修改 surface 規則。

若改成會拒絕的 guard、會拋出例外的實作，或不存在的工具名稱，log 的事件結構仍然相同，只有 `is_error` 與 `content` 不同。turn 不會因此中斷，模型也能讀到錯誤原因。離線測試會在同一個 step 中涵蓋四種失敗，確認例外不會穿過 `send()` 邊界。

作用域是這項機制的另一半。`request/header` 會記下每次請求提供了哪些工具，因此只看 log 就能確認各作用域的可見範圍。例如，agent 層可以覆寫 global 的同名工具，限制也能讓 agent b 只看到 `["where"]`，同時不影響 agent a。被限制的工具在該作用域中等同不存在；若仍嘗試呼叫，系統會回傳一般的 `unknown tool` 錯誤結果。

### 改了什麼

與第 04 章相比：

- `kernel.py` 完整沿用。`tools.py` 是唯一新增的原始檔；其他改動都是把 tool 這條線穿過原本就有的檔案，因此與第 04 章相比，diff 只包含本章新增的機制，不包含其他改動。
- `message.py`：`Message` 多了 `tool_calls`（assistant 用）和 `call_id`（tool 用），兩個都有預設值，所以第 04 章的每一個 Message 讀起來都跟以前一樣。
- `standin.py`：Model seam 多了一個 `tools` 參數，Scripted stand-in 直接忽略它；而事先寫好的回應可以是一個帶 `tool_calls` 的 dict，這樣會用到 tool 的 turn 也能離線寫成腳本。
- `session_log.py`：`derive_messages()` 會把凍起來的 payload 裡的 `tool_calls` 和 `call_id` 解凍，放回 Message 上。`SURFACE_TYPES` 完全沒動。
- `agent_loop.py`：Agent 現在除了 session 和 Model seam，還會收下自己的 `ToolScope`；step 會把 schema 跟著請求一起送出去、記進 `request/header`、把呼叫丟進 pipeline 跑，並且把第 04 章空在那裡的 `reason None` 那條分支補上。
- `demo.py`：實機示範現在會真的用到 tool，中間還有一次 guard 拒絕，model 得自己讀懂再解釋給你聽。

---

## 對照真正的 dsh

以下連結皆指向研究版本 [`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca)。tool 這一層位於 [`packages/core/tools`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/tools)，作用域的部分在 [`packages/core/scope`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/scope)。

| Mini-dsh | 真正的 dsh | 說明 |
| --- | --- | --- |
| `ToolRegistry` + `ToolScope` | [`packages/core/tools/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/tools/src/index.ts)：`ToolRuntime`；[`packages/core/scope/src/store.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/scope/src/store.ts)：`ScopedLayers` | `ctx.tools` 是一個底下墊著 `ScopedLayers` 的 registry：一層 global，加上每個 agent 一層作用域，同名會被蓋掉，限制會取交集，全都透過 `register` / `restrict` 做。 |
| `ToolDefinition` | [`packages/core/tools/src/schema.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/tools/src/schema.ts)：`defineTool()` | `ToolDefinition extends ToolSchema`（schema 這個型別位於 [`packages/llm/llm/src/types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/llm/llm/src/types.ts)），再多加上有型別的參數、一組輸出 `{schema, render}`、`timeoutMs`、`isConcurrencySafe`、`finalizeContent`。 |
| `pre()` 的投票 | [`packages/core/tools/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/tools/src/index.ts)：`tools/pre-execute` | 一個 waterfall 事件，產出 `PreToolDecision = allow \| deny \| ask`；`ask` 所需的核准由 policy plugin 處理，實際 UI 互動不在本教學的實作範圍內。 |
| `guard()` | [`packages/core/tools/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/tools/src/index.ts)：`ToolGuard` | `(execution) => string \| undefined`，只能拒絕，而且是同步的，在批准之後才在 pipeline 裡跑。這跟 `packages/guard/*` 那些 plugin 不一樣，那些只是普通的事件監聽器。 |
| `post()` 的複審 | [`packages/core/tools/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/tools/src/index.ts)：`tools/post-execute` | 一個 waterfall，產出 `PostToolDecision = accept \| block`，也能在結果中補充提醒，例如偵測到重複呼叫同一個工具時加入說明。 |
| 統一的 result dict | [`packages/core/tools/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/tools/src/index.ts)：`ToolExecutionSuccess` / `ToolExecutionFailure` | 同樣使用 `isError: false \| true` 區分成功與失敗，並在轉成 `tools/result` 事件前設為不可變資料。 |
| loop 裡那個一個一個跑的 for 迴圈 | [`packages/core/agent-loop/src/tool-calls.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-loop/src/tool-calls.ts)：`executeToolCalls` | 真正的 loop 從來不會直接呼叫 `ctx.tools.execute()`；推動這些呼叫的是一個四階段的 scheduler。那個 scheduler 就是第 06 章的機制。 |

真正的 tool 這一層還提供以下功能：

- **執行那一段外面還包了一層 waterfall。** `tools/execute` 把實作包起來，讓 plugin 可以幫它設時間上限：timeout policy（[`packages/guard/timeout-policy`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/guard/timeout-policy)）自己定義了 `TOOL_TIMEOUT`，而且是用合作的方式包住，不會把 tool 的 promise 丟在那裡不管。mini 是直接把實作跑下去。
- **從頭到尾都有型別的 schema。** `defineTool()` 會拿真的 schema 去驗參數，輸出也一起驗；`finalizeContent` 則決定 model 讀到的東西長什麼樣。mini 只驗參數名字對不對得上。
- **可以平行送出去跑。** `executeToolCalls` 跑的是一個 `prepare / dispatch / finalize / finish` 的 scheduler：可以平行跑的呼叫會疊在一起跑，互斥的呼叫會卡成一道關卡，而還沒開始就被中止的呼叫會拿到一個合成出來的結果（`TOOL_ABORTED_BEFORE_DISPATCH`），這樣重放才還算數。這一整套都是第 06 章的事。
- **result 能做的事更多。** 一個 result 可以帶 `concludesTurn`，讓 turn 提早結束；`tools/result` 事件還會記下 `sourceEventSeqs`；而且只要看得到的那組 tool 有變動，runtime 就會發出 `tools/change`。
- **`ask` 真的有人回答。** 人看到的那個批准提示是 UI，不在本教學的實作範圍內；mini 把這個 seam 收成一個 `asker` callable，離線測試直接在程式碼裡回答它。

---

## 常見失敗模式

- **用例外表示拒絕，會留下不完整的對話。** 工具呼叫出現時，對應的 assistant 訊息已寫入 log；如果系統只拋例外而不產生結果，模型歷史就會停在一個永遠沒有回覆的呼叫。無論成功或失敗，都必須補上一筆 result。
- **直接跳過呼叫，模型無法知道發生了什麼。** 被拒絕的呼叫若沒有結果，模型可能持續等待或重複提出相同請求。`is_error` 加上明確原因，才能讓模型判斷下一步。
- **沒有人處理的 ask 必須預設拒絕。** 若預設放行，未設定任何政策的 Mini-dsh 反而最寬鬆。測試會確認 ask 在沒有核准者時遭拒，提供 `asker` 後才會執行。
- **guard 如果能放行，它們就會互相打架。** guard 只能拒絕，所以方向是單一的：任何一個 guard 都只會讓能跑的事情變少，順序因此永遠不重要。一個能放行的 guard，會依照註冊的先後去蓋掉另一個的拒絕。
- **工具實作不能被視為可靠邊界。** 工具拋出例外是正常的失敗情況，pipeline 必須捕捉並轉成 result。參數錯誤和名稱不存在也要用相同方式處理，而不是透過 assert 中斷流程。
- **撤不掉的註冊會活得比它的 plugin 還久。** `register` / `restrict` / `guard` 每一個都會交回自己的 undo，讓 fiber 去收。檢查會在對話進行到一半時卸載一個 tool plugin：下一行 `request/header` 什麼都沒提供，而去呼叫那個已經消失的 tool，也不過就是另一個正常的結果。
- **不取交集，作用域就只會愈長愈大。** 蓋掉只能新增或替換，真正讓範圍變小的是限制。把所有適用的限制都取交集，代表任何一層都能把一個作用域圈起來；第 12 章讓 subagent 只拿到父層 tool 的一部分，靠的就是這件事。

---

## 動手驗證

[`src/`](src/) 延續第 04 章，並加入：

- [`tools.py`](src/tools.py)（新增）：`ToolDefinition`、帶著 pre/ask/guard/execute/post pipeline 的 `ToolRegistry`、`ToolScope`，還有提供 `tools` service 的 plugin。
- [`agent_loop.py`](src/agent_loop.py)：step 會把 tool 的 schema 跟著請求一起送出去，append `tool/call` 和 `tool/result` 兩行，並在 turn 需要再繞一圈時以 `None` 這個理由結束。
- [`message.py`](src/message.py)、[`standin.py`](src/standin.py)、[`session_log.py`](src/session_log.py)：tool 這條線，細節就是「改了什麼」列的那幾條。
- [`test.py`](src/test.py)：一個用到 tool 的 turn 會再繞一圈，完整流程照順序落在 log 上；四種失敗形狀都變成四個正常的結果；ask 這道門預設是關的，而且會蓋過比較鬆的投票；post 的複審會改寫一個結果；作用域的蓋掉和限制，都看得到寫在 `request/header` 上；卸載一個 tool plugin，會在對話進行到一半時把它的註冊反向撤銷。
- [`demo.py`](src/demo.py)：實機示範會真的用到 tool。model 走 pipeline 去讀一則筆記，接著撞上一次 guard 拒絕，再把 tool 告訴它的話講出來，最後把 log 中的完整執行紀錄印出來。

```bash
python sections/05-tools/src/test.py        # offline check, no key
```

實機示範需要根目錄的 `requirements.txt` 和一把 key；沒有設定 key 時會自動跳過：

```bash
pip install -r requirements.txt             # anthropic + python-dotenv
cp .env.example .env                        # then set ANTHROPIC_API_KEY
python sections/05-tools/src/demo.py
```

---

## 參考資料

- [`docs/subsystems/tools.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/subsystems/tools.md)：dsh 自己寫的文件，講 tool runtime。
- [`docs/tool-execution-pipeline.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/tool-execution-pipeline.md)：那條固定的 pipeline，一關一關講。
- [`docs/subsystems/scope.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/subsystems/scope.md)：有作用域的層、蓋掉，還有限制。
