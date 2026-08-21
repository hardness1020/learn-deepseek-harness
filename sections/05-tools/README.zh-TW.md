<!-- source: README.md @ 4a394ca -->

# 05 · Tools

[English](README.md) | 繁體中文 | [简体中文](README.zh-CN.md)

> 這條 pipeline 有四種說不的方法，回答卻只有一種：一個 result。連根本沒跑到的呼叫，也會拿到一個。

Section 04 的 loop 只會講話。每個 step 都以 `"completed"` 結束，因為 model 除了回一句之外沒別的事好做。tool 改變了這件事：model 會叫 mini-dsh 去跑某個東西，而且要先拿到結果，才能繼續往下走。

最直覺的做法，是弄一個裝著函式的 dict。用名字查出來、呼叫它、把它回傳的東西 append 進去。名字查不到就拋出例外。參數不對就拋出例外。政策說不行，那就在呼叫之前先拋出例外。

但這些例外每一個都落在一個 turn 的中間。帶著那次呼叫的 assistant 訊息早就進了 log；例外會一路把 `send()` 拆回去，留下一個沒有答案的問題。下一次推導出來的東西，會讓 model 看到一段講到一半就斷掉的對話紀錄，而重放只會把同一個壞掉的故事再重建一次。

所以：為什麼被拒絕或出錯的呼叫，還是會產生一則正常的 `tool/result`？

因為對話紀錄就是那份契約。model 一定要看到一段在同一個 turn 裡前後對得上的歷史，重放也一定要能把它重建回來，所以不管一個呼叫最後怎麼樣，它都會拿到一行回答。要撐住這件事，tool 這一層必須：

1. 把 tool 放在一個**有作用域的 registry** 裡：一層 global，加上每個 agent 作用域各一層；作用域那一層會蓋掉 global 的同名 tool，而套用到這個作用域的每一條限制，都會跟目前看得到的那組 tool 取交集。
2. 每一個呼叫都走同一條固定的 pipeline：**pre -> ask -> guard -> execute -> post**，永遠照這個順序。
3. 每一關都可以說不，但不准有任何例外穿過邊界跑出去：被拒絕、跑爆、參數不對、名字查不到，出來的形狀都一樣，就是 `{call_id, name, is_error, content}`。
4. ask 這道門預設是關的：pre 的投票（`allow` / `ask` / `deny`）只會愈收愈緊，而一個沒有人可以批准的 `ask`，就等於 deny。
5. 把結果穿回 loop 裡：送去跑之前先 append 一行只進 log 的 `tool/call`，跑完之後再把 `tool/result` append 進 surface；而回覆裡帶了呼叫的那個 step，會以 `None` 這個理由結束，意思是再繞一圈。
6. 每一次註冊都交回一個撤銷用的 undo，因為一切都是 plugin，而且每一次註冊都可以反向撤銷。

---

## Mechanism

一個新檔案 `tools.py`，另外把 tool 這條線穿過原本就有的那些檔案：

- **`ToolDefinition`**：model 看得到的部分（名字、說明、參數），加上真正做事的實作，`execute(args) -> content`。
- **`ToolRegistry`**：`tools` service。以作用域為 key 的層、限制條目、hook 清單，還有 `execute()` 裡那條 pipeline。`register` / `restrict` / `pre` / `guard` / `post` 每一個都會回傳自己的 undo。
- **`ToolScope`**：一個 agent 看到的 registry，也就是它自己那一層疊在 global 那一層上面。Agent 拿的是這個，永遠不是 registry 本身。
- **loop 新長出來的那條分支**：`_step()` 現在會把 schema 跟著請求一起送出去，把回覆裡的那些呼叫跑一遍，然後在 turn 需要再繞一圈的時候交出 `None`。

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

loop 把這個漏斗接進 Section 04 的 step 裡。這就是 agent loop 當初空在那裡、等人來接的 `None` 那條分支：

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

下面是一個 model 呼叫了 tool 的 turn，log 是這樣記的：

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

結果進了 surface，所以第二次推導出來的是 `user`、`assistant`（帶著它發出的呼叫）、`tool`：model 把自己發的呼叫和拿回來的答案，都當成普通的歷史在讀。Section 02 早就默默替這件事鋪好路了：從 surface 存在的那一天起，`tool/result` 就在 `SURFACE_TYPES` 裡面。

現在把同一個 turn 再跑一次，換成一個會拒絕的 guard、一段會跑爆的實作，或是一個根本不存在的名字。log 記下來的故事形狀一模一樣，只有 `is_error` 和 `content` 不同。turn 活下來了，model 讀得到哪裡出錯，而 Offline check 會把四種失敗全塞進同一個 step，用來證明沒有任何例外逃得出 `send()`。

作用域是這個 Mechanism 的另一半。`request/header` 現在會記下每次請求提供了哪些 tool，所以光看 log 就知道一個作用域看到了什麼：作用域那一層蓋掉了 global 的某個名字，而某條限制把 agent b 縮到只剩 `["where"]`，agent a 卻還是什麼都看得到。被限制掉的 tool 不是「被拒絕」，它對那個作用域來說根本不存在；硬要呼叫它，拿回來的是 `unknown tool`，跟其他任何一個一樣，是個正常的結果。

### 改了什麼

跟 Section 04 比起來：

- `kernel.py` 原封不動搬過來。`tools.py` 是唯一新增的原始檔；其他改動都是把 tool 這條線穿過原本就有的檔案，所以跟 04 的 diff 就是這個 section 的 Mechanism，沒有別的。
- `message.py`：`Message` 多了 `tool_calls`（assistant 用）和 `call_id`（tool 用），兩個都有預設值，所以 Section 04 的每一個 Message 讀起來都跟以前一樣。
- `standin.py`：Model seam 多了一個 `tools` 參數，Scripted stand-in 直接忽略它；而事先寫好的回應可以是一個帶 `tool_calls` 的 dict，這樣會用到 tool 的 turn 也能離線寫成腳本。
- `session_log.py`：`derive_messages()` 會把凍起來的 payload 裡的 `tool_calls` 和 `call_id` 解凍，放回 Message 上。`SURFACE_TYPES` 完全沒動。
- `agent_loop.py`：Agent 現在除了 session 和 Model seam，還會收下自己的 `ToolScope`；step 會把 schema 跟著請求一起送出去、記進 `request/header`、把呼叫丟進 pipeline 跑，並且把 Section 04 空在那裡的 `reason None` 那條分支補上。
- `demo.py`：Live demo 現在會真的用到 tool，中間還有一次 guard 拒絕，model 得自己讀懂再解釋給你聽。

---

## In real dsh

所有指過去的連結都固定在 Studied version [`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca) 上。tool 這一層住在 [`packages/core/tools`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/tools)，作用域的部分在 [`packages/core/scope`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/scope)。

| Mini-dsh | 真正的 dsh | 說明 |
| --- | --- | --- |
| `ToolRegistry` + `ToolScope` | [`packages/core/tools/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/tools/src/index.ts)：`ToolRuntime`；[`packages/core/scope/src/store.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/scope/src/store.ts)：`ScopedLayers` | `ctx.tools` 是一個底下墊著 `ScopedLayers` 的 registry：一層 global，加上每個 agent 一層作用域，同名會被蓋掉，限制會取交集，全都透過 `register` / `restrict` 做。 |
| `ToolDefinition` | [`packages/core/tools/src/schema.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/tools/src/schema.ts)：`defineTool()` | `ToolDefinition extends ToolSchema`（schema 這個型別住在 [`packages/llm/llm/src/types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/llm/llm/src/types.ts)），再多加上有型別的參數、一組輸出 `{schema, render}`、`timeoutMs`、`isConcurrencySafe`、`finalizeContent`。 |
| `pre()` 的投票 | [`packages/core/tools/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/tools/src/index.ts)：`tools/pre-execute` | 一個 waterfall 事件，產出 `PreToolDecision = allow \| deny \| ask`；`ask` 要的那個批准由 policy plugin 回答，再往上到 Ceiling 之上，就是 UI 在回答。 |
| `guard()` | [`packages/core/tools/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/tools/src/index.ts)：`ToolGuard` | `(execution) => string \| undefined`，只能拒絕，而且是同步的，在批准之後才在 pipeline 裡跑。這跟 `packages/guard/*` 那些 plugin 不一樣，那些只是普通的事件監聽器。 |
| `post()` 的複審 | [`packages/core/tools/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/tools/src/index.ts)：`tools/post-execute` | 一個 waterfall，產出 `PostToolDecision = accept \| block`，另外還負責往上補東西（重複呼叫同一個 tool 的提醒就是搭這班車）。 |
| 那個 result dict | [`packages/core/tools/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/tools/src/index.ts)：`ToolExecutionSuccess` / `ToolExecutionFailure` | 一樣是這個二分法，`isError: false \| true`，在變成 `tools/result` 事件之前會先被凍起來。 |
| loop 裡那個一個一個跑的 for 迴圈 | [`packages/core/agent-loop/src/tool-calls.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-loop/src/tool-calls.ts)：`executeToolCalls` | 真正的 loop 從來不會直接呼叫 `ctx.tools.execute()`；推動這些呼叫的是一個四階段的 scheduler。那個 scheduler 就是 Section 06 的 Mechanism。 |

真正的 tool 這一層，在這個 section 的 Mechanism 之上，還多做了這些：

- **執行那一段外面還包了一層 waterfall。** `tools/execute` 把實作包起來，讓 plugin 可以幫它設時間上限：timeout policy（[`packages/guard/timeout-policy`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/guard/timeout-policy)）自己定義了 `TOOL_TIMEOUT`，而且是用合作的方式包住，不會把 tool 的 promise 丟在那裡不管。mini 是直接把實作跑下去。
- **從頭到尾都有型別的 schema。** `defineTool()` 會拿真的 schema 去驗參數，輸出也一起驗；`finalizeContent` 則決定 model 讀到的東西長什麼樣。mini 只驗參數名字對不對得上。
- **可以平行送出去跑。** `executeToolCalls` 跑的是一個 `prepare / dispatch / finalize / finish` 的 scheduler：可以平行跑的呼叫會疊在一起跑，互斥的呼叫會卡成一道關卡，而還沒開始就被中止的呼叫會拿到一個合成出來的結果（`TOOL_ABORTED_BEFORE_DISPATCH`），這樣重放才還算數。這一整套都是 Section 06 的事。
- **result 能做的事更多。** 一個 result 可以帶 `concludesTurn`，讓 turn 提早結束；`tools/result` 事件還會記下 `sourceEventSeqs`；而且只要看得到的那組 tool 有變動，runtime 就會發出 `tools/change`。
- **`ask` 真的有人回答。** 人看到的那個批准提示是 UI，落在 Ceiling 之上；mini 把這個 seam 收成一個 `asker` callable，Offline check 直接在程式碼裡回答它。

---

## Failure modes

- **用例外來拒絕，會把對話紀錄撕開。** pipeline 說不的時候，帶著那次呼叫的 assistant 訊息早就在 log 裡了。不回答而是拋例外，推導出來的歷史就會停在一個 model 永遠等不到回音的問題上；重放只會把同一個洞再挖一次。不管判決是什麼，那一行 result 就是回答。
- **默默跳過，model 什麼都學不到。** 把被拒絕的呼叫直接丟掉，model 要嘛永遠等下去，要嘛永遠重發。`is_error` 加上一個理由才是資訊：檢查裡的 model 在同一個 step 裡讀到四種不同的失敗，還是把 turn 走完了。
- **沒有人批准的 ask，只能拒絕。** 如果預設放行，那一個什麼都還沒設定的 mini-dsh 反而是最寬鬆的。這道門預設是關的，而檢查會證明：只要有人來回答，同一個呼叫就跑得起來。
- **guard 如果能放行，它們就會互相打架。** guard 只能拒絕，所以方向是單一的：任何一個 guard 都只會讓能跑的事情變少，順序因此永遠不重要。一個能放行的 guard，會依照註冊的先後去蓋掉另一個的拒絕。
- **不能信任的是實作那一段。** 一個會拋例外的 tool 是很平常的事，接住它，包成 result 送回去。不能拋例外的是 pipeline 自己，所以參數不對和名字查不到也一樣要變成 result，而不是拿 assert 去擋。
- **撤不掉的註冊會活得比它的 plugin 還久。** `register` / `restrict` / `guard` 每一個都會交回自己的 undo，讓 fiber 去收。檢查會在對話進行到一半時卸載一個 tool plugin：下一行 `request/header` 什麼都沒提供，而去呼叫那個已經消失的 tool，也不過就是另一個正常的結果。
- **不取交集，作用域就只會愈長愈大。** 蓋掉只能新增或替換，真正讓範圍變小的是限制。把所有適用的限制都取交集，代表任何一層都能把一個作用域圈起來；Section 12 讓 subagent 只拿到父層 tool 的一部分，靠的就是這件事。

---

## 跑跑看

[`src/`](src/) 把 04 原封不動搬過來，再加上：

- [`tools.py`](src/tools.py)（新增）：`ToolDefinition`、帶著 pre/ask/guard/execute/post pipeline 的 `ToolRegistry`、`ToolScope`，還有提供 `tools` service 的 plugin。
- [`agent_loop.py`](src/agent_loop.py)：step 會把 tool 的 schema 跟著請求一起送出去，append `tool/call` 和 `tool/result` 兩行，並在 turn 需要再繞一圈時以 `None` 這個理由結束。
- [`message.py`](src/message.py)、[`standin.py`](src/standin.py)、[`session_log.py`](src/session_log.py)：tool 這條線，細節就是「改了什麼」列的那幾條。
- [`test.py`](src/test.py)：一個用到 tool 的 turn 會再繞一圈，整個故事照順序落在 log 上；四種失敗形狀都變成四個正常的結果；ask 這道門預設是關的，而且會蓋過比較鬆的投票；post 的複審會改寫一個結果；作用域的蓋掉和限制，都看得到寫在 `request/header` 上；卸載一個 tool plugin，會在對話進行到一半時把它的註冊反向撤銷。
- [`demo.py`](src/demo.py)：Live demo 會真的用到 tool。model 走 pipeline 去讀一則筆記，接著撞上一次 guard 拒絕，再把 tool 告訴它的話講出來，最後把 log 自己的故事印出來。

```bash
python sections/05-tools/src/test.py        # offline check, no key
```

Live demo 需要根目錄的 `requirements.txt` 和一把 key；沒有 key 的話，它會安靜地跳過：

```bash
pip install -r requirements.txt             # anthropic + python-dotenv
cp .env.example .env                        # then set ANTHROPIC_API_KEY
python sections/05-tools/src/demo.py
```

---

## 出處

- [`docs/subsystems/tools.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/subsystems/tools.md)：dsh 自己寫的文件，講 tool runtime。
- [`docs/tool-execution-pipeline.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/tool-execution-pipeline.md)：那條固定的 pipeline，一關一關講。
- [`docs/subsystems/scope.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/subsystems/scope.md)：有作用域的層、蓋掉，還有限制。
