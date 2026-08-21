<!-- source: README.md @ 55e829b -->

# 11 · Jobs

[English](README.md) | 繁體中文 | [简体中文](README.zh-CN.md)

> 慢指令不該拖住整個 turn。但自行執行的工作不屬於任何人，所以 id 一公布出去，唯一的停止鍵就得握在一個擁有者手上。

走到第十一個 Section，mini-dsh 開出去的每一件工作，還是跟著自己的 turn 一起死。Section 06 的 scheduler 對這件事的約定很硬：開始了的工作絕不丟下不管，而且每一次呼叫都要在 step 收掉之前給出答案。一旦你叫 shell seam 去跑一個很慢的東西，這個約定就會把整個 turn 綁在那裡；model、inbox、使用者，全都在等同一道指令。

最直覺的逃法，是讓 tool 的本體開一條執行緒就回來。但這樣一來，這件工作就不屬於任何人了。turn 的中止訊號指著一次早就回來的呼叫；執行緒的輸出沒有地址可以找；任何一個 session 只要猜中 id，就能讀它、殺它、等它。丟到背景很簡單，難的是歸屬。

所以：job id 一旦公開出去，取消的權責歸誰？

歸 registry，而且只認擁有者；這次交棒必須交得乾乾淨淨：

1. id 馬上公開：工作在自己的執行緒上跑，`start()` 當場交回一個 job id，而發動它的那次呼叫就回一則正常的結果，裡面除了那個 id 什麼都沒有。
2. 整份協定一起接過來：生產者的 `run()` 交回 `(cancel, done, read_output)`，從那之後，id、快照、定案、通知怎麼送，全都歸 registry 管。
3. 每一個入口都要認人：read、kill、list 只回答擁有這個 job 的 session，而呼叫者的身分是環境自帶的，在它的 tool 掛上去時就定死了，絕不會變成 model 的一個參數。
4. 定案只定一次，先到的算：`completed`、`failed`、`killed`，誰先到就是誰，之後永遠不變。
5. 通知一律走 inbox 送：`wakeup` 的 job 碰到閒著的擁有者就 followup，碰到忙著的就 inject；`quiet` 的 job 就等人來問。背景工作永遠不會自己往 log 加一行。
6. 控制用的 tool 只寫一次：`job_output`、`job_kill`、`job_list` 對每一種生產者都一視同仁。

---

## Mechanism

只新增一個檔案 `jobs.py`，前面搬過來的檔案一個都沒動：

- **`JobRegistry`**：jobs 這個 service，ctx key 是 `"jobs"`，由 `jobs_plugin` 掛上去。id、認人、快照、先到先算的定案，都歸它管；每個 job 配一條 watcher 執行緒等著工作結束，所以就算沒人來問，完成這件事還是會落地。
- **`JobOwner`**：這個 seam 的詞彙：拿來認人的身分，加上通知要送進哪個 agent 的 inbox。
- **`job_tools(owner)`**：一個 plugin 工廠，把一個生產者（`shell_job`，它在自己的執行緒上透過 Section 10 的 shell seam 跑指令）和三個控制用的 tool，一起掛進擁有者的 tool 作用域，而且擁有者的身分是寫死在裡面的。

交棒是整件事的核心。生產者交給 `start()` 一個 `run()`，這個 `run()` 會把工作啟動起來，再交回協定的三元組，而生產者拿回來的只有一個 id：

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

這個 return 發生的瞬間，取消權就換手了。發動這次呼叫的那個 turn 可以結束、可以中止、可以整個被取消，這些都到不了 job 身上，因為 turn 的訊號從來就沒接到它上面。剩下唯一一道門是 `job_kill`，而每一道門都先認人：

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

`caller_id` 永遠不是 model 給的。`job_tools(owner)` 在 tool 掛進擁有者作用域的時候就把身分寫死了，所以不管 agent B 打出什麼 id，它的 tool 對 registry 報的身分都是 B，A 的 job 對它來說就是不存在。認人這關會丟例外；Section 05 的 pipeline 再把這次拒絕變成一則正常的 `is_error` 結果。

一個 job 只會結束一次。工作跑完的時候，watcher 把它定成 `completed` 或 `failed`；`kill` 把它定成 `killed`；誰先到，誰就是最後的結果，永遠不變：

```python
def _settle(self, job, status, detail=None):
    with self._lock:
        if job.outcome is not None:
            return  # the race already settled; a later voice changes nothing
        job.outcome = {"status": status, "detail": detail}
    self._notify(job)  # outside the lock: delivery may drive a whole turn
```

定案的那一刻，也是擁有者知道這件事的那一刻，而這則通知走的是 Section 07 的 inbox，絕不直接寫進 log：

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

下面是一次真的執行，log 就是這樣記的。發動的那個 turn 收在一個 id 上就沒別的了；工作在 agent 閒著的時候跑完，通知再以一個 model 根本沒要求過的 turn 回來：

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

跟 Section 10 比起來：

- 每一個搬過來的檔案都原封不動：`agent_loop.py`、`capabilities.py`、`inbox.py`、`kernel.py`、`message.py`、`scheduler.py`、`session_log.py`、`skills.py`、`standin.py`、`system_prompt.py`、`tools.py`。`jobs.py` 是唯一新增的原始碼檔案，所以拿 10 來 diff，跑出來的就是這個 Section 的 Mechanism，沒有別的。
- 這個 Mechanism 一樣是純粹的組合：生產者用的是 Section 10 的 shell seam，通知搭的是 Section 07 的 `followup()` 和 `inject()` 兩個現成做法，控制用的 tool 從 Section 05 的 registry 進來，認人這關則重用 Section 05 的作用域分層，讓呼叫者的身分變成環境自帶的。
- log 沒有多出任何新的事件型別。一個背景 job 攤在檯面上的一生，就是幾行普通的紀錄：一行 `tool/result` 帶著它的 id，一行 `user/message` 帶著它的通知。
- `demo.py`：Live demo 把一道真的很慢的指令丟成背景 job，讓完成通知在一個 model 沒要求過的 turn 裡把真 model 叫醒，還在啟動第二個 quiet job 的同一則回覆裡就把它殺掉。

---

## In real dsh

所有指過去的連結都固定在 Studied version [`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca) 上。這一層對應的套件家族是 [`packages/jobs`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/jobs)。

| Mini-dsh | 真正的 dsh | 說明 |
| --- | --- | --- |
| `JobRegistry`，ctx key `"jobs"` | [`packages/jobs/jobs/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/jobs/jobs/src/index.ts)：`JobRegistry` | 真正的 Definition 是 `abstract class JobRegistry extends Service`，它擁有 `ctx.jobs`（第 62 行）：這本身就是一個 Section 10 講的 seam，具體的 registry 是當成 Provider 掛上去的。 |
| `run()` 交回 `(cancel, done, read_output)` | [`packages/jobs/jobs/src/types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/jobs/jobs/src/types.ts)：`JobStart` | 一樣的交棒：生產者的 `run()` 交出 `{cancel, done, readOutput?}`，換回一個 `JobId`；之後的每一件事都歸 registry。`JobKindMap`（第 23 到 26 行）只列了兩種生產者，`bash` 和 `subagent`。 |
| 先到先算的定案；`completed` / `failed` / `killed` | [`types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/jobs/jobs/src/types.ts)：`JobOutcome` | 一樣的三種結果，只定案一次；kill 跟完成誰慢了一步，誰就改不動已經定下來的答案。 |
| `delivery="quiet" \| "wakeup"`、`followup()` / `inject()` | [`types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/jobs/jobs/src/types.ts)：`CompletionDelivery`、[`packages/jobs/tool-jobs/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/jobs/tool-jobs/src/index.ts)（第 279 到 300 行） | 完成通知的送法是：擁有者閒著就 `owner.followup()`，忙著就 `owner.inject()`，正是 Section 07 那兩個現成做法，就是為了這種場合準備的。 |
| `jobs_plugin` | [`packages/jobs/jobs-local/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/jobs/jobs-local/src/index.ts)：`LocalJobRegistry` | 出貨的 Provider：抽象 seam 後面那個跑在同一個 process 裡的 registry。 |
| `job_output` / `job_list` / `job_kill` | [`packages/jobs/tool-jobs/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/jobs/tool-jobs/src/index.ts)（依序在第 303、343、363 行） | 控制用的 tool，為所有生產者只寫一次；認人是在 registry 裡做的，不是在 tool 裡做的。 |
| 用到 shell seam 的 `shell_job` | [`packages/shell/tool-bash/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/shell/tool-bash/src/index.ts)（第 354 到 356 行） | 真正的 bash tool 是用可有可無的查找拿到 jobs，也就是 `ctx.get('jobs')`，不是 `inject`：沒掛 registry 就退化成只能在前景跑，而不管哪一種，schema 上都只有一個 tool。 |

真正的 jobs 這一層，在這個 Section 的 Mechanism 之上，還多做了這些：

- **第二個生產者，地位平起平坐。** `JobKindMap` 裡寫著 `bash` 和 `subagent`：subagent 那個一次性的背景模式，會把 child 交給 bash tool 用的同一個 registry，這就是控制用的 tool 只需要寫一次的原因。那個生產者就是 Section 12 的 Mechanism；可以接著用的 subagent 完全不碰 jobs，位置在這次重建的 Ceiling 之上。
- **真的殺得死的 kill。** 真正的 bash 生產者，它的 `cancel` 會對一整個 process group 送訊號；mini 的 cancel 只是一個旗標，工作願意的話才會去看。seam 的形狀，還有誰先定案這件事，兩邊一模一樣，只是 `cancel` 背後的機器大很多。
- **走 callback 送，不發 bus 事件。** 跟前面每一層都不一樣，jobs 沒有宣告任何 Cordis 事件：變動和完成都走 `onJobDone` / `onJobsChanged` 這兩個 callback，而擁有者看到的那段通知文字是在 `tool-jobs` 裡組出來的，不是在 registry 裡。
- **更豐富的快照。** `JobSnapshot` 除了 mini 那四個欄位以外，還帶了時間、輸出的游標，以及每一種 job 各自的細節；另外有一個 `wait` 入口可以讓呼叫者卡在那裡等定案。這兩樣跟其他入口一樣，都只認擁有者。

---

## Failure modes

- **沒有 registry 的背景執行緒，就是一件沒人認領的工作。** tool 的本體開一條執行緒就回來，留下的是找不到地址的輸出，和一個沒接到任何東西的停止鍵；下一道慢指令照樣這樣丟到背景，然後就沒有人數得出來現在到底有什麼在跑。`start()` 很小，但它換來的 id、認人、定案，就是把工作丟到背景和把工作弄丟這兩件事的差別。
- **沒認人的 id，就是跨 session 的外洩。** job id 是夾在 model 的文字裡到處跑的，所以任何一個 session 都能打出任何一個 id。要是 registry 誰問都答，一個 session 就會讀到另一個 session 的輸出，或是把人家的 build 殺掉。每一個入口都先認人，而呼叫者的身分是在掛上去的時候就寫死的，寫在任何 prompt 都碰不到的地方。
- **第二次定案會改寫歷史。** 讓一個晚到的完成蓋掉 `killed`，那親手殺掉 job 的擁有者事後讀到的會是 `completed`，反過來也一樣；於是每一個要用這個結果的人，都得自己想一套判定誰贏的規則。registry 用先到先算，一次就替所有人把輸贏定下來，而 `job_kill` 給出的就是真正贏的那一邊。
- **在 step 中間插進來的通知會把對話紀錄撕破。** 負責定案的那條執行緒手上沒有任何邊界：工作一跑完就寫下去的那一行，會掉在一次請求和它的回覆中間，等於在說 model 看過一段它其實從來沒收到的文字。通知一律搭 inbox，在下一個邊界才進來，跟 Section 07 之後所有 turn 中途才到的東西一樣。
- **能伸進 job 裡的 cancel，會讓背景工作變成一句謊話。** 如果 turn 的中止會把已經公開的 job 殺掉，那取消一個 turn，就會無聲無息地毀掉 model 早就說過已經開始的工作。turn 的訊號到 scheduler 為止；被中止攔在派出去之前的呼叫還是會給出答案，只是那是一則合成出來的錯誤結果（Section 06）；而一個公開出去的 job，只有 `job_kill` 殺得死。

---

## 跑跑看

[`src/`](src/) 把 10 搬過來，再加上：

- [`jobs.py`](src/jobs.py)（新增）：`JobRegistry` 這個 service，帶著認人和先到先算的定案；`JobOwner` 這組詞彙；還有 `job_tools(owner)` 這個 plugin 工廠，把 `shell_job` 生產者和三個控制用的 tool 掛上去。
- [`test.py`](src/test.py)：Offline check 證明幾件事：id 活得比自己的 turn 久，通知會開出一個自己的 turn；擁有者在忙的話，通知會停在那裡等 step 的邊界；別的 session 來試探一律被拒絕，而且問不出哪些 id 存在；取消發動的那個 turn 碰不到 job；一個在開始前就被中止的背景呼叫會失敗，而不是什麼都不做；搶著定案的兩邊各跑一次，結果都定住不變；本體炸掉的話會定成 `failed`。
- [`demo.py`](src/demo.py)：Live demo 把一道真的很慢的指令丟到背景，讓完成通知在一個 model 沒要求過的 turn 裡把真 model 叫醒，再在啟動 quiet job 的那則回覆裡就把它殺掉。

```bash
python sections/11-jobs/src/test.py    # offline check, no key
```

Live demo 需要根目錄的 `requirements.txt` 和一把 key；沒有 key 的話，它會安靜地跳過：

```bash
pip install -r requirements.txt         # anthropic + python-dotenv
cp .env.example .env                    # then set ANTHROPIC_API_KEY
python sections/11-jobs/src/demo.py
```

---

## 出處

- [`.agents/notes/implemented/architecture/2026-06-20-generic-long-running-tool-runtime.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/.agents/notes/implemented/architecture/2026-06-20-generic-long-running-tool-runtime.md)：把 jobs 定成一個通用 runtime、讓 bash 和 subagent 成為平起平坐的生產者的那份設計筆記。
- [`.agents/notes/implemented/architecture/2026-07-26-job-registry-seam.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/.agents/notes/implemented/architecture/2026-07-26-job-registry-seam.md)：把抽象的 `JobRegistry` 和本地 Provider 拆開、讓 jobs 變成一個 capability seam 的那份筆記。
