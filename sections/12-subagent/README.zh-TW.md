<!-- source: README.md @ dfc7966 -->

# 12 · Subagent

[English](README.md) | 繁體中文 | [简体中文](README.zh-CN.md)

> parent 叫一個名字來幫忙，拿回來的是一個可以操作它的 handle。名字後面可能是一條執行緒、一個 process，也可能是網路另一頭的另一個產品；契約就是那個 run，至於是誰在跑，不關任何人的事。

section 11 教會 mini-dsh 把工作丟到背景，但所有的思考還是擠在同一個 context window 裡。叫 agent 去跑個腿，摘要一個套件、追一個一直失敗的測試，這趟腿的完整對話紀錄就會永遠跟在 parent 的歷史裡，把它原本要服務的正事擠掉。委派就是出路：把任務交給一個有自己的 session、自己的 tool 作用域、自己的 context 的 child，然後只拿回一個答案。

最直覺的做法是繼承。section 04 的 `Agent` 早就會跑 turn 了，所以 `class Subagent(Agent)` 看起來像是站在起跑線前面。但回答一次委派的，不一定是這個 process 裡的 agent。真正的 dsh 出貨的 Provider 裡，有的會 fork 出新的 process，有的透過一套傳輸協定去驅動另一個產品，有的乾脆包住另一套 harness；要是拿基底類別當契約，這些人全都得假裝自己內部長得像 Agent，只為了掛得進 registry。

所以：為什麼介面要架在「開一個 child、交回一次 run」上面，而不是繼承出一個 agent 子類別？

因為 parent 這一側的契約只有四個動詞，而繼承一個類別，等於把其他所有東西也一起答應下來。這個 Section 這樣把它做出來：

1. 用名字記住 Provider：一個 ctx key 底下一份 registry，每次註冊都交回自己的撤銷動作。
2. Provider 就是一份 callable 的契約：解好的啟動請求進去，一個 run 出來，registry 從來不去看它背後是什麼。
3. 讓 run 就是 parent 這一側契約的全部：`cancel`、`done`、`read_output`，刻意用 section 11 那組協定三元組。
4. 前景模式：這次 tool 呼叫就卡在 `done` 上等，然後把 child 的回覆當答案交出去。
5. 背景模式：把同一組三元組原封不動交給 job registry，於是 subagent 就成了第二個生產者，跟 shell 平起平坐，而 section 11 的控制用 tool 一行新程式碼都不用寫就能服務它。
6. 所有的拒絕都走 section 05 那道門：不認識的名字、沒掛 job registry、child 炸掉，全都變成正常的 `is_error` 結果。

---

## Mechanism

只新增一個檔案 `subagent.py`，前面搬過來的檔案一個都沒動：

- **`SubagentRuntime`**：subagents 這個 service，ctx key 是 `"subagents"`，由 `subagent_plugin` 掛上去。它是一份用名字當鍵的 Provider registry；`start()` 把名字解出來，組好那份請求，再把 Provider 做出來的那個 run 原樣交回去。
- **`SubagentRun`**：parent 這一側的契約，一個凍住的三元組。
- **`in_process_provider(ctx, model_factory)`**：這只是其中一個 Provider，不是契約本身：它用 parent 出身的那幾個 service，建出一個 child `Agent`。
- **`subagent_tools(owner)`**：一個 plugin 工廠，把唯一那個 `subagent` tool 掛進擁有者的作用域，擁有者的身分寫死在裡面。

registry 之所以小，是因為契約本來就小。任何一個能把解好的請求變成一個 run 的 callable，都算是 Provider；它身上沒有一個地方寫著「agent」：

```python
def start(self, name, task):
    """Resolve the name, hand the provider a resolved request, get a run."""
    provider = self._providers.get(name)
    if provider is None:
        raise LookupError(f"no subagent provider registered under '{name}'")
    self._count += 1
    return provider({"id": f"sub-{self._count}", "task": task})
```

而交回來的東西就只有這個 run。它刻意做成 section 11 的協定三元組，因為這個形狀早就回答了 parent 對一件自己已經抓不住的工作可能會問的每一個問題：

```python
@dataclass(frozen=True)
class SubagentRun:
    cancel: callable  # ask the child to stop; cooperative, best effort
    done: callable  # block until it ends: ("completed", None) | ("failed", detail)
    read_output: callable  # the child's answer so far, as text
```

同一個 process 裡的那個 Provider，讓你看清楚契約為什麼可以這麼薄。它用 parent 出身的那幾個 service，`sessions`、`agents`、`tools`，開出一個 child，在這個 run 自己的執行緒上推一次 `send()`，再從 child 的 log 上把答案讀出來。child 的整個故事都留在它自己的 session 裡；唯一跨回來的只有那個 run。就算一個 Provider 背後根本沒有 agent，答案是從快取、從一個子 process、從另一個產品來的，它交回來的還是同一組三元組，registry 和 tool 都分不出差別。

Consumer 把兩種委派模式折進同一個 tool 本體裡，而這個折疊正是第 3 條要求換來的回報：

```python
if mode == "foreground":
    started = subagents.start(name, task)
    status, detail = started.done()
    if status == "failed":
        raise RuntimeError(f"the subagent failed: {detail}")
    return started.read_output() or "(no reply)"
jobs = ctx.get("jobs")  # optional lookup: no registry, no background
if jobs is None:
    raise RuntimeError("no jobs registry mounted; use mode 'foreground'")

def run():
    started = subagents.start(name, task)
    return (started.cancel, started.done, started.read_output)

job_id = jobs.start("subagent", f"{name}: {task}", owner, run)
return f"started {job_id}"
```

前景模式就地等。背景模式把這個 run 包成生產者協定交給 section 11，之後的每一件事都歸它：id、認人、先到先算的定案，還有走 inbox 的完成通知。job registry 是用可有可無的查找拿到的，跟真正的 dsh 一樣，所以一個沒掛 jobs 的 harness 會大聲拒絕背景委派，而不是偷偷把 turn 卡在那裡。

```text
delegation, both ways

subagent {provider, task, mode}
  │  runtime.start(name, task): the name resolves, the provider
  │  establishes whatever it establishes, a run comes back
  │
foreground      done() waited on inside the tool call;
                the result is the child's reply
background      (cancel, done, read_output) handed to jobs;
                the result is a job id, and section 11 owns
                the fence, the settlement, and the notice
```

下面是一次前景委派，兩邊的 log 都在這裡。parent 的對話紀錄裡只留下這趟腿的兩行，一行呼叫、一行答案；跑腿本身是另一個地方的一整個 session：

```text
send("have the worker summarize the log")        the parent, session s1
  │   0  turn/start
  │   2  user/message   "have the worker summarize the log"
  │   5  tool/call      subagent {"provider": "worker",
  │                               "task": "summarize the log",
  │                               "mode": "foreground"}
  │   6  tool/result    "the log has 12 rows"    ◄ one answer crosses back
  │  13  assistant/message "the worker says the log has 12 rows"
  │  15  turn/end

meanwhile, the child, session sub-1: an ordinary transcript

  │   0  turn/start
  │   2  user/message   "summarize the log"      ◄ the task, as its prompt
  │   7  assistant/message "the log has 12 rows"
  │   9  turn/end
```

換成背景模式，同一個 run 改搭 section 11：parent 的 turn 收在 `"started job-1"` 上，child 在 parent 閒著的時候思考，通知再以一個 followup 的 turn 到達，在那裡 `job_output` 給出 child 的回覆，`job_list` 報出來的種類是 `subagent`。控制用的 tool 沒有變，變的是生產者。

### 改了什麼

跟 section 11 比：

- 每一個搬過來的檔案都原封不動：`agent_loop.py`、`capabilities.py`、`inbox.py`、`jobs.py`、`kernel.py`、`message.py`、`scheduler.py`、`session_log.py`、`skills.py`、`standin.py`、`system_prompt.py`、`tools.py`。`subagent.py` 是唯一新增的原始碼檔案，所以拿 11 來 diff，跑出來的就是這個 Section 的 Mechanism，沒有別的。
- 這個 Mechanism 是純粹的組合：child 是透過 section 02 的 sessions、section 04 的 agents、section 05 的 tool 這幾個 service 開出來的；run 就是 section 11 的協定三元組；背景模式把這組三元組交給 job registry，讓 subagent 成為 section 11 早就預告過的第二個生產者。
- log 沒有多出任何新的事件型別。一次委派在 parent 的 log 裡攤在檯面上的一生，就是一行 `tool/call` 加一行 `tool/result`；剩下的故事是它自己那個普通的 session。
- `demo.py`：Live demo 在前景委派給一個對著真 API 跑的 child，把它的答案引述出來，接著再把第二個 child 丟到背景，讓它的完成通知在一個 parent 沒要求過的 turn 裡把 parent 叫醒。

---

## In real dsh

所有連結都指向鎖定的那個 Studied version，[`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca)。這一層對應的套件家族是 [`packages/subagent`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/subagent)。

| Mini-dsh | 真正的 dsh | 說明 |
| --- | --- | --- |
| `SubagentRuntime`，ctx key `"subagents"` | [`packages/subagent/subagent/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/subagent/subagent/src/index.ts)：`SubagentRuntime` | 這個 runtime（第 171 行）是一個具體的 `Service`，跟 section 11 那個抽象的 `JobRegistry` 不一樣：它守的 seam 是 Provider 的介面，不是 registry 本身。 |
| Provider 是一份 callable 的契約 | [`packages/subagent/subagent/src/types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/subagent/subagent/src/types.ts)：`SubagentProvider` | 這個設計問題直接寫在型別系統裡：`SubagentProvider`（第 285 行）是一個 TS 介面，不是 `Service`，也不是 `Agent` 的子類別；任何能把解好的啟動請求變成一個 `SubagentRun` 的東西都算數。 |
| `in_process_provider` | [`packages/subagent/subagent-in-process-driver/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/subagent/subagent-in-process-driver/src/index.ts) | 第 132 行是同一招：child 是用 `parent.ctx.agents.create()` 建出來的，走的是 section 04 那道普通的門，不是什麼私有的建構子。 |
| 背景模式下交給 jobs 的那個 run | [`packages/subagent/subagent/src/run-settlement.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/subagent/subagent/src/run-settlement.ts)、[`packages/subagent/tool-subagent/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/subagent/tool-subagent/src/index.ts)（第 408 到 423 行） | 一次性的背景委派就是 `jobs.start({kind: 'subagent', ...})`：`JobKindMap` 裡的第二個種類，跟 `bash` 平起平坐，正是這個 Section 重建的那次交棒。 |
| `jobs = ctx.get("jobs")`，可有可無的查找 | [`tool-subagent/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/subagent/tool-subagent/src/index.ts)（第 402 到 405 行） | 真正的委派 tool 是用 `ctx.get('jobs')` 拿到 jobs，不是 `inject`：沒掛 registry 就是沒有背景模式，絕不會偷偷退回前景跑。 |
| `subagent` 這個 tool | [`packages/subagent/tool-subagent/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/subagent/tool-subagent/src/index.ts) | 出貨的 Consumer；連它的 tool 名字都可以設定，因為 model 看到的 schema 屬於 Consumer，永遠不屬於 Provider。 |

真正的 subagent 這一層，在這個 Section 的 Mechanism 之上還多做了什麼：

- **可以接著用的 child。** `startContinuable()` 加上一個續接管理器，讓 child 可以跨 turn 活著，parent 在兩個 turn 之間也找得到它。照 [`run-settlement.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/subagent/subagent/src/run-settlement.ts)（第 2 到 4 行），只有一次性的背景模式會碰 jobs；可以接著用的 child 完全不經過 registry。這種 subagent 在這次重建的 Ceiling 之上：只在這裡指給你看，不重建。
- **一整排 Provider。** `subagent-spawn-in-process`、`subagent-fork-in-process`、`subagent-acp`、`subagent-codex`、`subagent-claude-code`、`subagent-dsh-sdk`：選介面而不選繼承這個主張，在這裡變成看得到的東西。其中好幾個背後根本連一個 `Agent` 都沒有，這就是 registry 從來不開口要 Agent 的原因。
- **啟動成功的那一刻，擁有權轉手。** 一次啟動成功之後，child 的擁有權就轉給 parent，所以 parent 一死，它委派出去的東西也跟著陪葬；mini 的 child 則是跟著整個 process 一起生、一起死。
- **更忙的 runtime。** bus 事件（`subagent/provider-added`、`subagent/provider-removed`、`subagent/start`、`subagent/end`，在 runtime 的第 134 到 167 行）、descriptor 快照、找出所有後代的能力，加上三個套件、五個 tool 名字組成的 Consumer 這一面：`subagent` 是這個 Section 做的那個，另外還有給還活著的 child 用的 `send_message`、`interrupt_agent`、`list_agents` 和 `report`。這些全都住在 runtime 和它的 Consumer 裡，所以 Provider 可以一直薄得跟那個介面一樣。

---

## Failure modes

- **拿子類別當契約，等於把 Provider 的名單封頂。** 只要規定一定要是 `Subagent(Agent)`，那每一個 Provider 都得是這個 process 裡的 agent；fork 出去的、遠端的、別的產品的那些 Provider，不是根本不能存在，就是得假扮成 Agent 的內部長相才掛得上去。四個動詞只要求 parent 真正會做的事：開始、停止、等待、讀取。
- **跟 parent 共用 session 的 child，不叫委派。** 把這趟腿的每一行都寫進 parent 的 log，它的完整對話紀錄就會永遠跟在 parent 的 context 裡，而這正是委派要躲掉的那筆代價。兩份 log，一個答案跨過來：parent 留下一次呼叫和一則結果，child 留下其他全部。
- **沒有 cancel 的 run，就是一個沒人叫得停的 child。** 前景至少還能等它跑完；但一個背景的 child，如果三元組裡沒有 `cancel`，`job_kill` 就是在說謊，它把結果定成「killed」，工作卻還在跑。三元組把停止鍵一起帶著，就是為了讓 section 11 的認人背後真的有東西。
- **不認識的名字直接丟例外，會把對話紀錄撕破。** 沒有人註冊過的名字丟出來的 `LookupError`，必須從 section 05 的 pipeline 走出去，變成一則正常的 `is_error` 結果；放它逃出去，model 問的問題就沒有答案，重放也會在同一行斷掉。
- **偷偷退回前景，會讓背景模式變成一句謊話。** 沒掛 job registry 的時候，安靜地就地把任務跑完，等於把 turn 卡住，而且卡的時間剛好就是 model 想避開的那段等待，還連一個可以拿來殺的 id 都沒有。tool 選擇大聲拒絕，而這次拒絕是一則普通的結果，model 可以繞過它另外想辦法。

---

## 跑跑看

[`src/`](src/) 把 11 搬過來，再加上：

- [`subagent.py`](src/subagent.py)（新增）：`SubagentRuntime` 這份 registry、`SubagentRun` 這份契約、同一個 process 裡的那個 Provider，還有 `subagent_tools(owner)` 這個 plugin 工廠，把兩種模式都有的委派 tool 掛上去。
- [`test.py`](src/test.py)：Offline check 證明幾件事：一次前景委派會拿 child 自己 session 裡的回覆當答案；同一個 tool 後面的兩個 Provider 可以互換，就算其中一個根本不是 agent；不認識的名字會變成一則正常的錯誤結果；一次背景委派就是一個普通的 job，它的通知和控制用 tool 一行新程式碼都不用寫；沒掛 job registry 的背景模式會大聲拒絕；child 炸掉也會變成一則正常的錯誤結果。
- [`demo.py`](src/demo.py)：Live demo 委派給一個對著真 API 跑的 child，把它的答案引述出來，再把第二個 child 丟到背景，讓它的完成通知把 parent 叫醒。

```bash
python sections/12-subagent/src/test.py    # offline check, no key
```

Live demo 需要根目錄的 `requirements.txt` 和一把 key；沒有 key 的話，它會安靜地跳過：

```bash
pip install -r requirements.txt         # anthropic + python-dotenv
cp .env.example .env                    # then set ANTHROPIC_API_KEY
python sections/12-subagent/src/demo.py
```

---

## 出處

- [`docs/subsystems/subagent.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/subsystems/subagent.md)：委派這一層的子系統文件：Provider 的介面、runtime，還有 Consumer 那幾個 tool。
- [`packages/subagent/subagent/src/run-settlement.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/subagent/subagent/src/run-settlement.ts)：三種委派模式（前景、一次性背景、可以接著用），還有只有背景模式會碰 jobs 的證據。
