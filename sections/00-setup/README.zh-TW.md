<!-- source: README.md @ 8c7e193 -->

# 00 · Setup

[English](README.md) | 繁體中文 | [简体中文](README.zh-CN.md)

> 哪裡需要答案，就直接用一家 provider 的 SDK，那家廠商的形狀最後會跑進 prompt、跑進 log、跑進 loop。核心只認一種訊息形狀、只留一個可以換掉的呼叫，provider 就會待在邊界上。

DeepSeek Harness（dsh）是一套貨真價實的 agent harness：一個大型的 TypeScript 程式碼庫，裡面的 tool、prompt，甚至一整個子系統，都是 plugin，掛在一個正在跑的 kernel 上。這份 tutorial 只用 Python 標準函式庫，把它重建成一個最小的版本，一個 Section 只加一個 Mechanism。

這些 Mechanism 全都繞著同一件事轉：跟 model 要一個回答。歷史是為 model 推導出來的，tool 是 model 叫起來的，prompt 是為 model 組出來的。

所以重建這件事需要一個「怎麼問」的辦法，而最直覺的辦法，就是 import 一家 provider 的 SDK，哪裡需要答案就在哪裡呼叫它。

這麼一來，那家 provider 會滲進整套 harness。它的請求格式會跑到組 prompt 的地方，它的回應物件會跑進 log，它的 role 名稱會跑進 compaction；想換一家 provider，每個沾到的地方都得動手改。

而且答案不是一次到齊的。model 是一邊寫一邊把字吐出來，所以呼叫端要是非等到一整串完整的文字不可，等的這段時間就什麼都端不出來，log 也要等到最後才有東西可以記。

所以：為什麼 mini-dsh 的核心只講自己的 Message 形狀，而且要透過一個可以換掉的 Model seam 去問 model？

因為這套 harness 真正要講的，是 model 呼叫外面那一整圈事情，而那些事情都不該管回答的是誰家的 model。送進去是同一種形狀，拿回來也是同一種形狀，provider 就變成一個插上去就能用的零件。要做到這件事，Section 00 得先：

1. 給 mini-dsh 一套自己的 **Message 形狀**，跟真正的 dsh 一樣不綁任何 provider，這樣就不會有哪家廠商的傳輸格式滲進核心。
2. 把 **Model seam** 定下來：一個普通的 callable，收下訊息清單，先串流出 chunk 事件，最後剛好收在一則訊息上。
3. 附上一支 **Scripted stand-in**，照這份約定講話，讓這個 seam 一出現就有一個真的跑得動的實作。
4. 每一則回應都用同一套規則切成 chunk，這樣串流從第一天就是真的。

這份 tutorial 怎麼檢查自己，是從這個 seam 長出來的。stand-in 照著一條排好順序的佇列回答，裡面全是寫死的回應，而且它從來不去看送進來的請求，所以每個 Section 的檢查都能離線跑、不用 key，每次跑出來的東西都一模一樣。

---

## Mechanism

三個零件，一個檔案放一個：

- **`Message`**（`message.py`）：跟 model 來回交換的東西都長這個形狀，一個凍結的 dataclass，只有 `role` 和 `content`。
- **Model seam**：它不是一個類別，是一套呼叫慣例。`model(messages)` 先 yield 出 `("chunk", str)` 事件，最後 yield 一個 `("message", Message)`。
- **`ScriptedModel`**（`standin.py`）：seam 的第一個實作，一條佇列，裡面裝著寫死的回應。

Message 的形狀就是這套系統的全部詞彙：

```python
@dataclass(frozen=True)
class Message:
    role: str  # "user" | "assistant" | "tool"
    content: str
```

之所以凍結，是因為一則訊息記錄的是已經說出口的話，不是還能改的草稿。之所以不綁 provider，是因為核心不該在意回答的是誰家的 model；把這個形狀翻成某家廠商的傳輸格式，那是 adapter 的事，而核心裡面一個 adapter 也沒有。

三種 role 就涵蓋了這套 harness 會有的所有來回：使用者說了什麼、model 說了什麼、tool 回了什麼。後面的 Section 會在這些訊息外面加事件型別，而不是往訊息裡面加欄位。

seam 本身就是一套呼叫慣例。任何一個 callable，只要收下一份訊息清單、再 yield 出這兩種事件，它就算是一個 model；所以 adapter 可以是一個函式，可以是一個閉包，也可以像 stand-in 那樣是一個物件：

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

`messages` 傳進來了，卻從來沒被讀過。不管你問什麼，stand-in 都照著腳本、照著順序回答，而整份腳本就攤在寫它的那個檢查裡：第一則回應永遠對應第一次呼叫。

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

這兩個階段比 stand-in 本身重要得多。chunk 是當場流過去的那一段；最後那則 `Message` 才是留得住的紀錄，而且它每次都會把完整的文字再講一遍。到了 Section 02，log 會把這兩種東西存成不同的事件型別；到了 Section 04，loop 會把兩種都往下傳，中間不做任何緩衝。

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

- **某一家廠商的回應形狀會四處蔓延。** provider 回什麼就存什麼，於是 log 裡放的是它那份 JSON，compaction 學到的是它的 role 名稱，組 prompt 的程式碼則是照著它的請求格式寫的。等到要換一家 provider，這三個地方都得改。只有一種 Message 形狀，翻譯這件事就被關在一個 adapter 裡面。
- **訊息可以改，歷史就能被就地改寫。** Section 02 和 03 把記下來的訊息當成已經發生的事實，而 compaction 想縮掉 model 看到的東西，也只能走 log 這條路。要是一則訊息的欄位可以隨手重新指派，這兩件事就都不成立了：紀錄和 model 看到的畫面會愈飄愈開，而且改過的痕跡一點都不留。
- **seam 只回一整串寫完的文字，串流就被丟掉了。** model 在寫的時候，呼叫端沒有東西可以端出來，Section 02 也沒有 chunk 事件可以記，而一段長一點的回答看起來就像卡住了。有了 chunk，只要第一批位元組出來，harness 手上就有東西可以往下傳。
- **只有 chunk、沒有收尾的那則訊息，重組的工作就落到每一個呼叫端頭上。** loop、log，還有每一段在旁邊看著的程式碼，都得自己接出一份自己的副本，而每一份都可能在接縫的地方悄悄接錯。最後那一個 `("message", Message)` 讓這份留得住的紀錄只在 seam 這裡組一次。
- **把 seam 定義成一個基底類別，等於把整套 harness 拖進每一個 adapter 裡。** 要繼承，就代表 provider 得一併吃下 harness 那個類別已經先假設好的東西；而一個普通的函式，或是一個包住另一個 model 的閉包，就再也不算數了。改成一套呼叫慣例，要求就只停在「會 yield 出這兩種事件」，換一個 model 也就是傳一個不一樣的 callable 進去而已。

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
