<!-- source: README.md @ 3705bd7 -->

# 11 · Jobs

[English](README.md) | 繁體中文 | [简体中文](README.zh-CN.md)

> 長時間執行的指令不應阻塞整個 turn。但背景工作如果沒有明確擁有者，就沒有安全的讀取、等待與取消邊界。當 job id 公開後，所有控制操作都必須由擁有者權限保護。

到了第 11 章，Mini-dsh 啟動的所有工作仍然與當前 turn 綁在一起。第 06 章的 scheduler 保證，已開始的工作一定會完成，而每次呼叫都必須在 step 結束前產生結果。如果 shell seam 執行的指令需要很久，整個 turn、inbox 與使用者都只能原地等待。

一個直覺做法，是讓工具啟動 worker thread 後立即回傳。但這樣只是把工作丟到背景，沒有解決歸屬問題。turn 的中止訊號已經失去作用，輸出沒有統一查詢入口，任何 session 只要猜到 id，就可能讀取或取消他人的工作。

因此，本章要回答的問題是：job id 一旦公開，誰擁有讀取、等待和取消它的權限？

這些權責都歸 job registry，而 registry 只接受擁有者的操作。工作啟動後，必須完整交接：

1. 立即公開 id：工作會在自己的執行緒中執行，`start()` 立刻回傳 job id；啟動它的工具呼叫也只需回傳這個 id。
2. 完整接管執行協定：生產者的 `run()` 回傳 `(cancel, done, read_output)` 後，id、快照、最終狀態與通知都由 registry 管理。
3. 每個入口都驗證擁有者：read、kill、list 只接受 job 所屬 session 的操作。呼叫者身分在工具掛載時由環境綁定，不會成為模型可控制的參數。
4. 最終狀態只決定一次：`completed`、`failed`、`killed` 中，先發生者生效，之後不再改變。
5. 通知一律透過 inbox：`wakeup` job 在擁有者閒置時使用 followup，忙碌時使用 inject；`quiet` job 則等待查詢。背景執行緒不會直接寫入 log。
6. 控制工具只實作一次：`job_output`、`job_kill`、`job_list` 可共用於所有生產者。

---

## 核心機制

只新增一個檔案 `jobs.py`，前面沿用的檔案都沒有修改：

- **`JobRegistry`**：ctx key 為 `"jobs"` 的 service，由 `jobs_plugin` 掛載。它管理 id、擁有者驗證、快照與最終狀態；每個 job 都有 watcher 執行緒等待完成，因此即使沒有人查詢，結果仍會被記錄。
- **`JobOwner`**：這個 seam 的詞彙：拿來認人的身分，加上通知要送進哪個 agent 的 inbox。
- **`job_tools(owner)`**：一個 plugin 工廠，把一個生產者（`shell_job`，它在自己的執行緒上透過第 10 章的 shell seam 跑指令）和三個控制用的 tool，一起掛進擁有者的 tool 作用域，而且擁有者的身分是寫死在裡面的。

核心在於明確交接。生產者將 `run()` 傳給 `start()`；`run()` 啟動工作並回傳協定三元組，而生產者只取得一個 id：

```python
def start(self, kind, label, owner, run, delivery="wakeup"):
    cancel, done, read_output = run()
    with self._lock:
        self._count += 1
        job = Job(
            f"job-{self._count}", kind, label, owner, delivery, cancel, read_output
        )
        self._jobs[job.id] = job
    threading.Thread(
        target=lambda: self._settle(job, *done()), ...
    ).start()
    return job.id
```

`start()` 回傳後，取消權便交由 registry 管理。啟動工作的 turn 之後無論正常結束或被取消，都不會影響已公開的 job。要停止它只能使用 `job_kill`，而所有操作都會先驗證擁有者：

```python
def _fenced(self, job_id, caller_id):
    with self._lock:
        job = self._jobs.get(job_id)
    if job is None or job.owner.id != caller_id:
        # One message for a foreign id and a bogus one: a stranger
        # learns nothing, not even that the id exists.
        raise PermissionError(f"no job '{job_id}' owned by this session")
    return job
```

`caller_id` 不由模型提供。`job_tools(owner)` 在工具掛入擁有者作用域時就綁定身分，因此 agent B 即使取得 A 的 job id，registry 看到的呼叫者仍是 B，並會視該 job 為不存在。擁有者檢查拋出的例外，會由第 05 章的 pipeline 轉成一般 `is_error` 結果。

每個 job 的最終狀態只會設定一次。工作完成時，watcher 會設定為 `completed` 或 `failed`；`kill` 則嘗試設定為 `killed`。哪一方先成功，結果就固定不再改變：

```python
def _settle(self, job, status, detail=None):
    with self._lock:
        if job.outcome is not None:
            return  # the race already settled; a later voice changes nothing
        job.outcome = {"status": status, "detail": detail}
    self._notify(job)  # outside the lock: delivery may drive a whole turn
```

定案的那一刻，也是擁有者知道這件事的那一刻，而這則通知走的是第 07 章的 inbox，絕不直接寫進 log：

```python
if agent.status == "idle":
    agent.followup(notice)  # idle: the notice opens a turn of its own
else:
    agent.inject(notice)  # busy: park it for the next step boundary
```

```text
the handoff, in time

producing call      shell_job body: run() starts the thread,
                    jobs.start() publishes "job-1"
                      │ the call's abort signal stops mattering here
turn ends           the work is still running; nobody waits
                      │
settlement          first of: watcher (completed | failed), kill (killed)
delivery            wakeup + idle owner  ──► followup(): a turn of its own
                    wakeup + busy owner  ──► inject(): next step boundary
                    quiet                ──► nothing; poll job_output
```

以下是實際執行時的 log。啟動工作的 turn 只取得 job id；工作在 agent 閒置時完成後，通知會主動開啟另一個 turn：

```text
send("run echo hi in the background")     the gate holds the work open
  │   0  turn/start
  │   2  user/message   "run echo hi in the background"
  │   3  request/header tools [shell_job, job_output, job_kill, job_list]
  │   5  tool/call      shell_job {"command": "echo hi", "delivery": "wakeup"}
  │   6  tool/result    "started job-1"     ◄ the whole answer: an id
  │   8  step/start
  │  13  assistant/message "started it"
  │  15  turn/end                           ◄ the job is still running

the work finishes; the agent is idle; the watcher settles "completed"

  │  16  turn/start                         ◄ the notice's own turn
  │  18  user/message   "job job-1 (echo hi) finished: completed"
  │  21  tool/call      job_output {"job_id": "job-1"}
  │  22  tool/result    "completed; output: echo hi"
  │  29  assistant/message "all done"
  │  31  turn/end
```

從頭到尾，log 的邊界都是乾淨的：job 那條執行緒一行都沒寫過。它完成的消息跟其他所有輸入走同一條路，先進 inbox，再在邊界被認領，所以重放的時候讀到的就是一份普通的對話紀錄。

### 改了什麼

與第 10 章相比：

- 所有既有檔案都完整沿用：`agent_loop.py`、`capabilities.py`、`inbox.py`、`kernel.py`、`message.py`、`scheduler.py`、`session_log.py`、`skills.py`、`standin.py`、`system_prompt.py`、`tools.py`。`jobs.py` 是唯一新增的原始碼檔案，因此與第 10 章相比，diff 只包含本章新增的機制，不包含其他改動。
- 這項機制一樣是純粹的組合：生產者用的是第 10 章的 shell seam，通知搭的是第 07 章的 `followup()` 和 `inject()` 兩個現成做法，控制用的 tool 從第 05 章的 registry 進來，擁有者檢查則重用第 05 章的作用域分層，讓呼叫者的身分變成環境自帶的。
- log 沒有多出任何新的事件型別。一個背景 job 在系統中的生命週期，就是幾行普通的紀錄：一行 `tool/result` 帶著它的 id，一行 `user/message` 帶著它的通知。
- `demo.py`：實機示範會把耗時指令啟動為背景 job，由完成通知主動喚醒實際模型，並在啟動第二個 quiet job 的同一則回覆中取消它。

---

## 對照真正的 dsh

以下連結皆指向研究版本 [`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca)。這一層對應的套件家族是 [`packages/jobs`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/jobs)。

| Mini-dsh | 真正的 dsh | 說明 |
| --- | --- | --- |
| `JobRegistry`，ctx key `"jobs"` | [`packages/jobs/jobs/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/jobs/jobs/src/index.ts)：`JobRegistry` | 真正的 Definition 是 `abstract class JobRegistry extends Service`，擁有 `ctx.jobs`（第 62 行）。這本身就是第 10 章介紹的 seam，具體 registry 則以 Provider 掛載。 |
| `run()` 交回 `(cancel, done, read_output)` | [`packages/jobs/jobs/src/types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/jobs/jobs/src/types.ts)：`JobStart` | 一樣的交棒：生產者的 `run()` 交出 `{cancel, done, readOutput?}`，換回一個 `JobId`；之後的每一件事都歸 registry。`JobKindMap`（第 23 到 26 行）只列了兩種生產者，`bash` 和 `subagent`。 |
| 先到先算的定案；`completed` / `failed` / `killed` | [`types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/jobs/jobs/src/types.ts)：`JobOutcome` | 一樣的三種結果，只定案一次；kill 跟完成誰慢了一步，誰就改不動已經定下來的答案。 |
| `delivery="quiet" \| "wakeup"`、`followup()` / `inject()` | [`types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/jobs/jobs/src/types.ts)：`CompletionDelivery`、[`packages/jobs/tool-jobs/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/jobs/tool-jobs/src/index.ts)（第 279 到 300 行） | 完成通知的送法是：擁有者閒著就 `owner.followup()`，忙著就 `owner.inject()`，正是第 07 章那兩個現成做法，就是為了這種場合準備的。 |
| `jobs_plugin` | [`packages/jobs/jobs-local/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/jobs/jobs-local/src/index.ts)：`LocalJobRegistry` | 出貨的 Provider：抽象 seam 後面那個跑在同一個 process 裡的 registry。 |
| `job_output` / `job_list` / `job_kill` | [`packages/jobs/tool-jobs/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/jobs/tool-jobs/src/index.ts)（依序在第 303、343、363 行） | 控制用的 tool，為所有生產者只寫一次；認人是在 registry 裡做的，不是在 tool 裡做的。 |
| 用到 shell seam 的 `shell_job` | [`packages/shell/tool-bash/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/shell/tool-bash/src/index.ts)（第 354 到 356 行） | 真正的 bash tool 是用可有可無的查找拿到 jobs，也就是 `ctx.get('jobs')`，不是 `inject`：沒掛 registry 就退化成只能在前景跑，而不管哪一種，schema 上都只有一個 tool。 |

真正的 jobs 這一層還提供以下功能：

- **第二個生產者，地位平起平坐。** `JobKindMap` 裡寫著 `bash` 和 `subagent`：subagent 那個一次性的背景模式，會把 child 交給 bash tool 用的同一個 registry，這就是控制用的 tool 只需要寫一次的原因。那個生產者就是第 12 章的機制；可以接著用的 subagent 完全不碰 jobs，位置在 Mini-dsh 的實作範圍之外。
- **可真正終止工作的 kill。** 真正的 bash 生產者會透過 `cancel` 向整個 process group 發送訊號；Mini-dsh 只有合作式旗標，工作必須主動檢查。兩者使用相同 seam 與先到先算規則，差別在實際取消機制。
- **走 callback 送，不發 bus 事件。** 跟前面每一層都不一樣，jobs 沒有宣告任何 Cordis 事件：變動和完成都走 `onJobDone` / `onJobsChanged` 這兩個 callback，而擁有者看到的那段通知文字是在 `tool-jobs` 裡組出來的，不是在 registry 裡。
- **更豐富的快照。** `JobSnapshot` 除了 mini 那四個欄位以外，還帶了時間、輸出的游標，以及每一種 job 各自的細節；另外有一個 `wait` 入口可以讓呼叫者卡在那裡等定案。這兩樣跟其他入口一樣，都只認擁有者。

---

## 常見失敗模式

- **沒有 registry 的背景執行緒無法管理。** 工具自行啟動執行緒後立即回傳，輸出、取消與目前工作清單都沒有統一入口。`start()` 雖然簡單，卻提供 id、擁有者驗證與穩定的最終狀態，避免背景工作失去追蹤。
- **未驗證擁有者的 id 會造成跨 session 洩漏。** job id 可能出現在模型文字中，其他 session 也可能取得。若 registry 不驗證呼叫者，任何 session 都能讀取輸出或取消別人的工作。呼叫者身分必須在工具掛載時由環境綁定。
- **允許第二次改寫最終狀態會造成結果不一致。** 晚到的完成事件若能覆蓋 `killed`，呼叫者會看到與先前操作矛盾的狀態。registry 採先到先算，讓所有讀取者共享同一個結果。
- **在 step 中途直接寫入通知會破壞對話紀錄。** watcher thread 沒有 request 邊界，直接 append 可能讓 log 看似包含模型未收到的文字。通知必須先進 inbox，再於下一個邊界認領。
- **turn 的 cancel 不應影響已公開的 job。** 否則取消 turn 會連帶終止模型已被告知正在執行的背景工作。turn 的中止訊號只影響 scheduler；job 公開後，只能透過 `job_kill` 取消。

---

## 動手驗證

[`src/`](src/) 延續第 10 章，並加入：

- [`jobs.py`](src/jobs.py)（新增）：`JobRegistry` 這個 service，帶著認人和先到先算的定案；`JobOwner` 這組詞彙；還有 `job_tools(owner)` 這個 plugin 工廠，把 `shell_job` 生產者和三個控制用的 tool 掛上去。
- [`test.py`](src/test.py)：離線測試證明幾件事：id 活得比自己的 turn 久，通知會開出一個自己的 turn；擁有者在忙的話，通知會停在那裡等 step 的邊界；別的 session 來試探一律被拒絕，而且問不出哪些 id 存在；取消發動的那個 turn 碰不到 job；一個在開始前就被中止的背景呼叫會失敗，而不是什麼都不做；搶著定案的兩邊各跑一次，結果都定住不變；本體炸掉的話會定成 `failed`。
- [`demo.py`](src/demo.py)：將耗時指令啟動為背景 job，讓完成通知主動喚醒實際模型，再在啟動 quiet job 的同一則回覆中取消它。

```bash
python sections/11-jobs/src/test.py    # offline check, no key
```

實機示範需要根目錄的 `requirements.txt` 和一把 key；沒有設定 key 時會自動跳過：

```bash
pip install -r requirements.txt         # anthropic + python-dotenv
cp .env.example .env                    # then set ANTHROPIC_API_KEY
python sections/11-jobs/src/demo.py
```

---

## 參考資料

- [`.agents/notes/implemented/architecture/2026-06-20-generic-long-running-tool-runtime.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/.agents/notes/implemented/architecture/2026-06-20-generic-long-running-tool-runtime.md)：把 jobs 定成一個通用 runtime、讓 bash 和 subagent 成為平起平坐的生產者的那份設計筆記。
- [`.agents/notes/implemented/architecture/2026-07-26-job-registry-seam.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/.agents/notes/implemented/architecture/2026-07-26-job-registry-seam.md)：把抽象的 `JobRegistry` 和本地 Provider 拆開、讓 jobs 變成一個 capability seam 的那份筆記。
