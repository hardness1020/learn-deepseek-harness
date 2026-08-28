<!-- source: README.md @ 3705bd7 -->

# 12 · Subagent

[English](README.md) | 繁體中文 | [简体中文](README.zh-CN.md)

> 被委派的支線任務不應該佔滿 parent 的 context。如果用 Agent 子類別代表 subagent，就假設實際執行者一定在相同 process 內，但真實 provider 可能來自另一個 process、遠端服務或其他 harness。因此 parent 只依名稱啟動 provider，並取得統一的 run 介面。

第 11 章已經讓 Mini-dsh 可以執行背景工作，但所有推理仍共用同一個 context window。如果 parent 負責摘要套件或追查失敗測試，這些支線任務的完整對話都會留在 parent 歷史中，擠壓主任務可用的 context。委派的做法是為 child 建立獨立 session、工具作用域與 context，parent 最後只接收一個結果。

由於第 04 章的 `Agent` 已能執行 turn，直覺做法是建立 `class Subagent(Agent)`。但委派的實際執行者不一定是相同 process 中的 agent。真正 dsh 的 provider 可能 fork 新 process、透過傳輸協定驅動其他產品，或包裝另一套 harness。如果用基底類別定義契約，這些 provider 都必須假裝擁有 `Agent` 的內部結構，才能加入 registry。

因此，本章要回答的問題是：為什麼 subagent 介面應定義為「啟動 child，回傳一個 run」，而不是繼承 `Agent`？

parent 真正需要的契約非常小，而繼承會同時帶入大量不必要的假設。本章依下列原則實作：

1. 依名稱註冊 Provider：同一個 ctx key 下維護 registry，每次註冊都回傳撤銷動作。
2. Provider 只需符合 callable 契約：接收已解析的啟動請求並回傳 run；registry 不假設背後如何執行。
3. run 是 parent 需要的完整介面，只包含 `cancel`、`done`、`read_output`，沿用第 11 章的協定三元組。
4. 前景模式會在工具呼叫內等待 `done`，再將 child 回覆作為結果。
5. 背景模式將同一個三元組交給 job registry，讓 subagent 成為與 shell 對等的生產者，並直接共用第 11 章的控制工具。
6. 所有失敗都經過第 05 章的工具 pipeline：未知名稱、缺少 job registry 或 child 執行失敗，都會轉成一般 `is_error` 結果。

---

## 核心機制

只新增一個檔案 `subagent.py`，前面沿用的檔案都沒有修改：

- **`SubagentRuntime`**：ctx key 為 `"subagents"` 的 service，由 `subagent_plugin` 掛載。它以名稱管理 Provider；`start()` 解析名稱、組合請求，再直接回傳 Provider 建立的 run。
- **`SubagentRun`**：parent 這一側的契約，一個不可變的三元組。
- **`in_process_provider(ctx, model_factory)`**：這只是其中一個 Provider，不是契約本身。它使用 parent 可用的 service 建立 child `Agent`。
- **`subagent_tools(owner)`**：一個 plugin 工廠，把唯一那個 `subagent` tool 掛進擁有者的作用域，擁有者的身分寫死在裡面。

registry 可以保持精簡，是因為契約本身只要求：任何 callable 只要能將已解析請求轉成 run，就能作為 Provider，完全不需要繼承 Agent：

```python
def start(self, name, task):
    """Resolve the name, hand the provider a resolved request, get a run."""
    provider = self._providers.get(name)
    if provider is None:
        raise LookupError(f"no subagent provider registered under '{name}'")
    self._count += 1
    return provider({"id": f"sub-{self._count}", "task": task})
```

回傳值只有這個 run，並刻意沿用第 11 章的協定三元組，完整涵蓋 parent 對外部工作的三項需求：取消、等待完成，以及讀取輸出。

```python
@dataclass(frozen=True)
class SubagentRun:
    cancel: callable  # ask the child to stop; cooperative, best effort
    done: callable  # block until it ends: ("completed", None) | ("failed", detail)
    read_output: callable  # the child's answer so far, as text
```

同一個 process 內的 Provider 能說明為何契約可以這麼薄。它使用 parent 可用的 `sessions`、`agents`、`tools` service 建立 child，在 run 專屬執行緒中呼叫 `send()`，再從 child log 讀取答案。完整流程保留在 child 自己的 session，parent 只接觸 run。即使其他 Provider 從快取、子 process 或另一套產品取得結果，只要回傳同一組三元組，registry 與工具就不需要區分來源。

Consumer 透過同一個工具支援前景與背景兩種委派模式，兩者共用完全相同的 run 介面：

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

前景模式直接等待完成；背景模式則將 run 包成生產者協定交給第 11 章，後續 id、擁有者驗證、最終狀態與 inbox 通知都由 job registry 管理。job registry 透過可選查詢取得，因此未掛載 jobs 的 harness 會明確拒絕背景委派，不會無聲改成阻塞的前景模式。

```text
delegation, both ways

subagent {provider, task, mode}
  │  runtime.start(name, task): the name resolves, the provider
  │  establishes whatever it establishes, a run comes back
  │
foreground      done() waited on inside the tool call;
                the result is the child's reply
background      (cancel, done, read_output) handed to jobs;
                the result is a job id, and Section 11 owns
                the fence, the settlement, and the notice
```

以下是一次前景委派，以及 parent 與 child 各自的 log。parent 只保留一筆呼叫和一筆結果，child 的完整執行過程則留在自己的 session：

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

換成背景模式，同一個 run 改搭第 11 章：parent 的 turn 收在 `"started job-1"` 上，child 在 parent 閒著的時候思考，通知再以一個 followup 的 turn 到達，在那裡 `job_output` 給出 child 的回覆，`job_list` 報出來的種類是 `subagent`。控制用的 tool 沒有變，變的是生產者。

### 改了什麼

與第 11 章相比：

- 所有既有檔案都完整沿用：`agent_loop.py`、`capabilities.py`、`inbox.py`、`jobs.py`、`kernel.py`、`message.py`、`scheduler.py`、`session_log.py`、`skills.py`、`standin.py`、`system_prompt.py`、`tools.py`。`subagent.py` 是唯一新增的原始碼檔案，因此與第 11 章相比，diff 只包含本章新增的機制，不包含其他改動。
- 這項機制是純粹的組合：child 是透過第 02 章的 sessions、第 04 章的 agents、第 05 章的 tool 這幾個 service 開出來的；run 就是第 11 章的協定三元組；背景模式把這組三元組交給 job registry，讓 subagent 成為第 11 章早就預告過的第二個生產者。
- log 沒有新增任何事件型別。一次委派在 parent log 中只包含一筆 `tool/call` 與一筆 `tool/result`；其餘執行紀錄都保存在 child 自己的 session。
- `demo.py`：實機示範在前景委派給一個對著實際 API 跑的 child，把它的答案引述出來，接著再把第二個 child 丟到背景，讓它的完成通知在一個 parent 沒要求過的 turn 裡把 parent 叫醒。

---

## 對照真正的 dsh

以下連結皆指向研究版本 [`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca)。這一層對應的套件家族是 [`packages/subagent`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/subagent)。

| Mini-dsh | 真正的 dsh | 說明 |
| --- | --- | --- |
| `SubagentRuntime`，ctx key `"subagents"` | [`packages/subagent/subagent/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/subagent/subagent/src/index.ts)：`SubagentRuntime` | 這個 runtime（第 171 行）是一個具體的 `Service`，與第 11 章那個抽象的 `JobRegistry` 不一樣：它守的 seam 是 Provider 的介面，不是 registry 本身。 |
| Provider 是一份 callable 的契約 | [`packages/subagent/subagent/src/types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/subagent/subagent/src/types.ts)：`SubagentProvider` | 這個設計問題直接寫在型別系統裡：`SubagentProvider`（第 285 行）是一個 TS 介面，不是 `Service`，也不是 `Agent` 的子類別；任何能把解好的啟動請求變成一個 `SubagentRun` 的東西都算數。 |
| `in_process_provider` | [`packages/subagent/subagent-in-process-driver/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/subagent/subagent-in-process-driver/src/index.ts) | 第 132 行是同一招：child 是用 `parent.ctx.agents.create()` 建出來的，走的是第 04 章那道普通的門，不是什麼私有的建構子。 |
| 背景模式下交給 jobs 的那個 run | [`packages/subagent/subagent/src/run-settlement.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/subagent/subagent/src/run-settlement.ts)、[`packages/subagent/tool-subagent/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/subagent/tool-subagent/src/index.ts)（第 408 到 423 行） | 一次性的背景委派就是 `jobs.start({kind: 'subagent', ...})`：`JobKindMap` 裡的第二個種類，跟 `bash` 平起平坐，正是本章重建的那次交棒。 |
| `jobs = ctx.get("jobs")`，可有可無的查找 | [`tool-subagent/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/subagent/tool-subagent/src/index.ts)（第 402 到 405 行） | 真正的委派 tool 是用 `ctx.get('jobs')` 拿到 jobs，不是 `inject`：沒掛 registry 就是沒有背景模式，絕不會偷偷退回前景跑。 |
| `subagent` 這個 tool | [`packages/subagent/tool-subagent/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/subagent/tool-subagent/src/index.ts) | 出貨的 Consumer；連它的 tool 名字都可以設定，因為 model 看到的 schema 屬於 Consumer，永遠不屬於 Provider。 |

真正的 subagent 這一層還提供以下功能：

- **可以接著用的 child。** `startContinuable()` 加上一個續接管理器，讓 child 可以跨 turn 活著，parent 在兩個 turn 之間也找得到它。照 [`run-settlement.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/subagent/subagent/src/run-settlement.ts)（第 2 到 4 行），只有一次性的背景模式會碰 jobs；可以接著用的 child 完全不經過 registry。這種 subagent 在 Mini-dsh 的實作範圍之外：只在這裡指給你看，沒有做。
- **多種 Provider 實作。** `subagent-spawn-in-process`、`subagent-fork-in-process`、`subagent-acp`、`subagent-codex`、`subagent-claude-code`、`subagent-dsh-sdk` 展示了介面優於繼承的好處，其中部分實作背後甚至沒有 `Agent`，因此 registry 不應要求 Agent 類型。
- **啟動成功後，擁有權轉交給 parent。** parent 結束時，它啟動的 child 也會一併終止；Mini-dsh 的簡化版則讓 child 與整個 process 共用生命週期。
- **更忙的 runtime。** bus 事件（`subagent/provider-added`、`subagent/provider-removed`、`subagent/start`、`subagent/end`，在 runtime 的第 134 到 167 行）、descriptor 快照、找出所有後代的能力，加上三個套件、五個 tool 名字組成的 Consumer 這一面：`subagent` 是本章實作的那個，另外還有給還活著的 child 用的 `send_message`、`interrupt_agent`、`list_agents` 和 `report`。這些全都位於 runtime 和它的 Consumer 裡，所以 Provider 可以一直薄得跟那個介面一樣。

---

## 常見失敗模式

- **以子類別作為契約會限制 Provider 類型。** 若規定必須是 `Subagent(Agent)`，fork、遠端服務或其他產品都得模擬本地 Agent 內部結構。run 介面只要求 parent 真正需要的開始、停止、等待與讀取能力。
- **child 與 parent 共用 session 會失去委派的意義。** child 的完整對話若都寫入 parent log，就會持續佔用 parent context。兩者應使用獨立 log，只有最終答案跨回 parent。
- **缺少 cancel 的 run 無法真正支援背景取消。** `job_kill` 即使將狀態設為 `killed`，實際工作仍可能繼續執行。三元組必須包含停止方法，讓狀態與實際執行一致。
- **未知名稱的例外若穿過工具邊界，會留下不完整呼叫。** `LookupError` 必須由第 05 章的 pipeline 轉成 `is_error` 結果，確保模型收到對應回覆，重放也能繼續。
- **缺少 job registry 時不能無聲退回前景模式。** 否則背景請求會意外阻塞 turn，也沒有可取消的 id。工具應明確回傳錯誤，讓模型決定其他做法。

---

## 動手驗證

[`src/`](src/) 延續第 11 章，並加入：

- [`subagent.py`](src/subagent.py)（新增）：`SubagentRuntime` 這份 registry、`SubagentRun` 這份契約、同一個 process 裡的那個 Provider，還有 `subagent_tools(owner)` 這個 plugin 工廠，把兩種模式都有的委派 tool 掛上去。
- [`test.py`](src/test.py)：離線測試證明幾件事：一次前景委派會拿 child 自己 session 裡的回覆當答案；同一個 tool 後面的兩個 Provider 可以互換，就算其中一個根本不是 agent；不認識的名字會變成一則正常的錯誤結果；一次背景委派就是一個普通的 job，它的通知和控制用 tool 一行新程式碼都不用寫；沒掛 job registry 的背景模式會大聲拒絕；child 炸掉也會變成一則正常的錯誤結果。
- [`demo.py`](src/demo.py)：實機示範委派給一個對著實際 API 跑的 child，把它的答案引述出來，再把第二個 child 丟到背景，讓它的完成通知把 parent 叫醒。

```bash
python sections/12-subagent/src/test.py    # offline check, no key
```

實機示範需要根目錄的 `requirements.txt` 和一把 key；沒有設定 key 時會自動跳過：

```bash
pip install -r requirements.txt         # anthropic + python-dotenv
cp .env.example .env                    # then set ANTHROPIC_API_KEY
python sections/12-subagent/src/demo.py
```

---

## 參考資料

- [`docs/subsystems/subagent.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/subsystems/subagent.md)：委派這一層的子系統文件：Provider 的介面、runtime，還有 Consumer 那幾個 tool。
- [`packages/subagent/subagent/src/run-settlement.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/subagent/subagent/src/run-settlement.ts)：三種委派模式（前景、一次性背景、可以接著用），還有只有背景模式會碰 jobs 的證據。
