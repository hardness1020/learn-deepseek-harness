<!-- source: README.md @ 3705bd7 -->

# 12 · Subagent

[English](README.md) | [繁體中文](README.zh-TW.md) | 简体中文

> 被委派的支线任务不应该占满 parent 的 context。如果用 Agent 子类代表 subagent，就假设实际运行者一定在相同 process 内，但真实 provider 可能来自另一个 process、远程服务或其他 harness。因此 parent 只依名称启动 provider，并取得统一的 run 接口。

第 11 章已经让 Mini-dsh 可以运行背景工作，但所有推理仍共用同一个 context window。如果 parent 负责摘要软件包或追查失败测试，这些支线任务的完整对话都会留在 parent 历史中，挤压主任务可用的 context。委派的做法是为 child 建立独立 session、工具作用域与 context，parent 最后只接收一个结果。

由于第 04 章的 `Agent` 已能运行 turn，直觉做法是定义 `class Subagent(Agent)`。但委派的实际运行者不一定是相同 process 中的 agent。真正 dsh 的 provider 可能 fork 新 process、通过传输协议驱动其他产品，或包装另一套 harness。如果用基类定义契约，这些 provider 都必须假装拥有 `Agent` 的内部结构，才能加入 registry。

因此，本章要回答的问题是：为什么 subagent 接口应定义为「启动 child，返回一个 run」，而不是继承 `Agent`？

parent 真正需要的契约非常小，而继承会同时带入大量不必要的假设。本章依下列原则实现：

1. 依名称注册 Provider：同一个 ctx key 下维护 registry，每次注册都返回撤销动作。
2. Provider 只需符合 callable 契约：接收已解析的启动请求并返回 run；registry 不假设背后如何运行。
3. run 是 parent 需要的完整接口，只包含 `cancel`、`done`、`read_output`，沿用第 11 章的协议三元组。
4. 前景模式会在工具调用内等待 `done`，再将 child 回复作为结果。
5. 背景模式将同一个三元组交给 job registry，让 subagent 成为与 shell 对等的生产者，并直接共用第 11 章的控制工具。
6. 所有失败都经过第 05 章的工具 pipeline：未知名称、缺少 job registry 或 child 运行失败，都会转成一般 `is_error` 结果。

---

## 核心机制

只添加一个文件 `subagent.py`，前面沿用的文件都没有修改：

- **`SubagentRuntime`**：ctx key 为 `"subagents"` 的 service，由 `subagent_plugin` 挂载。它以名称管理 Provider；`start()` 解析名称、组合请求，再直接返回 Provider 创建的 run。
- **`SubagentRun`**：parent 这一侧的契约，一个不可变的三元组。
- **`in_process_provider(ctx, model_factory)`**：这只是其中一个 Provider，不是契约本身。它使用 parent 可用的 service 创建 child `Agent`。
- **`subagent_tools(owner)`**：一个 plugin 工厂，把唯一那个 `subagent` tool 挂进拥有者的作用域，拥有者的身份写死在里面。

registry 可以保持精简，是因为契约本身只要求：任何 callable 只要能将已解析请求转成 run，就能作为 Provider，完全不需要继承 Agent：

```python
def start(self, name, task):
    """Resolve the name, hand the provider a resolved request, get a run."""
    provider = self._providers.get(name)
    if provider is None:
        raise LookupError(f"no subagent provider registered under '{name}'")
    self._count += 1
    return provider({"id": f"sub-{self._count}", "task": task})
```

返回值只有这个 run，并刻意沿用第 11 章的协议三元组，完整涵盖 parent 对外部工作的三项需求：取消、等待完成，以及读取输出。

```python
@dataclass(frozen=True)
class SubagentRun:
    cancel: callable  # ask the child to stop; cooperative, best effort
    done: callable  # block until it ends: ("completed", None) | ("failed", detail)
    read_output: callable  # the child's answer so far, as text
```

同一个 process 内的 Provider 能说明为何契约可以这么薄。它使用 parent 可用的 `sessions`、`agents`、`tools` service 创建 child，在 run 专属线程中调用 `send()`，再从 child log 读取答案。完整流程保留在 child 自己的 session，parent 只接触 run。即使其他 Provider 从缓存、子 process 或另一套产品取得结果，只要返回同一组三元组，registry 与工具就不需要区分来源。

Consumer 通过同一个工具支持前景与背景两种委派模式，两者共用完全相同的 run 接口：

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

前景模式直接等待完成；背景模式则将 run 包成生产者协议交给第 11 章，后续 id、拥有者验证、最终状态与 inbox 通知都由 job registry 管理。job registry 通过可选查找取得，因此未挂载 jobs 的 harness 会明确拒绝背景委派，不会无声改成阻塞的前景模式。

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

以下是一次前景委派，以及 parent 与 child 各自的 log。parent 只保留一笔调用和一笔结果，child 的完整运行过程则留在自己的 session：

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

换成背景模式，同一个 run 改搭第 11 章：parent 的 turn 收在 `"started job-1"` 上，child 在 parent 闲着的时候思考，通知再以一个 followup 的 turn 到达，在那里 `job_output` 给出 child 的回复，`job_list` 报出来的种类是 `subagent`。控制用的 tool 没有变，变的是生产者。

### 改了什么

与第 11 章相比：

- 所有既有文件都完整沿用：`agent_loop.py`、`capabilities.py`、`inbox.py`、`jobs.py`、`kernel.py`、`message.py`、`scheduler.py`、`session_log.py`、`skills.py`、`standin.py`、`system_prompt.py`、`tools.py`。`subagent.py` 是唯一添加的源代码文件，因此与第 11 章相比，diff 只包含本章添加的机制，不包含其他改动。
- 这项机制是纯粹的组合：child 是通过第 02 章的 sessions、第 04 章的 agents、第 05 章的 tool 这几个 service 开出来的；run 就是第 11 章的协议三元组；背景模式把这组三元组交给 job registry，让 subagent 成为第 11 章早就预告过的第二个生产者。
- log 没有添加任何事件类型。一次委派在 parent log 中只包含一笔 `tool/call` 与一笔 `tool/result`；其余运行记录都保存在 child 自己的 session。
- `demo.py`：在线示例在前景委派给一个对着实际 API 跑的 child，把它的答案引述出来，接着再把第二个 child 丢到背景，让它的完成通知在一个 parent 没要求过的 turn 里把 parent 叫醒。

---

## 对照真正的 dsh

以下链接皆指向研究版本 [`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca)。这一层对应的软件包系列是 [`packages/subagent`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/subagent)。

| Mini-dsh | 真正的 dsh | 说明 |
| --- | --- | --- |
| `SubagentRuntime`，ctx key `"subagents"` | [`packages/subagent/subagent/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/subagent/subagent/src/index.ts)：`SubagentRuntime` | 这个 runtime（第 171 行）是一个具体的 `Service`，与第 11 章那个抽象的 `JobRegistry` 不一样：它守的 seam 是 Provider 的接口，不是 registry 本身。 |
| Provider 是一份 callable 的契约 | [`packages/subagent/subagent/src/types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/subagent/subagent/src/types.ts)：`SubagentProvider` | 这个设计问题直接写在类型系统里：`SubagentProvider`（第 285 行）是一个 TS 接口，不是 `Service`，也不是 `Agent` 的子类；任何能把解好的启动请求变成一个 `SubagentRun` 的东西都算数。 |
| `in_process_provider` | [`packages/subagent/subagent-in-process-driver/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/subagent/subagent-in-process-driver/src/index.ts) | 第 132 行是同一招：child 是用 `parent.ctx.agents.create()` 建出来的，走的是第 04 章那道普通的门，不是什么私有的建构子。 |
| 背景模式下交给 jobs 的那个 run | [`packages/subagent/subagent/src/run-settlement.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/subagent/subagent/src/run-settlement.ts)、[`packages/subagent/tool-subagent/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/subagent/tool-subagent/src/index.ts)（第 408 到 423 行） | 一次性的背景委派就是 `jobs.start({kind: 'subagent', ...})`：`JobKindMap` 里的第二个种类，跟 `bash` 平起平坐，正是本章重建的那次交棒。 |
| `jobs = ctx.get("jobs")`，可有可无的查找 | [`tool-subagent/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/subagent/tool-subagent/src/index.ts)（第 402 到 405 行） | 真正的委派 tool 是用 `ctx.get('jobs')` 拿到 jobs，不是 `inject`：没挂 registry 就是没有背景模式，绝不会偷偷退回前景跑。 |
| `subagent` 这个 tool | [`packages/subagent/tool-subagent/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/subagent/tool-subagent/src/index.ts) | 出货的 Consumer；连它的 tool 名字都可以设置，因为 model 看到的 schema 属于 Consumer，永远不属于 Provider。 |

真正的 subagent 这一层还提供以下功能：

- **可以接着用的 child。** `startContinuable()` 加上一个续接管理器，让 child 可以跨 turn 活着，parent 在两个 turn 之间也找得到它。照 [`run-settlement.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/subagent/subagent/src/run-settlement.ts)（第 2 到 4 行），只有一次性的背景模式会碰 jobs；可以接着用的 child 完全不经过 registry。这种 subagent 在 Mini-dsh 的实现范围之外：只在这里指给你看，没有做。
- **多种 Provider 实现。** `subagent-spawn-in-process`、`subagent-fork-in-process`、`subagent-acp`、`subagent-codex`、`subagent-claude-code`、`subagent-dsh-sdk` 展示了接口优于继承的好处，其中部分实现背后甚至没有 `Agent`，因此 registry 不应要求 Agent 类型。
- **启动成功后，拥有权转交给 parent。** parent 结束时，它启动的 child 也会一并终止；Mini-dsh 的简化版则让 child 与整个 process 共用生命周期。
- **更忙的 runtime。** bus 事件（`subagent/provider-added`、`subagent/provider-removed`、`subagent/start`、`subagent/end`，在 runtime 的第 134 到 167 行）、descriptor 快照、找出所有后代的能力，加上三个软件包、五个 tool 名字组成的 Consumer 这一面：`subagent` 是本章实现的那个，另外还有给还活着的 child 用的 `send_message`、`interrupt_agent`、`list_agents` 和 `report`。这些全都位于 runtime 和它的 Consumer 里，所以 Provider 可以一直薄得跟那个接口一样。

---

## 常见失败模式

- **以子类作为契约会限制 Provider 类型。** 若规定必须是 `Subagent(Agent)`，fork、远程服务或其他产品都得仿真本地 Agent 内部结构。run 接口只要求 parent 真正需要的开始、停止、等待与读取能力。
- **child 与 parent 共用 session 会失去委派的意义。** child 的完整对话若都写入 parent log，就会持续占用 parent context。两者应使用独立 log，只有最终答案跨回 parent。
- **缺少 cancel 的 run 无法真正支持背景取消。** `job_kill` 即使将状态设为 `killed`，实际工作仍可能继续运行。三元组必须包含停止方法，让状态与实际运行一致。
- **未知名称的例外若穿过工具边界，会留下不完整调用。** `LookupError` 必须由第 05 章的 pipeline 转成 `is_error` 结果，确保模型收到对应回复，重放也能继续。
- **缺少 job registry 时不能无声退回前景模式。** 否则背景请求会意外阻塞 turn，也没有可取消的 id。工具应明确返回错误，让模型决定其他做法。

---

## 动手验证

[`src/`](src/) 延续第 11 章，并加入：

- [`subagent.py`](src/subagent.py)（添加）：`SubagentRuntime` 这份 registry、`SubagentRun` 这份契约、同一个 process 里的那个 Provider，还有 `subagent_tools(owner)` 这个 plugin 工厂，把两种模式都有的委派 tool 挂上去。
- [`test.py`](src/test.py)：离线测试证明几件事：一次前景委派会拿 child 自己 session 里的回复当答案；同一个 tool 后面的两个 Provider 可以互换，就算其中一个根本不是 agent；不认识的名字会变成一则正常的错误结果；一次背景委派就是一个普通的 job，它的通知和控制用 tool 一行新代码都不用写；没挂 job registry 的背景模式会大声拒绝；child 炸掉也会变成一则正常的错误结果。
- [`demo.py`](src/demo.py)：在线示例委派给一个对着实际 API 跑的 child，把它的答案引述出来，再把第二个 child 丢到背景，让它的完成通知把 parent 叫醒。

```bash
python sections/12-subagent/src/test.py    # offline check, no key
```

在线示例需要根目录的 `requirements.txt` 和一把 key；没有设置 key 时会自动跳过：

```bash
pip install -r requirements.txt         # anthropic + python-dotenv
cp .env.example .env                    # then set ANTHROPIC_API_KEY
python sections/12-subagent/src/demo.py
```

---

## 参考资料

- [`docs/subsystems/subagent.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/subsystems/subagent.md)：委派这一层的子系统文档：Provider 的接口、runtime，还有 Consumer 那几个 tool。
- [`packages/subagent/subagent/src/run-settlement.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/subagent/subagent/src/run-settlement.ts)：三种委派模式（前景、一次性背景、可以接着用），还有只有背景模式会碰 jobs 的证据。
