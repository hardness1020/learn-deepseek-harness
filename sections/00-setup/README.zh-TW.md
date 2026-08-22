<!-- source: README.md @ 55e829b -->

# 00 · Setup

[English](README.md) | 繁體中文 | [简体中文](README.zh-CN.md)

> 每個 Section 收尾，都要檢查程式內容，拿結果說話。可是真的 model，同一道題問兩次就給你兩個答案，所以這裡先由腳本補位。

這份 tutorial 要重建一套 harness，而它裡面每一個 Mechanism 都繞著一次 model 呼叫轉：歷史是為 model 推導出來的，tool 是 model 叫起來的，prompt 是為 model 組出來的。14 個 Section，每一個的結尾都有跑得起來的檢查，必須證明自己那個 Mechanism 真的會動。

最直覺的做法，是把一個真的 API 擺在這些檢查後面：問一次真的 model，再對它的回答做斷言。

但真的 model 要 key、要網路、要花錢，而且同樣的輸入餵進去，吐回來的東西每次都不一樣。斷言掛掉的時候，你分不出是程式碼壞了，還是 model 今天心情不同；會因為兩種原因失敗的檢查，就算過了也證明不了什麼。更麻煩的是，這種不穩定出現在最不該出現的地方：這裡要研究的是 model 外面那套 harness，從來不是 model 本身。

所以：為什麼每個 Section 的檢查都得離線、對著 stand-in 跑？

因為檢查存在的理由，就是證明這個 Section 的 Mechanism；而 model 剛好是唯一一個會動、行為卻不歸這個 Mechanism 管的零件。把 model 鎖住，每次檢查都會得到固定結果：不用 key、不用網路，跑幾次輸出都一模一樣。要做到這件事，Section 00 得先：

1. 給 mini-dsh 一套自己的 **Message 形狀**，跟真正的 dsh 一樣不綁任何 provider，這樣就不會有哪家廠商的傳輸格式滲進核心。
2. 把 **Model seam** 定下來：一個普通的 callable，收下訊息清單，先串流出 chunk 事件，最後剛好收在一則訊息上。
3. 附上一支 **Scripted stand-in**，照這份約定講話：一條照順序排好的佇列，裡面是寫死的回應，從來不去看送進來的請求。
4. 每一則回應都用同一套規則切成 chunk，這樣串流從第一天就是真的，而且每次跑出來一模一樣。

---

## Mechanism

三個零件，一個檔案放一個：

- **`Message`**（`message.py`）：跟 model 來回交換的東西都長這個形狀，一個凍結的 dataclass，只有 `role` 和 `content`。
- **Model seam**：它不是一個類別，是一套呼叫慣例。`model(messages)` 先 yield 出 `("chunk", str)` 事件，最後 yield 一個 `("message", Message)`。
- **`ScriptedModel`**（`standin.py`）：seam 的離線實作，一條佇列，裡面裝著寫死的回應。

Message 的形狀就是這套系統的全部詞彙：

```python
@dataclass(frozen=True)
class Message:
    role: str  # "user" | "assistant" | "tool"
    content: str
```

之所以凍結，是因為一則訊息記錄的是已經說出口的話，不是還能改的草稿。之所以不綁 provider，是因為核心不該在意回答的是誰家的 model；把這個形狀翻成某家廠商的傳輸格式，那是 adapter 的事，而核心裡面一個 adapter 也沒有。

stand-in 是 seam 的第一個實作，而且刻意做得很被動：

```python
class ScriptedModel:
    def __init__(self, responses):
        self._queue = list(responses)

    def __call__(self, messages):
        """The Model seam: yields ("chunk", str)... then ("message", Message)."""
        text = self._queue.pop(0)
        for piece in _chunks(text):
            yield ("chunk", piece)
        yield ("message", Message(role="assistant", content=text))
```

`messages` 傳進來了，卻從來沒被讀過。不管你問什麼，stand-in 都照著腳本、照著順序回答。要是它會去比對請求內容，比對規則就會一條一條長出來，規則之間又互相牽扯，多到自己變成第二個 model，然後這個 model 又得再測一次。改用照順序排的佇列，整份腳本就攤在寫它的檢查裡：第一則回應永遠對應第一次呼叫。

每一則回應在送出最後那則訊息之前，會先切成幾塊固定大小的 chunk 串流出去：

```python
def _chunks(text, n=3):
    size = max(1, -(-len(text) // n))
    return [text[i : i + size] for i in range(0, len(text), size)]
```

一次呼叫從頭到尾穿過 seam，長這樣：

```text
check                                  ScriptedModel(["Hello, reader."])
  │
  │  model([Message("user", "hi")])
  ├──────────────────────────────────►  pop the next canned response
  │                                     (the request is never read)
  │   ("chunk", "Hello")   ◄──┐
  │   ("chunk", ", rea")   ◄──┼─────── split into fixed-size chunks
  │   ("chunk", "der.")    ◄──┘
  │   ("message", Message("assistant", "Hello, reader."))
  │◄──────────────────────────────────
```

這兩個階段比 stand-in 本身重要得多。chunk 是當場流過去的那一段；最後那則 `Message` 才是留得住的紀錄，而且它每次都會把完整的文字再講一遍。到了 Section 02，log 會把這兩種東西存成不同的事件型別；到了 Section 04，loop 會把兩種都往下傳，中間不做任何緩衝。因為 stand-in 從第一天就在串流，後面沒有任何一個 Section 需要拿真的 API 來第一次面對串流。

### 改了什麼

Section 00 前面沒有東西，所以這一格記的是後面每個 Section 都會繼承的起點：

- `src/` 從這裡開始：`message.py` 和 `standin.py` 是原始碼，`test.py` 是檢查。
- Carry-forward 這條規則從這裡開始。Section 01 會把這份 `src/` 原封不動抄過去，只加上它的 kernel，所以相鄰兩個 Section 的 diff 剛好就是一個 Mechanism，多的沒有。
- 這裡的東西還不知道 plugin、log 或 agent 是什麼。seam 現在只是一套呼叫慣例，等著有人來呼叫它。

---

## In real dsh

所有指過去的連結都固定在 Studied version [`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca) 上。Model seam 在真正的 dsh 裡的位置是 [`packages/llm`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/llm)。

| Mini-dsh | 真正的 dsh | 說明 |
| --- | --- | --- |
| `Message` | [`packages/llm/llm/src/types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/llm/llm/src/types.ts) | 詞彙型別歸 llm seam 管，跟我們一樣不綁 provider；`ToolSchema`（第 333 行）也在這個檔案裡，後面 tool 就是靠它向 model 自我介紹的。Mini-dsh 的整套詞彙只有一個 dataclass。 |
| Model seam 的約定 | [`packages/llm/llm/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/llm/llm/src/index.ts)：`LlmAdapter`（第 180 行） | 那邊的 seam 一樣是串流：`stream(options)` 回傳一個 `AsyncIterable<StreamChunk>`。mini 這邊「先 chunk、最後一則訊息」的慣例是同一個想法，只是把最後那則訊息講明白了。 |
| 擺在 seam 後面的 `ScriptedModel` | [`packages/llm/llm/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/llm/llm/src/index.ts)：`LlmRuntime`、`ctx.llm`（第 284 行） | adapter 透過 `ctx.llm.registerAdapter(providers, adapter)` 註冊，換掉的時候呼叫端不會察覺。stand-in 就是 mini-dsh 的第一個 adapter。 |
| 呼叫 `model(messages)` 的檢查 | [`packages/core/agent-loop/src/agent.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-loop/src/agent.ts) | 真正用它的是 loop：先 `ctx.llm.prepareCall()`，再 `preparedCall.stream(request)`（第 345、449 行）。Section 04 會讓 mini 也有同一個呼叫端。 |
| 先一串 chunk，最後一則訊息 | [`packages/core/session/src/types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session/src/types.ts)（第 236 行） | 等到 log 出現（Section 02），串流的這兩個階段就變成 session 事件型別 `assistant/chunk` 和 `assistant/message`。 |

真正的 llm seam 在這個 Section 的 Mechanism 之上，還多做了這些：

- **一個會做路由的 adapter registry。** `ctx.llm` 同時放著好幾個 adapter，用 provider 名字當鍵；要挑出一次部署的預設 model，本身又是一個 plugin （[`packages/core/agent-default-model`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-default-model)， `ctx.agentDefaultModel`）。mini 這邊一次只有一個 callable，要到 Section 10 才會給 seam 一個 service 的位置。
- **串流上可以掛 middleware。** 一道 `llm/stream` waterfall（`index.ts` 第 51 到 60 行）讓 plugin 可以包住或旁觀每一次 model 呼叫，而重試會以 `llm/retry` 這種 session 事件出現在 log 裡。
- **真的會講廠商協定的 adapter。** 內建的 provider [`llm-deepseek`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/llm/llm-deepseek/src/index.ts) 和 [`llm-pi-ai`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/llm/llm-pi-ai/src/index.ts) 會講各家廠商自己的協定。Ceiling：mini-dsh 不會去重建任何一個講廠商協定的 adapter；它唯一碰到真 API 的程式碼，是 Live demo 在 `demo.py` 裡那段大約 20 行、把訊息翻成 Anthropic 格式的東西（Section 04 以後），而且它待在離線核心外面。
- **折成一份，而不是拆成三份。** 真正的 dsh 通常會把一個能力拆成三邊：一個套件定義介面，一些套件提供它，一些套件使用它。llm seam 把定義端和使用端折進同一個套件，因為使用它的就是 agent loop 本身，不是一組隨時可以換掉的 tool。Section 10 會把這個 seam 和這條折疊規則一起重建一遍。

---

## Failure modes

- **拿真的 model 來跑，每次檢查都像在賭運氣。** 同樣的輸入，吐回來的東西每次都不一樣，於是斷言不是寫得很模糊（`"contains a word"`），就是時好時壞。stand-in 每次跑出來的輸出一模一樣，所以檢查可以直接斷言確切的內容，而且說到做到。
- **會去讀請求的 stand-in，遲早變成第二個 model。** 比對規則會愈積愈多，規則之間又互相牽扯，很快這個替身就聰明到足以出錯。佇列的約定是故意做笨的：第一則回應對第一次呼叫，而且整份腳本就明明白白攤在檢查裡。
- **一口氣把整段吐完的 stand-in，等於把串流往後拖。** 如果只 yield 最後那則訊息，處理 chunk 的程式碼要到 Section 04 才第一次跑起來，而且是對著真的 API 跑，出事還重現不了。每次切法都一樣的 chunk，讓串流從第一次檢查開始就是真的。
- **對著 stand-in 的內部下斷言，檢查到的只是測試用的架子。** 伸手去摸 `_queue`，會讓檢查綁死在一個真 adapter 根本沒有的東西上。這條規則貫穿全部 14 個 Section：只對穿過 seam 的東西下斷言，等 log 出現以後就對著 log 下，永遠不要對著 stand-in 下。
- **腳本用完了，就要明明白白地失敗。** 多呼叫一次 model，就會從空佇列裡 pop，然後直接拋錯，所以問過頭的檢查會失敗，而不是默默重用一個剛好會過的答案。

---

## 跑跑看

[`src/`](src/) 是 Carry-forward 這條鏈的起點，每個檔案都是新的：

- [`message.py`](src/message.py)：凍結的 `Message` dataclass。
- [`standin.py`](src/standin.py)：`ScriptedModel`，還有它那個每次切法都一樣的切塊函式。
- [`test.py`](src/test.py)：seam 的約定站得住腳：所有 chunk 接起來剛好等於最後那則訊息的內容，串流不是只吐出一整塊，佇列也照順序回答。

```bash
python sections/00-setup/src/test.py   # offline check, no key
```

Model seam 在這裡已經有了，但還沒有哪個 Mechanism 在驅動它，所以沒有 `demo.py`。第一支 Live demo 要等 Section 04 的 agent loop 才會出現。

---

## 出處

- [learn-agent-memory](https://github.com/hardness1020/learn-agent-memory)：這個 Section 的檢查慣例（離線、不用 key、每次結果都一樣）就是從這個 tutorial 系列沿用過來的。
