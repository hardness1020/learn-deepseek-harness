<!-- source: README.md @ 3705bd7 -->

# 00 · Setup

[English](README.md) | 繁體中文 | [简体中文](README.zh-CN.md)

> harness 裡許多地方都需要呼叫模型。如果每個模組都直接依賴某家 provider 的 SDK，它的格式就會擴散到 prompt、log 和 loop 中。因此，核心只使用統一的訊息格式，並把 provider 藏在可替換的模型介面後面。

DeepSeek Harness（dsh）是一套大型 TypeScript agent harness。工具、prompt，甚至完整的子系統，都以 plugin 形式掛載在執行中的 kernel 上。本教學只使用 Python 標準函式庫實作最小版本，每章專注加入一項機制。

後續所有機制都建立在同一個基礎上：把請求送給模型，再接收回覆。對話歷史要轉成模型能理解的格式，工具要等模型發出呼叫後才執行，prompt 也必須在請求送出前組裝完成。

Mini-dsh 因此需要一個統一的模型呼叫方式。最簡單的做法，是在每個需要模型的地方直接 import 某家 provider 的 SDK。

問題是，provider 的格式會因此滲入整套 harness。prompt 組裝程式會依賴它的請求格式，log 會儲存它回傳的物件，compaction 也會綁定它的 role 命名。未來一旦更換 provider，這些模組都必須一起修改。

另一個重點是串流。模型通常會逐段產生回覆。如果呼叫端一定要等到全部完成才能回傳，使用者就只能空等，log 也無法即時記錄中間過程。

因此，本章要回答的問題是：為什麼 Mini-dsh 的核心只認自己的 `Message` 格式，並透過可替換的 Model seam 呼叫模型？

這套 harness 關心的是模型呼叫周邊的機制，不應依賴背後究竟是哪家模型。無論更換哪個 provider，核心收發的都是同一種 `Message`，provider 因此成為可以獨立替換的元件。本章會先建立：

1. Mini-dsh 自己的 **`Message` 格式**，不與任何 provider 綁定。
2. **Model seam** 的呼叫規範：一個普通 callable，接收一組訊息，先產生多個 chunk 事件，最後再產生一則完整訊息。
3. 可直接執行的 **Scripted stand-in**，用預先設定的回覆實作這套規範。
4. 穩定、可測試的分塊規則，讓串流從一開始就是真正的執行模式，而不是事後模擬。

這個 seam 也奠定了整份教學的測試方式。stand-in 內部只有一列預先排好的回覆，完全不讀取請求內容。因此，後續每章的測試都能在離線環境中執行，不需 API key，結果也可重現。

---

## 核心機制

本章包含三個核心元件，各自放在獨立檔案中：

- **`Message`**（`message.py`）：用於模型交互的統一訊息格式。它是一個凍結的 dataclass，只包含 `role` 和 `content`。
- **Model seam**：一套呼叫約定，而不是基底類別。`model(messages)` 會先 yield `("chunk", str)`，最後再 yield `("message", Message)`。
- **`ScriptedModel`**（`standin.py`）：Model seam 的第一個實作，按順序回傳預先設定的回覆。

整套 harness 都使用同一種 `Message` 格式：

```python
@dataclass(frozen=True)
class Message:
    role: str  # "user" | "assistant" | "tool"
    content: str
```

這個 dataclass 設為不可變，因為訊息一旦寫入歷史，就不應再被事後修改。它也不綁定任何 provider：核心只處理自己的訊息格式，至於如何轉成特定服務的請求格式，則由 adapter 負責。

三種 role 已足以描述 harness 內的所有互動：使用者輸入、模型回覆，以及工具結果。後續章節會在訊息外層加入事件型別，而不是不斷擴充訊息本身的欄位。

Model seam 本身只是一套呼叫約定。任何 callable 只要接收一組訊息，並 yield 出這兩種事件，就能當作模型使用。因此，adapter 可以是函式、閉包，也可以像 stand-in 一樣實作成物件：

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

`ScriptedModel` 不會讀取傳入的 `messages`。無論輸入是什麼，它都按照測試中預先排好的順序回覆，因此第一次呼叫一定取得第一則回覆，測試結果也能穩定重現。

每則回覆在送出最終訊息前，會先切成大小相近的 chunk 逐段產生：

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

真正重要的是這兩個階段。chunk 用於即時串流，最後的 `Message` 則保留完整內容，供後續寫入紀錄。第 02 章會將兩者記成不同的事件型別；第 04 章的 loop 則會原樣轉送，不額外緩衝。

### 改了什麼

第 00 章是整條 Carry-forward 鏈的起點，後續每一章都會沿用以下內容：

- `src/` 從本章開始累積：`message.py` 和 `standin.py` 是實作，`test.py` 是離線測試。
- 第 01 章會完整沿用這份 `src/`，只加入 kernel。之後也維持相同方式，讓相鄰章節的 diff 聚焦在新機制上。
- 目前還沒有 plugin、log 或 agent。Model seam 現階段只定義呼叫方式，後續章節才會加入實際呼叫它的元件。

---

## 對照真正的 dsh

以下連結皆指向研究版本 [`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca)。Model seam 在真正的 dsh 裡的位置是 [`packages/llm`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/llm)。

| Mini-dsh | 真正的 dsh | 說明 |
| --- | --- | --- |
| `Message` | [`packages/llm/llm/src/types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/llm/llm/src/types.ts) | 訊息型別由 llm seam 定義，同樣不綁定 provider。`ToolSchema`（第 333 行）也在這個檔案中，工具之後會透過它向模型描述自己。Mini-dsh 則只需要一個 dataclass。 |
| Model seam 的約定 | [`packages/llm/llm/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/llm/llm/src/index.ts)：`LlmAdapter`（第 180 行） | 真正的 seam 同樣採用串流：`stream(options)` 會回傳 `AsyncIterable<StreamChunk>`。Mini-dsh 使用「先產生 chunk，最後產生完整訊息」的簡化版本。 |
| 擺在 seam 後面的 `ScriptedModel` | [`packages/llm/llm/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/llm/llm/src/index.ts)：`LlmRuntime`、`ctx.llm`（第 284 行） | adapter 透過 `ctx.llm.registerAdapter(providers, adapter)` 註冊，換掉的時候呼叫端不會察覺。stand-in 就是 mini-dsh 的第一個 adapter。 |
| 呼叫 `model(messages)` 的檢查 | [`packages/core/agent-loop/src/agent.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-loop/src/agent.ts) | 真正用它的是 loop：先 `ctx.llm.prepareCall()`，再 `preparedCall.stream(request)`（第 345、449 行）。第 04 章會讓 mini 也有同一個呼叫端。 |
| 先一串 chunk，最後一則訊息 | [`packages/core/session/src/types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/session/src/types.ts)（第 236 行） | 等到 log 出現（第 02 章），串流的這兩個階段就變成 session 事件型別 `assistant/chunk` 和 `assistant/message`。 |

真正的 llm seam 還提供以下功能：

- **一個會做路由的 adapter registry。** `ctx.llm` 同時放著好幾個 adapter，用 provider 名字當鍵；至於某一套部署要拿哪個 model 當預設，本身又是一個 plugin（[`packages/core/agent-default-model`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-default-model)，`ctx.agentDefaultModel`）。mini 這邊一次只有一個 callable，要到第 10 章才會給 seam 一個 service 的位置。
- **串流上可以掛 middleware。** 一道 `llm/stream` waterfall（`index.ts` 第 51 到 60 行）讓 plugin 可以包住或旁觀每一次 model 呼叫，而重試會以 `llm/retry` 這種 session 事件出現在 log 裡。
- **連接不同 provider 的 adapter。** 內建的 [`llm-deepseek`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/llm/llm-deepseek/src/index.ts) 和 [`llm-pi-ai`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/llm/llm-pi-ai/src/index.ts) 會處理各家服務的協定。本教學不實作完整 adapter；唯一接觸實際 API 的地方，是第 04 章之後 `demo.py` 中約 20 行的 Anthropic 格式轉換，而且與離線核心分開。
- **折成一份，而不是拆成三份。** 真正的 dsh 通常會把一個能力拆成三邊：一個套件定義介面，一些套件提供它，一些套件使用它。llm seam 把定義端和使用端折進同一個套件，因為使用它的就是 agent loop 本身，不是一組隨時可以換掉的 tool。第 10 章會把這個 seam 和這條折疊規則一起重現一遍。

---

## 常見失敗模式

- **provider 格式滲入核心。** 如果直接儲存 provider 回傳的 JSON，log、compaction 和 prompt 組裝都會依賴它的欄位與 role 命名。更換 provider 時，這些模組就得一起修改。統一使用 `Message`，可以把格式轉換集中在 adapter 中。
- **可修改的訊息會讓歷史失真。** 第 02、03 章把已寫入的訊息視為既成事實；如果欄位還能任意修改，紀錄與模型實際看到的內容可能逐漸分歧，而且沒有修改痕跡。
- **只回傳完整文字，就無法真正串流。** 模型產生回覆時，呼叫端沒有任何內容可以先顯示，log 也無法記錄 chunk。回覆越長，使用者等待的空窗就越明顯。
- **只有 chunk，會迫使每個呼叫端自行重組完整內容。** loop、log 和監看程式都要各自拼接一次，也可能得到不一致的結果。最後的 `("message", Message)` 讓完整訊息只需在 seam 中組裝一次。
- **用基底類別定義 seam，會增加不必要的耦合。** adapter 必須繼承 harness 的類別，普通函式或包裝另一個模型的閉包也難以直接使用。改用呼叫約定後，只要能 yield 指定事件的 callable 都能成為模型實作。

---

## 動手驗證

[`src/`](src/) 是 Carry-forward 這條鏈的起點，每個檔案都是新的：

- [`message.py`](src/message.py)：凍結的 `Message` dataclass。
- [`standin.py`](src/standin.py)：`ScriptedModel` 與固定規則的分塊函式。
- [`test.py`](src/test.py)：確認所有 chunk 拼接後等於最終訊息、串流確實包含多個區塊，而且預設回覆會依序取用。

```bash
python sections/00-setup/src/test.py   # offline check, no key
```

本章已建立 Model seam，但還沒有機制會主動呼叫它，因此不提供 `demo.py`。第一個實機示範會在第 04 章加入 agent loop 後出現。

---

## 參考資料

- [learn-agent-memory](https://github.com/hardness1020/learn-agent-memory)：本章的檢查慣例（離線、不用 key、每次結果都一樣）就是從這個 tutorial 系列沿用過來的。
