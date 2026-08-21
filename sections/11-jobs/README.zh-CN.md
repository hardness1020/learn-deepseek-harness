<!-- source: README.md @ 55e829b -->

# 11 · Jobs

[English](README.md) | [繁體中文](README.zh-TW.md) | 简体中文

> 慢命令不该拖住整个 turn。但自行运行的工作不属于任何人，所以 id 一公布出去，唯一的停止键就得握在一个拥有者手上。

走到第十一个 Section，mini-dsh 开出去的每一件工作，还是跟着自己的 turn 一起死。Section 06 的 scheduler 对这件事的约定很硬：开始了的工作绝不丢下不管，而且每一次调用都要在 step 收掉之前给出答案。一旦你叫 shell seam 去跑一个很慢的东西，这个约定就会把整个 turn 绑在那里；model、inbox、用户，全都在等同一条命令。

最直觉的逃法，是让 tool 的本体开一条线程就回来。但这样一来，这件工作就不属于任何人了。turn 的中止信号指着一次早就回来的调用；线程的输出没有地址可以找；任何一个 session 只要猜中 id，就能读它、杀它、等它。丢到后台很简单，难的是归属。

所以：job id 一旦公开出去，取消的权责归谁？

归 registry，而且只认拥有者；这次交棒必须交得干干净净：

1. id 马上公开：工作在自己的线程上跑，`start()` 当场交回一个 job id，而发动它的那次调用就回一条正常的结果，里面除了那个 id 什么都没有。
2. 整份协议一起接过来：生产者的 `run()` 交回 `(cancel, done, read_output)`，从那之后，id、快照、定案、通知怎么送，全都归 registry 管。
3. 每一个入口都要认人：read、kill、list 只回答拥有这个 job 的 session，而调用者的身份是环境自带的，在它的 tool 挂上去时就定死了，绝不会变成 model 的一个参数。
4. 定案只定一次，先到的算：`completed`、`failed`、`killed`，谁先到就是谁，之后永远不变。
5. 通知一律走 inbox 送：`wakeup` 的 job 碰到闲着的拥有者就 followup，碰到忙着的就 inject；`quiet` 的 job 就等人来问。后台工作永远不会自己往 log 加一行。
6. 控制用的 tool 只写一次：`job_output`、`job_kill`、`job_list` 对每一种生产者都一视同仁。

---

## Mechanism

只新增一个文件 `jobs.py`，前面搬过来的文件一个都没动：

- **`JobRegistry`**：jobs 这个 service，ctx key 是 `"jobs"`，由 `jobs_plugin` 挂上去。id、认人、快照、先到先算的定案，都归它管；每个 job 配一条 watcher 线程等着工作结束，所以就算没人来问，完成这件事还是会落地。
- **`JobOwner`**：这个 seam 的词汇：拿来认人的身份，加上通知要送进哪个 agent 的 inbox。
- **`job_tools(owner)`**：一个 plugin 工厂，把一个生产者（`shell_job`，它在自己的线程上通过 Section 10 的 shell seam 跑命令）和三个控制用的 tool，一起挂进拥有者的 tool 作用域，而且拥有者的身份是写死在里面的。

交棒是整件事的核心。生产者交给 `start()` 一个 `run()`，这个 `run()` 会把工作启动起来，再交回协议的三元组，而生产者拿回来的只有一个 id：

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

这个 return 发生的瞬间，取消权就换手了。发动这次调用的那个 turn 可以结束、可以中止、可以整个被取消，这些都到不了 job 身上，因为 turn 的信号从来就没接到它上面。剩下唯一一道门是 `job_kill`，而每一道门都先认人：

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

`caller_id` 永远不是 model 给的。`job_tools(owner)` 在 tool 挂进拥有者作用域的时候就把身份写死了，所以不管 agent B 打出什么 id，它的 tool 对 registry 报的身份都是 B，A 的 job 对它来说就是不存在。认人这关会抛异常；Section 05 的 pipeline 再把这次拒绝变成一条正常的 `is_error` 结果。

一个 job 只会结束一次。工作跑完的时候，watcher 把它定成 `completed` 或 `failed`；`kill` 把它定成 `killed`；谁先到，谁就是最后的结果，永远不变：

```python
def _settle(self, job, status, detail=None):
    with self._lock:
        if job.outcome is not None:
            return  # the race already settled; a later voice changes nothing
        job.outcome = {"status": status, "detail": detail}
    self._notify(job)  # outside the lock: delivery may drive a whole turn
```

定案的那一刻，也是拥有者知道这件事的那一刻，而这则通知走的是 Section 07 的 inbox，绝不直接写进 log：

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

下面是一次真的运行，log 就是这样记的。发动的那个 turn 收在一个 id 上就没别的了；工作在 agent 闲着的时候跑完，通知再以一个 model 根本没要求过的 turn 回来：

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

从头到尾，log 的边界都是干净的：job 那条线程一行都没写过。它完成的消息跟其他所有输入走同一条路，先进 inbox，再在边界被认领，所以重放的时候读到的就是一份普通的对话记录。

### 改了什么

跟 Section 10 比起来：

- 每一个搬过来的文件都原封不动：`agent_loop.py`、`capabilities.py`、`inbox.py`、`kernel.py`、`message.py`、`scheduler.py`、`session_log.py`、`skills.py`、`standin.py`、`system_prompt.py`、`tools.py`。`jobs.py` 是唯一新增的源代码文件，所以拿 10 来 diff，跑出来的就是这个 Section 的 Mechanism，没有别的。
- 这个 Mechanism 一样是纯粹的组合：生产者用的是 Section 10 的 shell seam，通知搭的是 Section 07 的 `followup()` 和 `inject()` 两个现成做法，控制用的 tool 从 Section 05 的 registry 进来，认人这关则重用 Section 05 的作用域分层，让调用者的身份变成环境自带的。
- log 没有多出任何新的事件类型。一个后台 job 摊在台面上的一生，就是几行普通的记录：一行 `tool/result` 带着它的 id，一行 `user/message` 带着它的通知。
- `demo.py`：Live demo 把一条真的很慢的命令丢成后台 job，让完成通知在一个 model 没要求过的 turn 里把真 model 叫醒，还在启动第二个 quiet job 的同一条回复里就把它杀掉。

---

## In real dsh

所有指过去的链接都固定在 Studied version [`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca) 上。这一层对应的包家族是 [`packages/jobs`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/jobs)。

| Mini-dsh | 真正的 dsh | 说明 |
| --- | --- | --- |
| `JobRegistry`，ctx key `"jobs"` | [`packages/jobs/jobs/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/jobs/jobs/src/index.ts)：`JobRegistry` | 真正的 Definition 是 `abstract class JobRegistry extends Service`，它拥有 `ctx.jobs`（第 62 行）：这本身就是一个 Section 10 讲的 seam，具体的 registry 是当成 Provider 挂上去的。 |
| `run()` 交回 `(cancel, done, read_output)` | [`packages/jobs/jobs/src/types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/jobs/jobs/src/types.ts)：`JobStart` | 一样的交棒：生产者的 `run()` 交出 `{cancel, done, readOutput?}`，换回一个 `JobId`；之后的每一件事都归 registry。`JobKindMap`（第 23 到 26 行）只列了两种生产者，`bash` 和 `subagent`。 |
| 先到先算的定案；`completed` / `failed` / `killed` | [`types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/jobs/jobs/src/types.ts)：`JobOutcome` | 一样的三种结果，只定案一次；kill 跟完成谁慢了一步，谁就改不动已经定下来的答案。 |
| `delivery="quiet" \| "wakeup"`、`followup()` / `inject()` | [`types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/jobs/jobs/src/types.ts)：`CompletionDelivery`、[`packages/jobs/tool-jobs/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/jobs/tool-jobs/src/index.ts)（第 279 到 300 行） | 完成通知的送法是：拥有者闲着就 `owner.followup()`，忙着就 `owner.inject()`，正是 Section 07 那两个现成做法，就是为了这种场合准备的。 |
| `jobs_plugin` | [`packages/jobs/jobs-local/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/jobs/jobs-local/src/index.ts)：`LocalJobRegistry` | 出货的 Provider：抽象 seam 后面那个跑在同一个 process 里的 registry。 |
| `job_output` / `job_list` / `job_kill` | [`packages/jobs/tool-jobs/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/jobs/tool-jobs/src/index.ts)（依序在第 303、343、363 行） | 控制用的 tool，为所有生产者只写一次；认人是在 registry 里做的，不是在 tool 里做的。 |
| 用到 shell seam 的 `shell_job` | [`packages/shell/tool-bash/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/shell/tool-bash/src/index.ts)（第 354 到 356 行） | 真正的 bash tool 是用可有可无的查找拿到 jobs，也就是 `ctx.get('jobs')`，不是 `inject`：没挂 registry 就退化成只能在前台跑，而不管哪一种，schema 上都只有一个 tool。 |

真正的 jobs 这一层，在这个 Section 的 Mechanism 之上，还多做了这些：

- **第二个生产者，地位平起平坐。** `JobKindMap` 里写着 `bash` 和 `subagent`：subagent 那个一次性的后台模式，会把 child 交给 bash tool 用的同一个 registry，这就是控制用的 tool 只需要写一次的原因。那个生产者就是 Section 12 的 Mechanism；可以接着用的 subagent 完全不碰 jobs，位置在这次重建的 Ceiling 之上。
- **真的杀得死的 kill。** 真正的 bash 生产者，它的 `cancel` 会对一整个 process group 送信号；mini 的 cancel 只是一个标志，工作愿意的话才会去看。seam 的形状，还有谁先定案这件事，两边一模一样，只是 `cancel` 背后的机器大很多。
- **走 callback 送，不发 bus 事件。** 跟前面每一层都不一样，jobs 没有声明任何 Cordis 事件：变动和完成都走 `onJobDone` / `onJobsChanged` 这两个 callback，而拥有者看到的那段通知文本是在 `tool-jobs` 里组出来的，不是在 registry 里。
- **更丰富的快照。** `JobSnapshot` 除了 mini 那四个字段以外，还带了时间、输出的游标，以及每一种 job 各自的细节；另外有一个 `wait` 入口可以让调用者卡在那里等定案。这两样跟其他入口一样，都只认拥有者。

---

## Failure modes

- **没有 registry 的后台线程，就是一件没人认领的工作。** tool 的本体开一条线程就回来，留下的是找不到地址的输出，和一个没接到任何东西的停止键；下一条慢命令照样这样丢到后台，然后就没有人数得出来现在到底有什么在跑。`start()` 很小，但它换来的 id、认人、定案，就是把工作丢到后台和把工作弄丢这两件事的差别。
- **没认人的 id，就是跨 session 的外泄。** job id 是夹在 model 的文本里到处跑的，所以任何一个 session 都能打出任何一个 id。要是 registry 谁问都答，一个 session 就会读到另一个 session 的输出，或是把人家的 build 杀掉。每一个入口都先认人，而调用者的身份是在挂上去的时候就写死的，写在任何 prompt 都碰不到的地方。
- **第二次定案会改写历史。** 让一个晚到的完成盖掉 `killed`，那亲手杀掉 job 的拥有者事后读到的会是 `completed`，反过来也一样；于是每一个要用这个结果的人，都得自己想一套判定谁赢的规则。registry 用先到先算，一次就替所有人把输赢定下来，而 `job_kill` 给出的就是真正赢的那一边。
- **在 step 中间插进来的通知会把对话记录撕破。** 负责定案的那条线程手上没有任何边界：工作一跑完就写下去的那一行，会掉在一次请求和它的回复中间，等于在说 model 看过一段它其实从来没收到的文本。通知一律搭 inbox，在下一个边界才进来，跟 Section 07 之后所有 turn 中途才到的东西一样。
- **能伸进 job 里的 cancel，会让后台工作变成一句谎话。** 如果 turn 的中止会把已经公开的 job 杀掉，那取消一个 turn，就会无声无息地毁掉 model 早就说过已经开始的工作。turn 的信号到 scheduler 为止；被中止拦在派出去之前的调用还是会给出答案，只是那是一条合成出来的错误结果（Section 06）；而一个公开出去的 job，只有 `job_kill` 杀得死。

---

## 跑跑看

[`src/`](src/) 把 10 搬过来，再加上：

- [`jobs.py`](src/jobs.py)（新增）：`JobRegistry` 这个 service，带着认人和先到先算的定案；`JobOwner` 这组词汇；还有 `job_tools(owner)` 这个 plugin 工厂，把 `shell_job` 生产者和三个控制用的 tool 挂上去。
- [`test.py`](src/test.py)：Offline check 证明几件事：id 活得比自己的 turn 久，通知会开出一个自己的 turn；拥有者在忙的话，通知会停在那里等 step 的边界；别的 session 来试探一律被拒绝，而且问不出哪些 id 存在；取消发动的那个 turn 碰不到 job；一个在开始前就被中止的后台调用会失败，而不是什么都不做；抢着定案的两边各跑一次，结果都定住不变；本体炸掉的话会定成 `failed`。
- [`demo.py`](src/demo.py)：Live demo 把一条真的很慢的命令丢到后台，让完成通知在一个 model 没要求过的 turn 里把真 model 叫醒，再在启动 quiet job 的那则回复里就把它杀掉。

```bash
python sections/11-jobs/src/test.py    # offline check, no key
```

Live demo 需要根目录的 `requirements.txt` 和一把 key；没有 key 的话，它会安静地跳过：

```bash
pip install -r requirements.txt         # anthropic + python-dotenv
cp .env.example .env                    # then set ANTHROPIC_API_KEY
python sections/11-jobs/src/demo.py
```

---

## 出处

- [`.agents/notes/implemented/architecture/2026-06-20-generic-long-running-tool-runtime.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/.agents/notes/implemented/architecture/2026-06-20-generic-long-running-tool-runtime.md)：把 jobs 定成一个通用 runtime、让 bash 和 subagent 成为平起平坐的生产者的那份设计笔记。
- [`.agents/notes/implemented/architecture/2026-07-26-job-registry-seam.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/.agents/notes/implemented/architecture/2026-07-26-job-registry-seam.md)：把抽象的 `JobRegistry` 和本地 Provider 拆开、让 jobs 变成一个 capability seam 的那份笔记。
