<!-- source: README.md @ 55e829b -->

# 12 · Subagent

[English](README.md) | [繁體中文](README.zh-TW.md) | 简体中文

> 旁支任务不该花掉 parent 的 context。用继承 agent 来做，就假设负责回答的东西住在这个 process 里，但它常常不在。所以 parent 只问名字，拿回一个 run。

Section 11 教会 mini-dsh 把工作丢到后台，但所有的思考还是挤在同一个 context window 里。叫 agent 去跑个腿，摘要一个包、追一个一直失败的测试，这趟腿的完整对话记录就会永远跟在 parent 的历史里，把它原本要服务的正事挤掉。委派就是出路：把任务交给一个有自己的 session、自己的 tool 作用域、自己的 context 的 child，然后只拿回一个答案。

最直觉的做法是继承。Section 04 的 `Agent` 早就会跑 turn 了，所以 `class Subagent(Agent)` 看起来像是站在起跑线前面。但回答一次委派的，不一定是这个 process 里的 agent。真正的 dsh 出货的 Provider 里，有的会 fork 出新的 process，有的通过一套传输协议去驱动另一个产品，有的干脆包住另一套 harness；要是拿基类当契约，这些人全都得假装自己内部长得像 Agent，只为了挂得进 registry。

所以：为什么接口要架在“开一个 child、交回一次 run”上面，而不是继承出一个 agent 子类？

因为 parent 这一侧的契约只有四个动词，而继承一个类，等于把其他所有东西也一起答应下来。这个 Section 这样把它做出来：

1. 用名字记住 Provider：一个 ctx key 底下一份 registry，每次注册都交回自己的撤销动作。
2. Provider 就是一份 callable 的契约：解好的启动请求进去，一个 run 出来，registry 从来不去看它背后是什么。
3. 让 run 就是 parent 这一侧契约的全部：`cancel`、`done`、`read_output`，刻意用 Section 11 那组协议三元组。
4. 前台模式：这次 tool 调用就卡在 `done` 上等，然后把 child 的回复当答案交出去。
5. 后台模式：把同一组三元组原封不动交给 job registry，于是 subagent 就成了第二个生产者，跟 shell 平起平坐，而 Section 11 的控制用 tool 一行新代码都不用写就能服务它。
6. 所有的拒绝都走 Section 05 那道门：不认识的名字、没挂 job registry、child 炸掉，全都变成正常的 `is_error` 结果。

---

## Mechanism

只新增一个文件 `subagent.py`，前面搬过来的文件一个都没动：

- **`SubagentRuntime`**：subagents 这个 service，ctx key 是 `"subagents"`，由 `subagent_plugin` 挂上去。它是一份用名字当键的 Provider registry；`start()` 把名字解出来，组好那份请求，再把 Provider 做出来的那个 run 原样交回去。
- **`SubagentRun`**：parent 这一侧的契约，一个冻住的三元组。
- **`in_process_provider(ctx, model_factory)`**：这只是其中一个 Provider，不是契约本身：它用 parent 出身的那几个 service，建出一个 child `Agent`。
- **`subagent_tools(owner)`**：一个 plugin 工厂，把唯一那个 `subagent` tool 挂进拥有者的作用域，拥有者的身份写死在里面。

registry 之所以小，是因为契约本来就小。任何一个能把解好的请求变成一个 run 的 callable，都算是 Provider；它身上没有一个地方写着“agent”：

```python
def start(self, name, task):
    """Resolve the name, hand the provider a resolved request, get a run."""
    provider = self._providers.get(name)
    if provider is None:
        raise LookupError(f"no subagent provider registered under '{name}'")
    self._count += 1
    return provider({"id": f"sub-{self._count}", "task": task})
```

而交回来的东西就只有这个 run。它刻意做成 Section 11 的协议三元组，因为这个形状早就回答了 parent 对一件自己已经抓不住的工作可能会问的每一个问题：

```python
@dataclass(frozen=True)
class SubagentRun:
    cancel: callable  # ask the child to stop; cooperative, best effort
    done: callable  # block until it ends: ("completed", None) | ("failed", detail)
    read_output: callable  # the child's answer so far, as text
```

同一个 process 里的那个 Provider，让你看清楚契约为什么可以这么薄。它用 parent 出身的那几个 service，`sessions`、`agents`、`tools`，开出一个 child，在这个 run 自己的线程上推一次 `send()`，再从 child 的 log 上把答案读出来。child 的整个故事都留在它自己的 session 里；唯一跨回来的只有那个 run。就算一个 Provider 背后根本没有 agent，答案是从缓存、从一个子 process、从另一个产品来的，它交回来的还是同一组三元组，registry 和 tool 都分不出差别。

Consumer 把两种委派模式折进同一个 tool 本体里，而这个折叠正是第 3 条要求换来的回报：

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

前台模式就地等。后台模式把这个 run 包成生产者协议交给 Section 11，之后的每一件事都归它：id、认人、先到先算的定案，还有走 inbox 的完成通知。job registry 是用可有可无的查找拿到的，跟真正的 dsh 一样，所以一个没挂 jobs 的 harness 会大声拒绝后台委派，而不是偷偷把 turn 卡在那里。

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

下面是一次前台委派，两边的 log 都在这里。parent 的对话记录里只留下这趟腿的两行，一行调用、一行答案；跑腿本身是另一个地方的一整个 session：

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

换成后台模式，同一个 run 改搭 Section 11：parent 的 turn 收在 `"started job-1"` 上，child 在 parent 闲着的时候思考，通知再以一个 followup 的 turn 到达，在那里 `job_output` 给出 child 的回复，`job_list` 报出来的种类是 `subagent`。控制用的 tool 没有变，变的是生产者。

### 改了什么

跟 Section 11 比起来：

- 每一个搬过来的文件都原封不动：`agent_loop.py`、`capabilities.py`、`inbox.py`、`jobs.py`、`kernel.py`、`message.py`、`scheduler.py`、`session_log.py`、`skills.py`、`standin.py`、`system_prompt.py`、`tools.py`。`subagent.py` 是唯一新增的源代码文件，所以拿 11 来 diff，跑出来的就是这个 Section 的 Mechanism，没有别的。
- 这个 Mechanism 是纯粹的组合：child 是通过 Section 02 的 sessions、Section 04 的 agents、Section 05 的 tool 这几个 service 开出来的；run 就是 Section 11 的协议三元组；后台模式把这组三元组交给 job registry，让 subagent 成为 Section 11 早就预告过的第二个生产者。
- log 没有多出任何新的事件类型。一次委派在 parent 的 log 里摊在台面上的一生，就是一行 `tool/call` 加一行 `tool/result`；剩下的故事是它自己那个普通的 session。
- `demo.py`：Live demo 在前台委派给一个对着真 API 跑的 child，把它的答案引述出来，接着再把第二个 child 丢到后台，让它的完成通知在一个 parent 没要求过的 turn 里把 parent 叫醒。

---

## In real dsh

所有指过去的链接都固定在 Studied version [`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca) 上。这一层对应的包家族是 [`packages/subagent`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/subagent)。

| Mini-dsh | 真正的 dsh | 说明 |
| --- | --- | --- |
| `SubagentRuntime`，ctx key `"subagents"` | [`packages/subagent/subagent/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/subagent/subagent/src/index.ts)：`SubagentRuntime` | 这个 runtime（第 171 行）是一个具体的 `Service`，跟 Section 11 那个抽象的 `JobRegistry` 不一样：它守的 seam 是 Provider 的接口，不是 registry 本身。 |
| Provider 是一份 callable 的契约 | [`packages/subagent/subagent/src/types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/subagent/subagent/src/types.ts)：`SubagentProvider` | 这个设计问题直接写在类型系统里：`SubagentProvider`（第 285 行）是一个 TS 接口，不是 `Service`，也不是 `Agent` 的子类；任何能把解好的启动请求变成一个 `SubagentRun` 的东西都算数。 |
| `in_process_provider` | [`packages/subagent/subagent-in-process-driver/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/subagent/subagent-in-process-driver/src/index.ts) | 第 132 行是同一招：child 是用 `parent.ctx.agents.create()` 建出来的，走的是 Section 04 那道普通的门，不是什么私有的构造函数。 |
| 后台模式下交给 jobs 的那个 run | [`packages/subagent/subagent/src/run-settlement.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/subagent/subagent/src/run-settlement.ts)、[`packages/subagent/tool-subagent/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/subagent/tool-subagent/src/index.ts)（第 408 到 423 行） | 一次性的后台委派就是 `jobs.start({kind: 'subagent', ...})`：`JobKindMap` 里的第二个种类，跟 `bash` 平起平坐，正是这个 Section 重建的那次交棒。 |
| `jobs = ctx.get("jobs")`，可有可无的查找 | [`tool-subagent/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/subagent/tool-subagent/src/index.ts)（第 402 到 405 行） | 真正的委派 tool 是用 `ctx.get('jobs')` 拿到 jobs，不是 `inject`：没挂 registry 就是没有后台模式，绝不会偷偷退回前台跑。 |
| `subagent` 这个 tool | [`packages/subagent/tool-subagent/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/subagent/tool-subagent/src/index.ts) | 出货的 Consumer；连它的 tool 名字都可以设置，因为 model 看到的 schema 属于 Consumer，永远不属于 Provider。 |

真正的 subagent 这一层，在这个 Section 的 Mechanism 之上，还多做了这些：

- **可以接着用的 child。** `startContinuable()` 加上一个续接管理器，让 child 可以跨 turn 活着，parent 在两个 turn 之间也找得到它。照 [`run-settlement.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/subagent/subagent/src/run-settlement.ts)（第 2 到 4 行），只有一次性的后台模式会碰 jobs；可以接着用的 child 完全不经过 registry。这种 subagent 在 Mini-dsh 的 Ceiling 之上：只在这里指给你看，没有做。
- **一整排 Provider。** `subagent-spawn-in-process`、`subagent-fork-in-process`、`subagent-acp`、`subagent-codex`、`subagent-claude-code`、`subagent-dsh-sdk`：选接口而不选继承这个主张，在这里变成看得到的东西。其中好几个背后根本连一个 `Agent` 都没有，这就是 registry 从来不开口要 Agent 的原因。
- **启动成功的那一刻，拥有权转手。** 一次启动成功之后，child 的拥有权就转给 parent，所以 parent 一死，它委派出去的东西也跟着陪葬；mini 的 child 则是跟着整个 process 一起生、一起死。
- **更忙的 runtime。** bus 事件（`subagent/provider-added`、`subagent/provider-removed`、`subagent/start`、`subagent/end`，在 runtime 的第 134 到 167 行）、descriptor 快照、找出所有后代的能力，加上三个包、五个 tool 名字组成的 Consumer 这一面：`subagent` 是这个 Section 做的那个，另外还有给还活着的 child 用的 `send_message`、`interrupt_agent`、`list_agents` 和 `report`。这些全都住在 runtime 和它的 Consumer 里，所以 Provider 可以一直薄得跟那个接口一样。

---

## Failure modes

- **拿子类当契约，等于把 Provider 的名单封顶。** 只要规定一定要是 `Subagent(Agent)`，那每一个 Provider 都得是这个 process 里的 agent；fork 出去的、远程的、别的产品的那些 Provider，不是根本不能存在，就是得假扮成 Agent 的内部长相才挂得上去。四个动词只要求 parent 真正会做的事：开始、停止、等待、读取。
- **跟 parent 共用 session 的 child，不叫委派。** 把这趟腿的每一行都写进 parent 的 log，它的完整对话记录就会永远跟在 parent 的 context 里，而这正是委派要躲掉的那笔代价。两份 log，一个答案跨过来：parent 留下一次调用和一条结果，child 留下其他全部。
- **没有 cancel 的 run，就是一个没人叫得停的 child。** 前台至少还能等它跑完；但一个后台的 child，如果三元组里没有 `cancel`，`job_kill` 就是在说谎，它把结果定成“killed”，工作却还在跑。三元组把停止键一起带着，就是为了让 Section 11 的认人背后真的有东西。
- **不认识的名字直接抛异常，会把对话记录撕破。** 没有人注册过的名字丢出来的 `LookupError`，必须从 Section 05 的 pipeline 走出去，变成一条正常的 `is_error` 结果；放它逃出去，model 问的问题就没有答案，重放也会在同一行断掉。
- **偷偷退回前台，会让后台模式变成一句谎话。** 没挂 job registry 的时候，安静地就地把任务跑完，等于把 turn 卡住，而且卡的时间刚好就是 model 想避开的那段等待，还连一个可以拿来杀的 id 都没有。tool 选择大声拒绝，而这次拒绝是一条普通的结果，model 可以绕过它另外想办法。

---

## 跑跑看

[`src/`](src/) 把 11 搬过来，再加上：

- [`subagent.py`](src/subagent.py)（新增）：`SubagentRuntime` 这份 registry、`SubagentRun` 这份契约、同一个 process 里的那个 Provider，还有 `subagent_tools(owner)` 这个 plugin 工厂，把两种模式都有的委派 tool 挂上去。
- [`test.py`](src/test.py)：Offline check 证明几件事：一次前台委派会拿 child 自己 session 里的回复当答案；同一个 tool 后面的两个 Provider 可以互换，就算其中一个根本不是 agent；不认识的名字会变成一条正常的错误结果；一次后台委派就是一个普通的 job，它的通知和控制用 tool 一行新代码都不用写；没挂 job registry 的后台模式会大声拒绝；child 炸掉也会变成一条正常的错误结果。
- [`demo.py`](src/demo.py)：Live demo 委派给一个对着真 API 跑的 child，把它的答案引述出来，再把第二个 child 丢到后台，让它的完成通知把 parent 叫醒。

```bash
python sections/12-subagent/src/test.py    # offline check, no key
```

Live demo 需要根目录的 `requirements.txt` 和一把 key；没有 key 的话，它会安静地跳过：

```bash
pip install -r requirements.txt         # anthropic + python-dotenv
cp .env.example .env                    # then set ANTHROPIC_API_KEY
python sections/12-subagent/src/demo.py
```

---

## 出处

- [`docs/subsystems/subagent.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/subsystems/subagent.md)：委派这一层的子系统文档：Provider 的接口、runtime，还有 Consumer 那几个 tool。
- [`packages/subagent/subagent/src/run-settlement.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/subagent/subagent/src/run-settlement.ts)：三种委派模式（前台、一次性后台、可以接着用），还有只有后台模式会碰 jobs 的证据。
