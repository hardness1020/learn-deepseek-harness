<!-- source: README.md @ d5b8152 -->

# 08 · System prompt

[English](README.md) | 繁體中文 | [简体中文](README.zh-CN.md)

> harness 裡有好幾塊都各自掌管一段要告訴 model 的文字，而且每個 step 送出的字必須完全一樣。所以會在 step 之間變動的東西，不能放進那段文字裡。

Section 07 送出去的 request 很誠實，但也很空。`_step()` 直接從 tool registry 撈 schema，system 文字則是一個字都不帶：沒有人告訴 model 它是誰、該怎麼表現、現在外面的世界長什麼樣。

harness 裡有好幾個部分會各自寫一段這種文字。Mini-dsh 寫自己的身分那一行；persona plugin 寫語氣；tool 這一層寫 schema 清單。每一方都想把自己的那一段放進去，又不想為了這件事跟別人協調；而且每一段在每次 request 裡，都得落在同一個位置。

而且有些狀態是會變的。時鐘、工作目錄這種：model 要的是當下的讀數；但只要把它寫死在 system 文字裡，就不會有任何兩個 step 送出一樣的 prompt。model 那一端是靠穩定的 prompt 前綴在做快取，所以 system 文字裡只要有一個時間戳，每個 step 的快取都會落空。

另一個直覺的做法更糟：把動態文字從旁邊補進 request，它就永遠不會進到 log 裡。重放的時候，你重建不出 model 真正看到的東西，而那正是 Section 02 的全部重點。

所以：為什麼動態狀態是一則重新發出的 user 訊息，而不是寫進 system 文字裡？

因為 system 文字必須待著不動，而 log 必須是完整的故事。要做到這件事，組裝的過程必須：

1. 只留一個 registry，裡面有四種 provider：sections（固定不動的 system 文字）、context（動態狀態）、variable（`{{name}}` 要填的值），還有 tool schema 的 provider。每一次註冊都會回傳它自己的撤銷函式。
2. 算出來的結果要固定：每一筆有一個數字順序，同分就照註冊順序排，所以同樣的註冊永遠算出同樣的文字。
3. 代入變數要嚴格：`{{name}}` 對應的變數不認得，或根本沒設值，就直接不送這次 request，而不是送一個帶著洞的 prompt 出去。
4. 一次組裝產出三樣東西：system 文字、這次 request 的 tool 清單，還有一份 runtime-context 快照。
5. 快照用一筆 `user/message` 送出去，而且只有變了才重發。拿來比對的那份快照，就是 log 裡最後一筆快照本身，不另外存一份狀態。
6. 每個 step 組裝一次，就在邊界上，跟歷史重新推導的位置同一個地方。

---

## Mechanism

一個新檔案 `system_prompt.py`，再把 request 的組裝改道，讓它走這裡：

- **`SystemPrompt`**：那個 registry。`section()`、`context()`、`variable()`、 `tools()` 負責把 provider 收進來；每一個都照 kernel 的做法，回傳自己的撤銷函式。內建的 `harness:identity` 這一段坐在 order -100，所以 plugin 的文字預設會排在它後面。
- **`assemble(assemble_context)`**：照 `(order, 註冊順序)` 把每個 provider 解出來，回傳那三樣東西：`system`、`tools`、`runtime_context`。
- **那座橋**：plugin 註冊一個 tool schema 的 provider，從 assemble context 裡拿出 agent 在作用域內看得到的那些 tool，所以這次 request 的 tool 清單，也算是 prompt 組裝出來的東西之一。
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

在 `_step()` 裡面，組裝就接在 inbox 認領後面，位置是 Section 04 本來就會把所有東西重新推導一次的那個邊界。快照只有跟最後一筆快照不一樣，才會進 log；同時 Model seam 多了第三個值：

```python
assembly = self.prompt.assemble({"tools": self.tools})
snapshot = assembly["runtime_context"]
if snapshot and snapshot != latest_snapshot(self.session):
    self.session.append("user/message", {"content": snapshot, "kind": "runtime-context"})
messages = self.session.derive_messages()  # re-derived, never cached
```

provider 在一邊把東西算出來；只有變過的快照會跨進 log：

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

下面是一次真的執行，照 log 記下來的樣子。一個叫 `tick` 的 tool 在 turn 中途撥動一個假時鐘；兩次 request 的 system 文字都是 61 個字元，一個位元組都不差，而快照重發了一次：

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

時鐘要是沒動，seq 10 根本不會出現：第二個 step 會發現快照跟最後一筆快照一樣，什麼都不追加。model 看過的那兩個讀數，在推導出來的歷史裡都是普通的 `user` 紀錄，存得住，也重放得出來。

### 改了什麼

跟 Section 07 比起來：

- `inbox.py`、`kernel.py`、`message.py`、`scheduler.py`、`session_log.py`、 `tools.py` 原封不動搬過來。`system_prompt.py` 是唯一的新原始碼檔案；其他改動都是把組裝接進 `agent_loop.py`，所以跟 07 的 diff 剛好就是這個 Section 的 Mechanism，沒有別的。
- `agent_loop.py`：`Agent` 和 `AgentRegistry.create()` 多了一個 `prompt` 參數。`_step()` 每個 step 組裝一次，快照變了就追加一筆，tool 清單改成從組裝的結果拿、不再直接跟 registry 要，並且把 system 文字經由 Model seam 傳下去。
- `standin.py`：Model seam 的簽名多了 `system=""`，就一行。Scripted stand-in 還是被動的：它從來不去看 request 裡有什麼，system 文字也一樣不看。
- log 的長相變了：`request/header` 現在會記下組裝出來的 system 文字，而 `user/message` 的 payload 可能帶著 `"kind": "runtime-context"`，用來標記這是一筆快照。推導歷史的時候，兩種都當成普通的 `user` 訊息。
- `demo.py`：Live demo 註冊一段 persona 文字，把真的時鐘和 cwd 當成 context 收進來，再放一個很慢的 tool，慢到時鐘會在 turn 中途走動，所以重發這件事會發生在一次真的 model 呼叫上。

---

## In real dsh

所有指過去的連結都固定在 Studied version [`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca) 上。registry 住在 core 的 system-prompt 套件裡，快照去重則在 loop 裡： [`packages/core/system-prompt`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/system-prompt)。

| Mini-dsh | 真正的 dsh | 說明 |
| --- | --- | --- |
| `system_prompt.py` 裡的 `SystemPrompt` | [`packages/core/system-prompt/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/system-prompt/src/index.ts)：`SystemPrompt` | 一樣是 `section() / context() / variable() / tools()` 後面那四種 provider，每一個都回傳一個 Cordis 的 effect disposer，也就是 mini 那個撤銷函式在真實世界裡的樣子。 |
| `assemble()` 回傳三樣東西 | [`index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/system-prompt/src/index.ts)：`PromptAssembly`、`renderPrompt` | 組裝先解成一個 `PromptAssembly`，走過 `system-prompt/assemble` 這個 waterfall，再算出 `system` 字串、這次 request 的 tool 清單，以及 runtime-context 快照。 |
| 內建 identity 的 `order=-100` | [`index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/system-prompt/src/index.ts)：`'harness:identity'` | 內建的 identity 那一段坐在 order -100，對外匯出的 `PERSONA_SECTION` 在 0，tool 的指引在 100 到 199。排序就是一個數字欄位 `order`，不是什麼階段列舉。 |
| `{{name}}` 的嚴格代入 | [`index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/system-prompt/src/index.ts) | `{{variable}}` 是嚴格代入：名字不認得，或值是 undefined，就直接丟出例外，跟 mini 那條「不合格就不送」的規則一模一樣。 |
| `latest_snapshot(session)` | [`packages/core/agent-loop/src/runtime-context.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-loop/src/runtime-context.ts)：`RuntimeContextProjection` | 拿來比對的快照是一份投影；只有跟它不一樣的時候，快照才會以 `user/message` 的身分發出去，永遠不會變成 system 文字。 |
| `_step()` 裡面的組裝 | [`packages/core/agent-loop/src/agent.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-loop/src/agent.ts)：`preStep` | 組裝每個 step 做一次，發生在 `preStep` 裡面、`agent/pre-step` 這個 hook 之前，跟 mini 用的是同一個邊界（第 230 行）。 |
| `system_prompt_plugin` 裡的那座橋 | [`packages/core/tools/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/tools/src/index.ts)：`ctx.systemPrompt.tools(...)` | tool 把自己的 schema 註冊成一個 prompt provider（第 832 到 836 行）。mini 把這座橋收進 prompt 的 plugin 裡；真正的 dsh 則是從 tools 套件那一側註冊。 |
| 檢查裡用的 time context | [`packages/context/time-context/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/context/time-context/src/index.ts) | 有一整個套件家族都用這種方式提供 context；`agent-instructions` 也是走同一條通道，把工作區的指示送進來。 |

真正的 system-prompt 這一層，在這個 Section 的 Mechanism 之上，還多做了這些：

- **組裝前後有事件。**`system-prompt/assemble` 是一個會依 scope 過濾的 waterfall，可以在組裝還在進行的時候就把結果改掉，而 `system-prompt/change` 會公告 registry 有變動。mini 的組裝沒有任何 hook。
- **tool 的順序有明確規則。**真正的 dsh 在排這次 request 的 tool 清單時，會照一個寫死的常數 `TOOL_ORDER_REST` 來排；mini 就只靠註冊順序。
- **registry 之外還有一條 context 通道。**`packages/context` 底下大部分的東西根本不走 `systemPrompt.context()`：`agent-instructions`、`time-context`、 `tmux-context` 都是從 `agent/pre-step` 的 listener 直接追加 `UserMessage`。真正會去呼叫 registry 那個 `context()` 的，是 sandbox 政策、核准政策，還有 subagent 的委派。真正的 sandbox 隔離在 Ceiling 之上；mini 那個改寫 argv 的替身，要等 Section 10 講 capability seam 的時候才會出現。
- **有些 section 可以慢慢來。**真正的 `PromptSection` 可以宣告 `complete?`，讓組裝先往下走，慢的 provider 之後再把內容補上。mini 的 provider 都是同步的。

---

## Failure modes

- **system 文字裡放一個時鐘，每個 step 的快取都會落空。**model 那一端是靠穩定的 prompt 前綴做快取，而 system 文字就排在前綴的最前面。只要有一個時間戳每個 step 重算一次，就沒有任何一次 request 用得到那個前綴。section 和 context 分成兩邊，等於從結構上就把所有會變的位元組擋在 system 文字之外。
- **文字從旁邊補進 request，就會從紀錄裡消失。**狀態補進了 request，卻沒有留下任何一筆 log，重放的時候就重建不出 model 看到的東西。快照是一筆 `user/message`，就是普通的推導歷史；連 system 文字都會記在 `request/header` 上，所以 log 還是完整的故事。
- **沒變也重發，歷史會被灌爆。**每個 step 都把讀數追加一次，等於後面每一次 request 都多背一筆，卻沒多帶任何資訊。邊界會拿它跟最後一筆快照比一下，變了才追加。
- **比對用的快照放在記憶體裡，它會跟 log 對不上。**重新開起來之後記憶體是空的， log 卻不是，於是第一個 step 又把 model 早就看過的快照發一次。mini 是直接從 log 推出比對用的那份快照，所以去重和重放天生就對得上。
- **代入太寬鬆，會送出一個帶洞的 prompt。**一個 `{{typo}}` 就這樣以大括號的原樣送到 model 面前，讀起來就是一句沒有意義的話。嚴格代入會改成丟出例外，而 log 上看得到這個 step 停在 `request/header` 之前：這次根本沒有 request 送出去。
- **provider 沒有順序，文字就會亂跳。**如果算的時候照的是 dict 順序或誰先跑完，同樣的註冊在不同次執行就可能算出不同的 prompt，前綴快取又落空一次。一個數字順序，同分照註冊順序，每次算出來的文字都一樣。

---

## 跑跑看

[`src/`](src/) 把 07 搬過來，然後加上：

- [`system_prompt.py`](src/system_prompt.py)（新的）：`SystemPrompt`，四種 provider，每一次註冊都給一個撤銷函式；`assemble()`；`latest_snapshot()`；還有那個 plugin，內建 identity 和 tool schema 的橋都在裡面。
- [`agent_loop.py`](src/agent_loop.py)：`_step()` 每個 step 組裝一次，快照變了就追加一筆，並把 system 文字經由 Model seam 傳下去；`Agent` 和 `create()` 多了 `prompt` 參數。
- [`standin.py`](src/standin.py)：seam 的簽名多了 `system=""`；Scripted stand-in 一樣不去看它。
- [`test.py`](src/test.py)：Offline check 證明三樣東西會落在同一次 request 裡； turn 中途 tick 一下會讓快照重發，而 system 文字一個位元組都沒變；去重在同一個 turn 內和跨 turn 都成立；`{{variable}}` 不認得或沒設值，會讓這個 step 停在任何 request 送出去之前；每一次註冊都撤銷得掉。
- [`demo.py`](src/demo.py)：Live demo 在內建 identity 上面疊一段 persona，把真的時鐘和 cwd 拍成快照，再讓一個很慢的 tool 逼出一次 turn 中途的重發，整段跑在真的 model 呼叫上。

```bash
python sections/08-system-prompt/src/test.py    # offline check, no key
```

Live demo 需要根目錄的 `requirements.txt` 和一把 key；沒有 key 的話，它會安靜地跳過：

```bash
pip install -r requirements.txt         # anthropic + python-dotenv
cp .env.example .env                    # then set ANTHROPIC_API_KEY
python sections/08-system-prompt/src/demo.py
```

---

## 出處

- [`docs/subsystems/system-prompt.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/subsystems/system-prompt.md)： dsh 自己帶你走一遍那四種 provider，還有算出來的那三樣東西。
- [`packages/context/README.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/context/README.md)： context 這一整個套件家族，還有裡面哪些成員走 registry、哪些走 pre-step 那條通道。
