<!-- source: README.md @ d01aaee -->

# 10 · Capability seams

[English](README.md) | 繁體中文 | [简体中文](README.zh-CN.md)

> model 看到的是一個 tool，tool 看到的是一份契約，只有 provider 看得到底下那台機器。把機器換掉，另外兩個房間完全不會知道。

走到第十個 Section，mini-dsh 還是碰不到自己那份 log 以外的任何東西。第一個真正的能力，不管是讀一個檔案還是跑一道指令，總得住在某個地方，而最順手的地方就是 tool 的本體。

但這個位置會把三件事焊死在同一個 function 裡：model 看到什麼、契約長什麼樣、由哪一台機器來兌現。Offline check 要的是記憶體，你自己那台機器要的是磁碟，鎖得很緊的主機要的是一道圍籬；差一種環境就要重寫一次 tool，連 model 拿來規劃的 schema 都跟著晃。

反過來把場面做足也一樣會壞。第一天就給每個能力配一套介面、一個後端套件、一個 tool 套件，harness 就會被一堆只有一種實作、從來沒人換過的抽象淹掉。

所以：一個能力要到什麼時候，才值得拆成三份？

答案是：當有一段程式碼不能知道回答它的是哪一台機器的時候，比如一個面對 model 的 Consumer，或是一個排在後面等著上場的第二個後端。只要這個拆分值得做，seam 就必須做到這幾件事：

1. 每個 seam 只定義一次：一個抽象基底類別、一個 ctx key，加上這個 seam 的詞彙；它只擁有契約，別的什麼都不管。
2. Provider 一律當 plugin 掛上去：一個 key 底下掛一份實作，撤銷的動作掛在 fiber 上；獨佔的 key（fs、shell、sandbox）被掛第二次的時候要當場報錯。
3. 讓 Consumer 看不到 Provider：tool 的本體要到執行的當下才去解那個 key，而且只講抽象基底類別定義的那幾個動詞，所以換掉後端不會改變 model 看到的東西。
4. 把 sandbox 做成一道圍籬，不是一個 tool：只有一個動詞 `confine(argv, policy)`，由別的 seam 的 Provider 來用，遇到不認識的 policy 就拒絕。
5. llm 要折在一起：Definition 和 Consumer 放在同一個 service 裡，adapter 就是符合 Model seam 形狀的普通 callable，用名字分成很多個，每次呼叫才解一次名字。
6. 出事要在 tool 這道門口就降級：沒有 Provider、或是 policy 被拒絕，都變成一則正常的 `is_error` 結果，讓這個 turn 自己好好收尾。

---

## Mechanism

只新增一個檔案 `capabilities.py`，前面搬過來的檔案一個都沒動：

- **Definition**：`FileSystem`（read、write）、`ShellExecutor`（run）、`SandboxProvider`（confine）三個抽象基底類別，各自指名一個 ctx key。抽象基底類別、key、詞彙，這三樣就是這個角色的全部；Definition 不帶任何真的會做事的程式碼。
- **`provider()`**：把 Provider 這個角色做成一個 plugin 工廠。記帳的事 kernel 早就做好了：`provide()` 會交回一個撤銷用的函式，而且拒絕重複的 key，所以獨佔的 seam 不用多寫一行，掛上去的當下就會報錯。
- **`capability_tools_plugin`**：這裡放的是 Consumer。`read`、`write`、`shell` 三個 tool 要到執行的當下才用 `ctx.get()` 去解自己的 seam，而且只講抽象基底類別的動詞；沒有任何一個 tool 去 import Provider。這條 import 的紀律就是 seam 本身。
- **兩個轉折**：sandbox 這個 seam 有 Provider 卻沒有 tool，llm 這個 seam 有 service 卻沒有抽象基底類別。每一個轉折，都是同一個設計問題換另一種方式回答。

先看 sandbox 這個轉折。它唯一的動詞會照指定的 policy 改寫一組 argv，遇到不認識的 policy 就直接拒絕，而不是讓 argv 沒被圍住就過去：

```python
def confine(self, argv, policy):
    if policy not in self._policies:  # fail closed: never run unfenced
        raise ValueError(f"unknown sandbox policy '{policy}'")
    return [SANDBOX_ARGV_MARKER, "--policy", policy, "--", *argv]
```

沒有人會把 `confine` 端到 model 面前。sandbox 的 Consumer 是別的 seam 的 Provider，所以這道圍籬包住的，是 model 早就透過別的 schema 要求過的工作：

```python
class SandboxedShellExecutor(ShellExecutor):
    """Provider built on another seam: run everything through the fence."""

    def run(self, argv):
        return self._inner.run(self._sandbox.confine(argv, self._policy))
```

llm 這個轉折折的是另一個方向。它的 Consumer 就是 agent loop 自己，也就是從 Section 04 開始每個 Agent 都收的那個 `model` 參數，所以另外幫 Consumer 開一個家，只會畫出一條永遠沒人跨過去的界線。而且 Model seam 本身就已經是契約了：一個先串出好幾個 chunk、最後給一則 Message 的普通 callable，根本不需要抽象基底類別。剩下的只有數量這件事：一份用名字記住 adapter 的 registry，加上 `model(name)` 晚一點才解名字，這樣連正在跑的 agent 都換得掉：

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

下面是一次真的執行，log 就是這樣記的。兩個 turn 讀同一個路徑；中間第一個 fs Provider 的撤銷跑掉了，換另一份實作接手同一個 key。agent 完全沒被動過：

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

seam 的證據就在這個對比上：換前換後，log 裡每一行 `request/header` 都一模一樣，只有 `tool/result` 那幾行看得出後端換了。

### 改了什麼

跟 Section 09 比起來：

- 每一個搬過來的檔案都原封不動：`agent_loop.py`、`inbox.py`、`kernel.py`、`message.py`、`scheduler.py`、`session_log.py`、`skills.py`、`standin.py`、`system_prompt.py`、`tools.py`。`capabilities.py` 是唯一新增的原始碼檔案，所以拿 09 來 diff，跑出來的就是這個 Section 的 Mechanism，沒有別的。
- 這個 Mechanism 一樣是純粹的 plugin：Consumer 從 Section 05 的 registry 進來，Provider 從 kernel 的 `provide()` 進來，折起來的 llm 則走 loop 從 Section 04 就一直在收的那個 model 參數。要做這個拆分不用加任何框架，只要守住誰可以 import 誰。
- Model seam 多了一個 service 當家，形狀卻沒變：`llm.model(name)` 還是那個先串 chunk、最後給一則 Message 的普通 callable，所以 `ScriptedModel` 和 `live_model` 一行都不用改就能註冊成 adapter。
- log 沒有多出任何新的事件型別。換後端這件事，只會表現成同樣的 `request/header` 底下，`tool/result` 那幾行不一樣。
- `demo.py`：Live demo 透過 llm runtime 掛上真正的 Anthropic adapter，在兩個 turn 之間換掉 fs 的後端，再讓 model 自己說出 sandbox 替身圍出來的 argv 長什麼樣。

---

## In real dsh

所有指過去的連結都固定在 Studied version [`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca) 上。每個 seam 都是一組套件家族：[`packages/fs`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/fs)、[`packages/shell`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/shell)、[`packages/sandbox`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/sandbox)、[`packages/llm`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/llm)。

| Mini-dsh | 真正的 dsh | 說明 |
| --- | --- | --- |
| `FileSystem` 抽象基底類別，一個 `"fs"` key | [`packages/fs/fs/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/fs/fs/src/index.ts)：`FileSystem` | 真正的 Definition 是 `abstract class FileSystem extends Service`，它擁有 `ctx.fs`（第 86 行）：繼承 `Service` 會把 key 和契約一起帶進來，不會只留下一個光禿禿的介面。 |
| `provider("fs", MemoryFileSystem(...))` | [`packages/fs/fs-local/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/fs/fs-local/src/index.ts)：`LocalFileSystem`、[`packages/fs/fs-sandbox/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/fs/fs-sandbox/src/index.ts)：`SandboxedFileSystem` | 出貨的 Provider。有 sandbox 的那個 fs 會透過 `ctx.sandboxPolicy`（第 127 行）把路徑圍起來，那是 sandbox 的第二個對外介面，這次重建把它折進 `confine` 的 policy 名字裡。 |
| `read`/`write` 這兩個 tool | [`packages/fs/tool-fs/src/read.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/fs/tool-fs/src/read.ts) 和它的鄰居 | 這裡是 Consumer：`read`、`write`、`edit`、`read_image`，另外 `glob` 和 `grep` 放在 `packages/fs` 的別處。沒有任何一份 tool schema 提到後端的名字。 |
| `ShellExecutor`，獨佔掛載 | [`packages/shell/shell/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/shell/shell/src/index.ts)：`ShellExecutor` | `ctx.shell`（第 65 行）在一個 context 裡只准一份實作；註冊第二次就丟例外（第 48 到 50 行）。mini 這邊是 kernel 的 `provide()` 給出同樣的拒絕。 |
| `SandboxedShellExecutor` | [`packages/shell/bash-sandbox/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/shell/bash-sandbox/src/index.ts)：`SandboxBashExecutor` | 它會呼叫 `ctx.sandbox.confine(['bash', '-c', command], policy)`（第 178 行）：一個用到 sandbox seam 的 shell Provider，也就是 mini 那個外面再包一層的做法，只是後面接的是真機器。 |
| `ArgvRewriteSandbox.confine` | [`packages/sandbox/sandbox/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/sandbox/sandbox/src/index.ts)：`SandboxProvider` | `confine(argv, policy)` 是這個 Definition 唯一的抽象方法（第 158 行）；這個 seam 不擁有任何 tool，也不擁有任何事件。 |
| `LlmRuntime` | [`packages/llm/llm/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/llm/llm/src/index.ts)：`LlmRuntime`、`LlmAdapter` | Definition 和 Consumer 折在同一個套件裡：`ctx.llm`（第 284 行）是給 loop 用的，adapter 則繼承 `LlmAdapter`（第 180 行）。像 [`llm-deepseek`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/llm/llm-deepseek/src/index.ts) 這樣的 Provider 透過 `ctx.llm.registerAdapter` 註冊進來。 |

真正的 seam 在這個 Section 的 Mechanism 之上，還多做了這些：

- **真的關得住。** `sandbox-local` 會串起各平台的執行器：linux 上是 `bwrap` 和 `landlock`，darwin 上是 `seatbelt`（第 160 行），還有一個 Windows ACL 的 Provider。那一整套機器就是這次重建的 Ceiling：只會改寫 argv 的替身留住了 seam 的形狀，也留住了出事就關死的規則，但它其實什麼都擋不住；真正的隔離只在這裡指給你看，不重建。
- **事件由 seam 自己擁有。** fs 的 Definition 自己擁有 `fs/write-intent` 和 `fs/edit-intent` 兩個 waterfall，再加一個 `fs/observed` 的 emit，所以在任何 Provider 看到這次寫入之前，plugin 就可以否決它或改寫它；llm 擁有一個給中介層用的 `llm/stream` waterfall。shell 和 sandbox 一個事件都沒有：一個 Definition 對外的樣子，就是它那幾個動詞，加上它自己宣告的那些事件。
- **adapter 的分流。** `registerAdapter(providers, adapter)` 綁的是 model 名字的前綴，runtime 再照每個請求的 model id 去分流。mini 是在建 agent 的時候綁一個名字，每次呼叫才去解；晚綁這件事兩邊一樣，只是拿來分流的鍵小很多。
- **這個拆分是白紙黑字的規定。** dsh 的架構筆記把這個 Section 在問的規則寫死了：能力不預先拆，一個 Provider 配一個 Consumer 就先待在同一個套件裡，等第二個出現再說；`dsh-llm` 是那個長期的例外，因為它的 Consumer 就是 loop。

---

## Failure modes

- **一個去 import Provider 的 tool，等於把 seam 焊死。** 如果 `read` 的本體自己生一個後端出來，或是自己去開磁碟，那換機器就等於改 tool，每一種環境都會分岔出一份自己的 schema。本體每次呼叫都去解 `"fs"`，而且只講抽象基底類別的動詞；那條 import 的紀律就是 seam。
- **安靜掛上去的第二個 Provider，等於出貨一個設定 bug。** 讓兩個 shell 都安靜地掛上去，那到底是哪一台機器在跑這道指令，就取決於一個沒人在讀的掛載順序。獨佔的 key 在掛載的當下就拒絕第二個，比任何一次呼叫挑錯都還早。
- **一個出事就放行的 sandbox，比沒有還糟。** 遇到不認識的 policy 就把 argv 原封不動還回去，那每一次設定錯誤都會在沒有圍籬的情況下跑起來，而且看不見。`confine()` 直接丟例外，Section 05 的 pipeline 回一則正常的 `is_error` 結果，什麼都不會跑。
- **把 sandbox 做成給 model 用的 tool，等於守錯了門。** 把 `confine` 寫進 schema，要不要圍就變成 model 自己決定。sandbox 的 Consumer 是別的 seam 的 Provider：這道圍籬包住的是已經批准過的工作，位置在 schema 底下，沒有人開得了口叫它別圍。
- **提前拆分只是多餘的重量。** 幫 llm 開一個抽象基底類別，可是它的 Consumer 只有一個，而且永遠不會變，那只是多畫一條沒人會跨的界線；adapter 早就以普通 callable 的身分躲在 `model(name)` 後面換來換去了。三份拆分是靠一個不能知道自己 Provider 是誰的 Consumer 換來的，不是靠對稱好看。

---

## 跑跑看

[`src/`](src/) 把 09 搬過來，再加上：

- [`capabilities.py`](src/capabilities.py)（新增）：三個 seam 的抽象基底類別和它們的 Provider（`MemoryFileSystem`、`EchoShellExecutor`、`ArgvRewriteSandbox`、`SandboxedShellExecutor`）、`provider()` 這個 plugin 工廠、折起來的 `LlmRuntime`，還有那幾個 Consumer tool。
- [`test.py`](src/test.py)：Offline check 證明幾件事：在 schema 一模一樣的前提下，換後端會換出不同的結果；獨佔的 seam 會拒絕第二次掛載；sandbox 改寫過的 argv 會透過 shell Provider 一路寫進 log；不認識的 policy 和沒掛 Provider，兩種情況都回正常的錯誤結果；llm 的 adapter 可以用名字並存，而且能在 agent 活著的時候換掉。
- [`demo.py`](src/demo.py)：Live demo 透過 llm runtime 用真正的 model，在兩個 turn 之間換掉 fs 的後端，再讓 model 說出 sandbox 替身圍出來的那串 argv。

```bash
python sections/10-capability-seams/src/test.py    # offline check, no key
```

Live demo 需要根目錄的 `requirements.txt` 和一把 key；沒有 key 的話，它會安靜地跳過：

```bash
pip install -r requirements.txt         # anthropic + python-dotenv
cp .env.example .env                    # then set ANTHROPIC_API_KEY
python sections/10-capability-seams/src/demo.py
```

---

## 出處

- [`docs/glossary.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/glossary.md)：dsh 自己對 Service Definition、Service Provider、Service Consumer 的定義。
- [`.agents/notes/implemented/architecture/2026-06-13-capability-seams.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/.agents/notes/implemented/architecture/2026-06-13-capability-seams.md)：決定了三份拆分和「不預先拆」這條規則的那份架構筆記。
