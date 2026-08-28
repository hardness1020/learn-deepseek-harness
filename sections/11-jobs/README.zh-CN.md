<!-- source: README.md @ 3705bd7 -->

# 11 · Jobs

[English](README.md) | [繁體中文](README.zh-TW.md) | 简体中文

> 长时间运行的指令不应阻塞整个 turn。但背景工作如果没有明确拥有者，就没有安全的读取、等待与取消边界。当 job id 公开后，所有控制操作都必须由拥有者权限保护。

到了第 11 章，Mini-dsh 启动的所有工作仍然与当前 turn 绑在一起。第 06 章的 scheduler 保证，已开始的工作一定会完成，而每次调用都必须在 step 结束前产生结果。如果 shell seam 运行的指令需要很久，整个 turn、inbox 与用户都只能原地等待。

一个直觉做法，是让工具启动 worker thread 后立即返回。但这样只是把工作丢到背景，没有解决归属问题。turn 的中止信号已经失去作用，输出没有统一查找入口，任何 session 只要猜到 id，就可能读取或取消他人的工作。

因此，本章要回答的问题是：job id 一旦公开，谁拥有读取、等待和取消它的权限？

这些权责都归 job registry，而 registry 只接受拥有者的操作。工作启动后，必须完整交接：

1. 立即公开 id：工作会在自己的线程中运行，`start()` 立刻返回 job id；启动它的工具调用也只需返回这个 id。
2. 完整接管运行协议：生产者的 `run()` 返回 `(cancel, done, read_output)` 后，id、快照、最终状态与通知都由 registry 管理。
3. 每个入口都验证拥有者：read、kill、list 只接受 job 所属 session 的操作。调用者身份在工具挂载时由环境绑定，不会成为模型可控制的参数。
4. 最终状态只决定一次：`completed`、`failed`、`killed` 中，先发生者生效，之后不再改变。
5. 通知一律通过 inbox：`wakeup` job 在拥有者闲置时使用 followup，忙碌时使用 inject；`quiet` job 则等待查找。背景线程不会直接写入 log。
6. 控制工具只实现一次：`job_output`、`job_kill`、`job_list` 可共用于所有生产者。

---

## 核心机制

只添加一个文件 `jobs.py`，前面沿用的文件都没有修改：

- **`JobRegistry`**：ctx key 为 `"jobs"` 的 service，由 `jobs_plugin` 挂载。它管理 id、拥有者验证、快照与最终状态；每个 job 都有 watcher 线程等待完成，因此即使没有人查找，结果仍会被记录。
- **`JobOwner`**：这个 seam 的词汇：拿来认人的身份，加上通知要送进哪个 agent 的 inbox。
- **`job_tools(owner)`**：一个 plugin 工厂，把一个生产者（`shell_job`，它在自己的线程上通过第 10 章的 shell seam 跑指令）和三个控制用的 tool，一起挂进拥有者的 tool 作用域，而且拥有者的身份是写死在里面的。

核心在于明确交接。生产者将 `run()` 传给 `start()`；`run()` 启动工作并返回协议三元组，而生产者只取得一个 id：

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

`start()` 返回后，取消权便交由 registry 管理。启动工作的 turn 之后无论正常结束或被取消，都不会影响已公开的 job。要停止它只能使用 `job_kill`，而所有操作都会先验证拥有者：

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

`caller_id` 不由模型提供。`job_tools(owner)` 在工具挂入拥有者作用域时就绑定身份，因此 agent B 即使取得 A 的 job id，registry 看到的调用者仍是 B，并会视该 job 为不存在。拥有者检查抛出的例外，会由第 05 章的 pipeline 转成一般 `is_error` 结果。

每个 job 的最终状态只会设置一次。工作完成时，watcher 会设置为 `completed` 或 `failed`；`kill` 则尝试设置为 `killed`。哪一方先成功，结果就固定不再改变：

```python
def _settle(self, job, status, detail=None):
    with self._lock:
        if job.outcome is not None:
            return  # the race already settled; a later voice changes nothing
        job.outcome = {"status": status, "detail": detail}
    self._notify(job)  # outside the lock: delivery may drive a whole turn
```

定案的那一刻，也是拥有者知道这件事的那一刻，而这则通知走的是第 07 章的 inbox，绝不直接写进 log：

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

以下是实际运行时的 log。启动工作的 turn 只取得 job id；工作在 agent 闲置时完成后，通知会主动打开另一个 turn：

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

与第 10 章相比：

- 所有既有文件都完整沿用：`agent_loop.py`、`capabilities.py`、`inbox.py`、`kernel.py`、`message.py`、`scheduler.py`、`session_log.py`、`skills.py`、`standin.py`、`system_prompt.py`、`tools.py`。`jobs.py` 是唯一添加的源代码文件，因此与第 10 章相比，diff 只包含本章添加的机制，不包含其他改动。
- 这项机制一样是纯粹的组合：生产者用的是第 10 章的 shell seam，通知搭的是第 07 章的 `followup()` 和 `inject()` 两个现成做法，控制用的 tool 从第 05 章的 registry 进来，拥有者检查则重用第 05 章的作用域分层，让调用者的身份变成环境自带的。
- log 没有多出任何新的事件类型。一个背景 job 在系统中的生命周期，就是几行普通的记录：一行 `tool/result` 带着它的 id，一行 `user/message` 带着它的通知。
- `demo.py`：在线示例会把耗时指令启动为背景 job，由完成通知主动唤醒实际模型，并在启动第二个 quiet job 的同一则回复中取消它。

---

## 对照真正的 dsh

以下链接皆指向研究版本 [`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca)。这一层对应的软件包系列是 [`packages/jobs`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/jobs)。

| Mini-dsh | 真正的 dsh | 说明 |
| --- | --- | --- |
| `JobRegistry`，ctx key `"jobs"` | [`packages/jobs/jobs/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/jobs/jobs/src/index.ts)：`JobRegistry` | 真正的 Definition 是 `abstract class JobRegistry extends Service`，拥有 `ctx.jobs`（第 62 行）。这本身就是第 10 章介绍的 seam，具体 registry 则以 Provider 挂载。 |
| `run()` 交回 `(cancel, done, read_output)` | [`packages/jobs/jobs/src/types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/jobs/jobs/src/types.ts)：`JobStart` | 一样的交棒：生产者的 `run()` 交出 `{cancel, done, readOutput?}`，换回一个 `JobId`；之后的每一件事都归 registry。`JobKindMap`（第 23 到 26 行）只列了两种生产者，`bash` 和 `subagent`。 |
| 先到先算的定案；`completed` / `failed` / `killed` | [`types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/jobs/jobs/src/types.ts)：`JobOutcome` | 一样的三种结果，只定案一次；kill 跟完成谁慢了一步，谁就改不动已经定下来的答案。 |
| `delivery="quiet" \| "wakeup"`、`followup()` / `inject()` | [`types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/jobs/jobs/src/types.ts)：`CompletionDelivery`、[`packages/jobs/tool-jobs/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/jobs/tool-jobs/src/index.ts)（第 279 到 300 行） | 完成通知的送法是：拥有者闲着就 `owner.followup()`，忙着就 `owner.inject()`，正是第 07 章那两个现成做法，就是为了这种场合准备的。 |
| `jobs_plugin` | [`packages/jobs/jobs-local/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/jobs/jobs-local/src/index.ts)：`LocalJobRegistry` | 出货的 Provider：抽象 seam 后面那个跑在同一个 process 里的 registry。 |
| `job_output` / `job_list` / `job_kill` | [`packages/jobs/tool-jobs/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/jobs/tool-jobs/src/index.ts)（依序在第 303、343、363 行） | 控制用的 tool，为所有生产者只写一次；认人是在 registry 里做的，不是在 tool 里做的。 |
| 用到 shell seam 的 `shell_job` | [`packages/shell/tool-bash/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/shell/tool-bash/src/index.ts)（第 354 到 356 行） | 真正的 bash tool 是用可有可无的查找拿到 jobs，也就是 `ctx.get('jobs')`，不是 `inject`：没挂 registry 就退化成只能在前景跑，而不管哪一种，schema 上都只有一个 tool。 |

真正的 jobs 这一层还提供以下功能：

- **第二个生产者，地位平起平坐。** `JobKindMap` 里写着 `bash` 和 `subagent`：subagent 那个一次性的背景模式，会把 child 交给 bash tool 用的同一个 registry，这就是控制用的 tool 只需要写一次的原因。那个生产者就是第 12 章的机制；可以接着用的 subagent 完全不碰 jobs，位置在 Mini-dsh 的实现范围之外。
- **可真正终止工作的 kill。** 真正的 bash 生产者会通过 `cancel` 向整个 process group 发送信号；Mini-dsh 只有合作式旗标，工作必须主动检查。两者使用相同 seam 与先到先算规则，差别在实际取消机制。
- **走 callback 送，不发 bus 事件。** 跟前面每一层都不一样，jobs 没有声明任何 Cordis 事件：变动和完成都走 `onJobDone` / `onJobsChanged` 这两个 callback，而拥有者看到的那段通知文本是在 `tool-jobs` 里组出来的，不是在 registry 里。
- **更丰富的快照。** `JobSnapshot` 除了 mini 那四个字段以外，还带了时间、输出的光标，以及每一种 job 各自的细节；另外有一个 `wait` 入口可以让调用者卡在那里等定案。这两样跟其他入口一样，都只认拥有者。

---

## 常见失败模式

- **没有 registry 的背景线程无法管理。** 工具自行启动线程后立即返回，输出、取消与目前工作列表都没有统一入口。`start()` 虽然简单，却提供 id、拥有者验证与稳定的最终状态，避免背景工作失去追踪。
- **未验证拥有者的 id 会造成跨 session 泄漏。** job id 可能出现在模型文本中，其他 session 也可能取得。若 registry 不验证调用者，任何 session 都能读取输出或取消别人的工作。调用者身份必须在工具挂载时由环境绑定。
- **允许第二次改写最终状态会造成结果不一致。** 晚到的完成事件若能覆盖 `killed`，调用者会看到与先前操作矛盾的状态。registry 采先到先算，让所有读取者共享同一个结果。
- **在 step 中途直接写入通知会破坏对话记录。** watcher thread 没有 request 边界，直接 append 可能让 log 看似包含模型未收到的文本。通知必须先进 inbox，再于下一个边界认领。
- **turn 的 cancel 不应影响已公开的 job。** 否则取消 turn 会连带终止模型已被告知正在运行的背景工作。turn 的中止信号只影响 scheduler；job 公开后，只能通过 `job_kill` 取消。

---

## 动手验证

[`src/`](src/) 延续第 10 章，并加入：

- [`jobs.py`](src/jobs.py)（添加）：`JobRegistry` 这个 service，带着认人和先到先算的定案；`JobOwner` 这组词汇；还有 `job_tools(owner)` 这个 plugin 工厂，把 `shell_job` 生产者和三个控制用的 tool 挂上去。
- [`test.py`](src/test.py)：离线测试证明几件事：id 活得比自己的 turn 久，通知会开出一个自己的 turn；拥有者在忙的话，通知会停在那里等 step 的边界；别的 session 来试探一律被拒绝，而且问不出哪些 id 存在；取消发动的那个 turn 碰不到 job；一个在开始前就被中止的背景调用会失败，而不是什么都不做；抢着定案的两边各跑一次，结果都定住不变；本体炸掉的话会定成 `failed`。
- [`demo.py`](src/demo.py)：将耗时指令启动为背景 job，让完成通知主动唤醒实际模型，再在启动 quiet job 的同一则回复中取消它。

```bash
python sections/11-jobs/src/test.py    # offline check, no key
```

在线示例需要根目录的 `requirements.txt` 和一把 key；没有设置 key 时会自动跳过：

```bash
pip install -r requirements.txt         # anthropic + python-dotenv
cp .env.example .env                    # then set ANTHROPIC_API_KEY
python sections/11-jobs/src/demo.py
```

---

## 参考资料

- [`.agents/notes/implemented/architecture/2026-06-20-generic-long-running-tool-runtime.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/.agents/notes/implemented/architecture/2026-06-20-generic-long-running-tool-runtime.md)：把 jobs 定成一个通用 runtime、让 bash 和 subagent 成为平起平坐的生产者的那份设计笔记。
- [`.agents/notes/implemented/architecture/2026-07-26-job-registry-seam.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/.agents/notes/implemented/architecture/2026-07-26-job-registry-seam.md)：把抽象的 `JobRegistry` 和本地 Provider 拆开、让 jobs 变成一个 capability seam 的那份笔记。
