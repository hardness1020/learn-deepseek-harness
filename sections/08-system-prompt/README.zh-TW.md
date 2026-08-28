<!-- source: README.md @ 3705bd7 -->

# 08 · System prompt

[English](README.md) | 繁體中文 | [简体中文](README.zh-CN.md)

> harness 中有多個模組會共同組成 system prompt，而這些文字在每個 step 送出時必須完全一致。因此，會隨 step 變動的狀態不能寫入 system prompt。

第 07 章送出的請求已能正確反映歷史，但內容仍然很簡單。`_step()` 直接從 tool registry 取得 schema，system prompt 則是空的。模型不知道自己的角色、回應方式，也不知道當前執行環境。

harness 中的多個部分都可能提供 prompt 內容。Mini-dsh 提供身分說明，persona plugin 定義語氣，工具層提供 schema 清單。這些模組應能獨立註冊內容，而組裝後的順序必須穩定。

但時間、工作目錄等狀態會持續變化，模型需要的是當下快照。如果將這些內容寫進 system prompt，每個 step 都會產生不同的 prompt 前綴，使模型端的 prompt cache 無法命中。

如果只在發送請求時臨時附加動態狀態，這些內容又不會進入 log，之後便無法重建模型實際看到的資料。這會破壞第 02 章建立的可重放性。

因此，本章要回答的問題是：為什麼動態狀態要作為 user 訊息重新發送，而不是寫進 system prompt？

system prompt 需要保持穩定，log 則必須完整記錄模型看過的動態狀態。組裝過程因此必須：

1. 使用單一 registry 管理四種 provider：固定的 system sections、動態 context、`{{name}}` 對應的 variable，以及 tool schema。每次註冊都會回傳撤銷函式。
2. 組裝結果必須穩定：每筆內容都有數字順序，同分時依註冊順序排列，確保相同註冊永遠產生相同文字。
3. 變數代入必須嚴格：`{{name}}` 若不存在或尚未設定，就取消這次 request，避免送出殘缺 prompt。
4. 每次組裝會產生 system 文字、當次 request 的工具清單，以及 runtime-context 快照。
5. 快照以 `user/message` 送出，只有內容變更時才重發。比對基準直接取自 log 中最後一筆快照，不維護額外狀態。
6. 每個 step 都在重新推導歷史的同一個邊界進行一次組裝。

---

## 核心機制

本章新增 `system_prompt.py`，並將 request 組裝集中到這裡：

- **`SystemPrompt`**：prompt registry。`section()`、`context()`、`variable()`、`tools()` 用來註冊 provider，並依 kernel 慣例回傳撤銷函式。內建的 `harness:identity` 使用 order -100，因此 plugin 提供的文字預設排在它後面。
- **`assemble(assemble_context)`**：依 `(order, 註冊順序)` 解析所有 provider，回傳 `system`、`tools` 與 `runtime_context`。
- **工具橋接**：plugin 會註冊一個 tool schema provider，從 assemble context 取得 agent 在目前作用域可見的工具，讓工具清單也成為 prompt 組裝結果的一部分。
- **`latest_snapshot(session)`**：負責去重。拿來比對的那份快照是 log 的投影，也就是 payload 帶著 `"kind": "runtime-context"` 的最後一筆 `user/message`。

```python
def assemble(self, assemble_context):
    """Resolve every provider, in order: the request's three artifacts."""
    sections = [self._render(e["text"], assemble_context) for e in _ordered(self._sections)]
    contexts = [e["provider"](assemble_context) for e in _ordered(self._contexts)]
    return {
        "system": "\n\n".join(text for text in sections if text),
        "tools": [s for provider in self._tools for s in provider(assemble_context)],
        "runtime_context": "\n".join(text for text in contexts if text),
    }
```

在 `_step()` 裡面，組裝就接在 inbox 認領後面，位置是第 04 章本來就會把所有東西重新推導一次的那個邊界。快照只有跟最後一筆快照不一樣，才會進 log；同時 Model seam 多了第三個值：

```python
assembly = self.prompt.assemble({"tools": self.tools})
snapshot = assembly["runtime_context"]
if snapshot and snapshot != latest_snapshot(self.session):
    self.session.append("user/message", {"content": snapshot, "kind": "runtime-context"})
messages = self.session.derive_messages()  # re-derived, never cached
```

provider 會在每個 step 重新計算內容，只有變更過的快照會寫入 log：

```text
registered, ordered              assemble({"tools": scope}), every step

sections  -100 harness:identity ─┐
             0 persona           ├─► system text ────► request, byte-identical
variables  {{user}} = "Ada"     ─┘                     every step
tool providers  the bridge ──────► tool list ────────► request
contexts     0 time: 10:01      ─┐
            10 cwd: /home/ada    ├─► snapshot ─► same as the last snapshot
                                 ┘               row in the log?
                                                 ├─ yes: nothing appended
                                                 └─ no:  user/message row,
                                                         "kind": "runtime-context"
```

以下是實際執行時的 log。`tick` 工具會在 turn 中途調整模擬時鐘；兩次 request 的 system 文字都維持 61 個字元，內容完全相同，但 runtime-context 快照會因時間變化而重發一次：

```text
send("go")
  │   0  turn/start
  │   1  step/start
  │   2  user/message   "go"                  ◄ claimed at the boundary
  │   3  user/message   "time: 10:00"         ◄ snapshot, first reading
  │   4  request/header system 61 chars, tools ["tick"]
  │   5  assistant/message {"tool_calls": [tick]}
  │   6  tool/call     tick
  │   7  tool/result   "ticked"               ◄ the clock now says 10:01
  │   8  step/end      {"reason": null}
  │   9  step/start
  │  10  user/message   "time: 10:01"         ◄ changed: re-emitted
  │  11  request/header system 61 chars, tools ["tick"]
  │  12  assistant/chunk "do"
  │  13  assistant/chunk "ne"
  │  14  assistant/message "done"
  │  15  step/end      {"reason": "completed"}
  │  16  turn/end
```

如果時鐘沒有變動，seq 10 就不會出現：第二個 step 發現快照與上一筆相同後，不會追加任何事件。模型看過的讀數都以普通 `user` 訊息保存在歷史中，因此可以持久化與重放。

### 改了什麼

與第 07 章相比：

- `inbox.py`、`kernel.py`、`message.py`、`scheduler.py`、`session_log.py`、 `tools.py` 完整沿用。`system_prompt.py` 是唯一的新原始碼檔案；其他改動都是把組裝接進 `agent_loop.py`，因此與第 07 章相比，diff 只包含本章新增的機制，不包含其他改動。
- `agent_loop.py`：`Agent` 和 `AgentRegistry.create()` 多了一個 `prompt` 參數。`_step()` 每個 step 組裝一次，快照變了就追加一筆，tool 清單改成從組裝的結果拿、不再直接跟 registry 要，並且把 system 文字經由 Model seam 傳下去。
- `standin.py`：Model seam 的簽名多了 `system=""`，就一行。Scripted stand-in 還是被動的：它從來不去看 request 裡有什麼，system 文字也一樣不看。
- log 的長相變了：`request/header` 現在會記下組裝出來的 system 文字，而 `user/message` 的 payload 可能帶著 `"kind": "runtime-context"`，用來標記這是一筆快照。推導歷史的時候，兩種都當成普通的 `user` 訊息。
- `demo.py`：實機示範註冊一段 persona 文字，把真的時鐘和 cwd 當成 context 收進來，再放一個很慢的 tool，慢到時鐘會在 turn 中途走動，所以重發這件事會發生在一次實際模型呼叫上。

---

## 對照真正的 dsh

以下連結皆指向研究版本 [`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca)。registry 位於 core 的 system-prompt 套件裡，快照去重則在 loop 裡： [`packages/core/system-prompt`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/system-prompt)。

| Mini-dsh | 真正的 dsh | 說明 |
| --- | --- | --- |
| `system_prompt.py` 裡的 `SystemPrompt` | [`packages/core/system-prompt/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/system-prompt/src/index.ts)：`SystemPrompt` | 一樣是 `section() / context() / variable() / tools()` 後面那四種 provider，每一個都回傳一個 Cordis 的 effect disposer，也就是 mini 那個撤銷函式在真實世界裡的樣子。 |
| `assemble()` 回傳三樣東西 | [`index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/system-prompt/src/index.ts)：`PromptAssembly`、`renderPrompt` | 組裝先解成一個 `PromptAssembly`，走過 `system-prompt/assemble` 這個 waterfall，再算出 `system` 字串、這次 request 的 tool 清單，以及 runtime-context 快照。 |
| 內建 identity 的 `order=-100` | [`index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/system-prompt/src/index.ts)：`'harness:identity'` | 內建的 identity 那一段坐在 order -100，對外匯出的 `PERSONA_SECTION` 在 0，tool 的指引在 100 到 199。排序就是一個數字欄位 `order`，不是什麼階段列舉。 |
| `{{name}}` 的嚴格代入 | [`index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/system-prompt/src/index.ts) | `{{variable}}` 是嚴格代入：名字不認得，或值是 undefined，就直接丟出例外，跟 mini 那條「不合格就不送」的規則一模一樣。 |
| `latest_snapshot(session)` | [`packages/core/agent-loop/src/runtime-context.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-loop/src/runtime-context.ts)：`RuntimeContextProjection` | 拿來比對的快照是一份投影；只有跟它不一樣的時候，快照才會以 `user/message` 的身分發出去，永遠不會變成 system 文字。 |
| `_step()` 裡面的組裝 | [`packages/core/agent-loop/src/agent.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-loop/src/agent.ts)：`preStep` | 組裝每個 step 做一次，發生在 `preStep` 裡面、`agent/pre-step` 這個 hook 之前，跟 mini 用的是同一個邊界（第 230 行）。 |
| `system_prompt_plugin` 的工具橋接 | [`packages/core/tools/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/tools/src/index.ts)：`ctx.systemPrompt.tools(...)` | 工具會將 schema 註冊為 prompt provider（第 832 到 836 行）。Mini-dsh 在 prompt plugin 中完成橋接；真正的 dsh 則由 tools 套件主動註冊。 |
| 檢查裡用的 time context | [`packages/context/time-context/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/context/time-context/src/index.ts) | 有一整個套件家族都用這種方式提供 context；`agent-instructions` 也是走同一條通道，把工作區的指示送進來。 |

真正的 system-prompt 這一層還提供以下功能：

- **組裝前後有事件。**`system-prompt/assemble` 是一個會依 scope 過濾的 waterfall，可以在組裝還在進行的時候就把結果改掉，而 `system-prompt/change` 會公告 registry 有變動。mini 的組裝沒有任何 hook。
- **tool 的順序有明確規則。**真正的 dsh 在排這次 request 的 tool 清單時，會照一個寫死的常數 `TOOL_ORDER_REST` 來排；mini 就只靠註冊順序。
- **registry 之外還有一條 context 通道。**`packages/context` 底下大部分的東西根本不走 `systemPrompt.context()`：`agent-instructions`、`time-context`、 `tmux-context` 都是從 `agent/pre-step` 的 listener 直接追加 `UserMessage`。真正會去呼叫 registry 那個 `context()` 的，是 sandbox 政策、核准政策，還有 subagent 的委派。真正的 sandbox 隔離超出本教學的實作範圍；mini 那個改寫 argv 的替身，要等第 10 章講 capability seam 的時候才會出現。
- **section 可以延後完成。** 真正的 `PromptSection` 可宣告 `complete?`，讓組裝先繼續進行，較慢的 provider 之後再補上內容。Mini-dsh 的 provider 則全部同步執行。

---

## 常見失敗模式

- **把時鐘放進 system 文字會讓每個 step 的前綴快取失效。** 只要時間戳持續變動，prompt 前綴就無法重用。將 section 與 context 分開，可以從結構上確保動態內容不會進入 system 文字。
- **只在 request 中臨時附加文字，重放時就會遺失。** 動態狀態必須以 `user/message` 快照寫入 log，system 文字則記在 `request/header`，才能完整重建模型實際收到的內容。
- **快照未變仍重發，會無端增加歷史長度。** 每次 request 都多帶一筆相同資料，卻沒有新增資訊。系統會在邊界與最後一筆快照比較，只在變更時追加。
- **將比對快照放在記憶體，重啟後會與 log 不一致。** 記憶體狀態會消失，第一個 step 可能重送模型已看過的快照。Mini-dsh 直接從 log 推導比對基準，讓去重與重放維持一致。
- **寬鬆代入可能送出未填值的 prompt。** `{{typo}}` 若原樣送給模型，只會形成無意義內容。嚴格代入會在送出 request 前拋錯，log 也會顯示 step 停在 `request/header` 之前。
- **provider 缺少穩定順序，組裝結果就可能改變。** 若依賴 dict 順序或完成時間，同一組註冊可能產生不同 prompt。使用數字 order，並以註冊順序處理同分項目，才能得到穩定結果。

---

## 動手驗證

[`src/`](src/) 延續第 07 章，並加入：

- [`system_prompt.py`](src/system_prompt.py)（新的）：`SystemPrompt`，四種 provider，每一次註冊都給一個撤銷函式；`assemble()`；`latest_snapshot()`；還有那個 plugin，內建 identity 和 tool schema 的橋都在裡面。
- [`agent_loop.py`](src/agent_loop.py)：`_step()` 每個 step 組裝一次，快照變了就追加一筆，並把 system 文字經由 Model seam 傳下去；`Agent` 和 `create()` 多了 `prompt` 參數。
- [`standin.py`](src/standin.py)：seam 的簽名多了 `system=""`；Scripted stand-in 一樣不去看它。
- [`test.py`](src/test.py)：離線測試證明三樣東西會落在同一次 request 裡； turn 中途 tick 一下會讓快照重發，而 system 文字一個位元組都沒變；去重在同一個 turn 內和跨 turn 都成立；`{{variable}}` 不認得或沒設值，會讓這個 step 停在任何 request 送出去之前；每一次註冊都撤銷得掉。
- [`demo.py`](src/demo.py)：實機示範在內建 identity 上面疊一段 persona，把真的時鐘和 cwd 拍成快照，再讓一個很慢的 tool 逼出一次 turn 中途的重發，整段跑在實際模型呼叫上。

```bash
python sections/08-system-prompt/src/test.py    # offline check, no key
```

實機示範需要根目錄的 `requirements.txt` 和一把 key；沒有設定 key 時會自動跳過：

```bash
pip install -r requirements.txt         # anthropic + python-dotenv
cp .env.example .env                    # then set ANTHROPIC_API_KEY
python sections/08-system-prompt/src/demo.py
```

---

## 參考資料

- [`docs/subsystems/system-prompt.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/subsystems/system-prompt.md)： dsh 自己帶你走一遍那四種 provider，還有算出來的那三樣東西。
- [`packages/context/README.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/context/README.md)： context 這一整個套件家族，還有裡面哪些成員走 registry、哪些走 pre-step 那條通道。
