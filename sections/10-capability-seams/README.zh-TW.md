<!-- source: README.md @ 3705bd7 -->

# 10 · Capability seams

[English](README.md) | 繁體中文 | [简体中文](README.zh-CN.md)

> 如果把能力直接寫進工具實作，模型看到的 schema、能力契約與實際執行環境就會綁在一起。但若一開始就把所有能力拆成三層，又會產生大量沒有替換需求的抽象。只有當使用端不應知道實際由哪個後端執行時，才需要建立 capability seam。

到了第 10 章，Mini-dsh 仍然不會存取 session log 以外的資源。無論是讀取檔案還是執行指令，第一個實際能力都需要一個落點，而最直接的位置就是工具本體。

但這樣會把三個獨立決策寫進同一個函式：模型看到的 schema、對外契約，以及真正執行工作的後端。離線測試可能需要記憶體檔案系統，本機環境需要真實磁碟，受限環境則可能禁止檔案存取。如果為每種環境重寫工具，模型用來規劃的 schema 也會一起變動。

但過早抽象也會讓架構變得笨重。如果每個能力一開始就拆成介面、後端套件和工具套件，harness 會充滿只有一種實作、從未真正被替換的抽象層。

因此，本章要回答：一個能力在什麼時候，才值得拆成 Definition、Provider 和 Consumer 三個角色？

答案是：當某個使用端不應知道實際由哪個後端提供能力時，或當第二種後端已經出現時。一旦決定建立 seam，它必須滿足：

1. 每個 seam 只定義一次：包含抽象基底類別、ctx key 與專用詞彙，只描述契約，不負責實作。
2. Provider 一律以 plugin 掛載：每個 key 只對應一份實作，撤銷動作由 fiber 管理；fs、shell、sandbox 等獨佔 key 若重複掛載，立即報錯。
3. Consumer 不應知道 Provider：工具要到執行時才解析 ctx key，並且只使用抽象基底類別定義的方法。更換後端不會改變模型看到的 schema。
4. sandbox 是執行圍籬，不是模型工具：它只提供 `confine(argv, policy)`，由其他 seam 的 Provider 呼叫；遇到未知 policy 時一律拒絕。
5. llm 要折在一起：Definition 和 Consumer 放在同一個 service 裡，adapter 就是符合 Model seam 形狀的普通 callable，用名字分成很多個，每次呼叫才解一次名字。
6. 錯誤要在工具邊界轉換：找不到 Provider 或 policy 遭拒時，都回傳一般 `is_error` 結果，讓 turn 正常收尾。

---

## 核心機制

只新增一個檔案 `capabilities.py`，前面沿用的檔案都沒有修改：

- **Definition**：`FileSystem`（read、write）、`ShellExecutor`（run）、`SandboxProvider`（confine）三個抽象基底類別，各自指名一個 ctx key。抽象基底類別、key、詞彙，這三樣就是這個角色的全部；Definition 不帶任何真的會做事的程式碼。
- **`provider()`**：將 Provider 包成 plugin factory。kernel 的 `provide()` 會回傳撤銷函式並拒絕重複 key，因此獨佔 seam 不需要額外管理生命週期或重複掛載檢查。
- **`capability_tools_plugin`**：這裡放的是 Consumer。`read`、`write`、`shell` 三個 tool 要到執行的當下才用 `ctx.get()` 去解自己的 seam，而且只講抽象基底類別的動詞；沒有任何一個 tool 去 import Provider。這條 import 的紀律就是 seam 本身。
- **兩個轉折**：sandbox 這個 seam 有 Provider 卻沒有 tool，llm 這個 seam 有 service 卻沒有抽象基底類別。每一個轉折，都是同一個設計問題換另一種方式回答。

先看 sandbox 這個轉折。它唯一的動詞會照指定的 policy 改寫一組 argv，遇到不認識的 policy 就直接拒絕，而不是讓 argv 沒被圍住就過去：

```python
def confine(self, argv, policy):
    if policy not in self._policies:  # fail closed: never run unfenced
        raise ValueError(f"unknown sandbox policy '{policy}'")
    return [SANDBOX_ARGV_MARKER, "--policy", policy, "--", *argv]
```

`confine` 不會出現在模型可見的工具中。sandbox 的 Consumer 是其他 seam 的 Provider，它負責限制模型已透過其他 schema 核准的工作：

```python
class SandboxedShellExecutor(ShellExecutor):
    """Provider built on another seam: run everything through the fence."""

    def run(self, argv):
        return self._inner.run(self._sandbox.confine(argv, self._policy))
```

llm 這個轉折折的是另一個方向。它的 Consumer 就是 agent loop 自己，也就是從第 04 章開始每個 Agent 都收的那個 `model` 參數，所以另外幫 Consumer 開一個家，只會畫出一條永遠沒人跨過去的界線。而且 Model seam 本身就已經是契約了：一個先串出好幾個 chunk、最後給一則 Message 的普通 callable，根本不需要抽象基底類別。剩下的只有數量這件事：一份用名字記住 adapter 的 registry，加上 `model(name)` 晚一點才解名字，這樣連正在跑的 agent 都換得掉：

```python
def model(self, name):
    """The Model seam bound to an adapter name, resolved per call."""

    def seam(messages, tools=(), system=""):
        adapter = self._adapters.get(name)
        if adapter is None:
            raise LookupError(f"no llm adapter registered under '{name}'")
        return adapter(messages, tools, system)

    return seam
```

```text
the three roles, one seam (fs)

Definition   FileSystem ABC: read, write; one ctx key "fs"
Provider     provide("fs", MemoryFileSystem({...}))   undo on the fiber;
                                                      a second mount raises
Consumer     read/write tools: ctx.get("fs") per call, the ABC's verbs only

the sandbox bend: consumed by a provider, never by a tool

shell tool ──► ctx.get("shell").run(["echo", "hi"])
                 SandboxedShellExecutor              a shell provider,
                   │ confine(["echo", "hi"], ...)    consuming the sandbox seam
                   │  ├─ known policy: prepend the fence marker
                   │  └─ unknown policy: raise; fail closed, nothing runs
                 EchoShellExecutor.run(fenced argv)  the inner provider
tool/result   "mini-sandbox --policy read-only -- echo hi"
```

以下是實際執行時的 log。兩個 turn 讀取同一路徑；中間卸載第一個 fs Provider，再由另一份實作接手相同 key。agent 本身完全不需修改：

```text
send("read it")                 provide("fs", A), notes.txt = "alpha"
  │   0  turn/start
  │   1  step/start
  │   2  user/message   "read it"
  │   3  request/header tools [read, write, shell]
  │   4  assistant/message {"tool_calls": [read "notes.txt"]}
  │   5  tool/call      read {"path": "notes.txt"}
  │   6  tool/result    "alpha"                  ◄ the machine's answer
  │   7  step/end       {"reason": null}
  │   8  step/start
  │   9  request/header tools [read, write, shell]
  │  10  assistant/chunk "do"
  │  11  assistant/chunk "ne"
  │  12  assistant/message "done"
  │  13  step/end       {"reason": "completed"}
  │  14  turn/end

A's undo runs; provide("fs", B), notes.txt = "beta"

send("read it again")
  │  15  turn/start
  │  ...
  │  18  request/header tools [read, write, shell] ◄ byte-identical offer,
  │  ...                                             same system text
  │  21  tool/result    "beta"                     ◄ only the machine changed
  │  ...
  │  29  turn/end
```

這個對比正好說明 seam 的作用：更換前後，log 中的 `request/header` 完全相同，只有 `tool/result` 反映後端差異。

### 改了什麼

與第 09 章相比：

- 所有既有檔案都完整沿用：`agent_loop.py`、`inbox.py`、`kernel.py`、`message.py`、`scheduler.py`、`session_log.py`、`skills.py`、`standin.py`、`system_prompt.py`、`tools.py`。`capabilities.py` 是唯一新增的原始碼檔案，因此與第 09 章相比，diff 只包含本章新增的機制，不包含其他改動。
- 這項機制一樣是純粹的 plugin：Consumer 從第 05 章的 registry 進來，Provider 從 kernel 的 `provide()` 進來，折起來的 llm 則走 loop 從第 04 章就一直在收的那個 model 參數。要做這個拆分不用加任何框架，只要守住誰可以 import 誰。
- Model seam 多了一個 service 當家，形狀卻沒變：`llm.model(name)` 還是那個先串 chunk、最後給一則 Message 的普通 callable，所以 `ScriptedModel` 和 `live_model` 一行都不用改就能註冊成 adapter。
- log 沒有多出任何新的事件型別。換後端這件事，只會表現成同樣的 `request/header` 底下，`tool/result` 那幾行不一樣。
- `demo.py`：實機示範透過 llm runtime 掛上真正的 Anthropic adapter，在兩個 turn 之間換掉 fs 的後端，再讓 model 自己說出 sandbox 替身圍出來的 argv 長什麼樣。

---

## 對照真正的 dsh

以下連結皆指向研究版本 [`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca)。每個 seam 都是一組套件家族：[`packages/fs`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/fs)、[`packages/shell`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/shell)、[`packages/sandbox`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/sandbox)、[`packages/llm`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/llm)。

| Mini-dsh | 真正的 dsh | 說明 |
| --- | --- | --- |
| `FileSystem` 抽象基底類別，一個 `"fs"` key | [`packages/fs/fs/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/fs/fs/src/index.ts)：`FileSystem` | 真正的 Definition 是 `abstract class FileSystem extends Service`，它擁有 `ctx.fs`（第 86 行）：繼承 `Service` 會把 key 和契約一起帶進來，不會只留下一個光禿禿的介面。 |
| `provider("fs", MemoryFileSystem(...))` | [`packages/fs/fs-local/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/fs/fs-local/src/index.ts)：`LocalFileSystem`、[`packages/fs/fs-sandbox/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/fs/fs-sandbox/src/index.ts)：`SandboxedFileSystem` | 出貨的 Provider。有 sandbox 的那個 fs 會透過 `ctx.sandboxPolicy`（第 127 行）把路徑圍起來，那是 sandbox 的第二個對外介面，Mini-dsh 把它折進 `confine` 的 policy 名字裡。 |
| `read`/`write` 這兩個 tool | [`packages/fs/tool-fs/src/read.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/fs/tool-fs/src/read.ts) 和它的鄰居 | 這裡是 Consumer：`read`、`write`、`edit`、`read_image`，另外 `glob` 和 `grep` 放在 `packages/fs` 的別處。沒有任何一份 tool schema 提到後端的名字。 |
| `ShellExecutor`，獨佔掛載 | [`packages/shell/shell/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/shell/shell/src/index.ts)：`ShellExecutor` | `ctx.shell`（第 65 行）在一個 context 裡只准一份實作；註冊第二次就丟例外（第 48 到 50 行）。mini 這邊是 kernel 的 `provide()` 給出同樣的拒絕。 |
| `SandboxedShellExecutor` | [`packages/shell/bash-sandbox/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/shell/bash-sandbox/src/index.ts)：`SandboxBashExecutor` | 它會呼叫 `ctx.sandbox.confine(['bash', '-c', command], policy)`（第 178 行）：一個用到 sandbox seam 的 shell Provider，也就是 mini 那個外面再包一層的做法，只是後面接的是真機器。 |
| `ArgvRewriteSandbox.confine` | [`packages/sandbox/sandbox/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/sandbox/sandbox/src/index.ts)：`SandboxProvider` | `confine(argv, policy)` 是這個 Definition 唯一的抽象方法（第 158 行）；這個 seam 不擁有任何 tool，也不擁有任何事件。 |
| `LlmRuntime` | [`packages/llm/llm/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/llm/llm/src/index.ts)：`LlmRuntime`、`LlmAdapter` | Definition 和 Consumer 折在同一個套件裡：`ctx.llm`（第 284 行）是給 loop 用的，adapter 則繼承 `LlmAdapter`（第 180 行）。像 [`llm-deepseek`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/llm/llm-deepseek/src/index.ts) 這樣的 Provider 透過 `ctx.llm.registerAdapter` 註冊進來。 |

真正的 seam 還提供以下功能：

- **真正的隔離機制。** `sandbox-local` 會串接各平台執行器：Linux 使用 `bwrap` 與 `landlock`，Darwin 使用 `seatbelt`（第 160 行），另有 Windows ACL Provider。Mini-dsh 只用 argv 改寫保留 seam 形狀與 fail-closed 規則，並沒有實作真正的隔離。
- **事件由 seam 自己擁有。** fs 的 Definition 自己擁有 `fs/write-intent` 和 `fs/edit-intent` 兩個 waterfall，再加一個 `fs/observed` 的 emit，所以在任何 Provider 看到這次寫入之前，plugin 就可以否決它或改寫它；llm 擁有一個給中介層用的 `llm/stream` waterfall。shell 和 sandbox 一個事件都沒有：一個 Definition 對外的樣子，就是它那幾個動詞，加上它自己宣告的那些事件。
- **adapter 的分流。** `registerAdapter(providers, adapter)` 綁的是 model 名字的前綴，runtime 再照每個請求的 model id 去分流。mini 是在建 agent 的時候綁一個名字，每次呼叫才去解；晚綁這件事兩邊一樣，只是拿來分流的鍵小很多。
- **架構筆記明確定義拆分時機。** dsh 不會預先拆分能力；只有一個 Provider 與一個 Consumer 時先放在同一套件，等第二種實作出現再建立 seam。`dsh-llm` 是長期例外，因為它的 Consumer 就是 loop。

---

## 常見失敗模式

- **工具直接 import Provider 會讓 seam 失去意義。** 如果 `read` 自行建立後端或直接讀取磁碟，更換環境就必須修改工具，也可能產生不同 schema。正確做法是每次呼叫都解析 `"fs"`，並只使用抽象契約中的方法。
- **獨佔 Provider 重複掛載若未報錯，行為就取決於掛載順序。** 兩個 shell 同時存在時，系統無法明確判斷哪個負責執行。獨佔 key 應在第二次掛載時立即拒絕，提早暴露設定錯誤。
- **sandbox 遇到錯誤時放行，比完全沒有隔離更危險。** 未知 policy 若直接回傳原 argv，設定錯誤就會在沒有隔離的情況下執行。`confine()` 應直接拋錯，再由工具 pipeline 轉成 `is_error`，確保指令不會執行。
- **將 sandbox 暴露成模型工具，會把隔離決策交給模型。** `confine` 不應出現在 schema 中；它應位於 Provider 內部，強制套用在已核准的工作上，模型無法選擇略過。
- **提前拆分只是多餘的重量。** 幫 llm 開一個抽象基底類別，可是它的 Consumer 只有一個，而且永遠不會變，那只是多畫一條沒人會跨的界線；adapter 早就以普通 callable 的身分躲在 `model(name)` 後面換來換去了。三份拆分是靠一個不能知道自己 Provider 是誰的 Consumer 換來的，不是靠對稱好看。

---

## 動手驗證

[`src/`](src/) 延續第 09 章，並加入：

- [`capabilities.py`](src/capabilities.py)（新增）：三個 seam 的抽象基底類別和它們的 Provider（`MemoryFileSystem`、`EchoShellExecutor`、`ArgvRewriteSandbox`、`SandboxedShellExecutor`）、`provider()` 這個 plugin 工廠、折起來的 `LlmRuntime`，還有那幾個 Consumer tool。
- [`test.py`](src/test.py)：離線測試證明幾件事：在 schema 一模一樣的前提下，換後端會換出不同的結果；獨佔的 seam 會拒絕第二次掛載；sandbox 改寫過的 argv 會透過 shell Provider 一路寫進 log；不認識的 policy 和沒掛 Provider，兩種情況都回正常的錯誤結果；llm 的 adapter 可以用名字並存，而且能在 agent 活著的時候換掉。
- [`demo.py`](src/demo.py)：實機示範透過 llm runtime 用真正的 model，在兩個 turn 之間換掉 fs 的後端，再讓 model 說出 sandbox 替身圍出來的那串 argv。

```bash
python sections/10-capability-seams/src/test.py    # offline check, no key
```

實機示範需要根目錄的 `requirements.txt` 和一把 key；沒有設定 key 時會自動跳過：

```bash
pip install -r requirements.txt         # anthropic + python-dotenv
cp .env.example .env                    # then set ANTHROPIC_API_KEY
python sections/10-capability-seams/src/demo.py
```

---

## 參考資料

- [`docs/glossary.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/glossary.md)：dsh 自己對 Service Definition、Service Provider、Service Consumer 的定義。
- [`.agents/notes/implemented/architecture/2026-06-13-capability-seams.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/.agents/notes/implemented/architecture/2026-06-13-capability-seams.md)：決定了三份拆分和「不預先拆」這條規則的那份架構筆記。
