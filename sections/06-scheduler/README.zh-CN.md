<!-- source: README.md @ 3705bd7 -->

# 06 · Scheduler

[English](README.md) | [繁體中文](README.zh-TW.md) | 简体中文

> 逐一运行工具调用会浪费等待时间，但若依照完成先后写入 log，同一个 turn 每次运行都可能产生不同的记录。因此，工具可以并行运行，但 log 必须保持固定的写入顺序。

第 05 章使用 for loop 依序运行回复中的工具调用。当每次回复只有一个调用时，这个限制不明显；但模型往往会一次要求多个工具。例如同时读取三份笔记，若每次读取需要一秒，串行运行就必须等待三秒。

把所有调用丢进线程池并不难，但如果哪个先完成就先 append，log 顺序就会受调度影响。同一个 turn 重复运行可能产生不同记录，重放也失去确定性。此外，具有依赖关系的读写操作不能随意重叠，而 turn 取消时，那些尚未开始的调用也仍然需要对应结果，否则对话历史会再次出现缺口。

因此，本章要回答的问题是：为什么可并行的调用会重叠运行，互斥调用会形成 barrier，而在 dispatch 前中止的调用也会收到合成结果？

并行化不能牺牲第 05 章创建的对话完整性。scheduler 必须遵守以下原则：

1. 先写 log，再开始运行：所有 `tool/call` 都要在 dispatch 前 append；`tool/result` 则固定依模型给出的调用顺序写入，不受线程完成顺序影响。
2. 是否可并行由工具自行声明：工具必须通过 `is_concurrency_safe` 主动标示，未标示时一律视为互斥，因为只有实现者知道它会碰触哪些共享资源。
3. 只有同一批安全调用会并行：连续的安全调用会一起 dispatch；互斥调用各自形成一批，也就是一道 barrier，前一批完成后才能开始下一批。
4. 已开始的工作不会半途取消：取消只在两批之间生效，已 dispatch 的实现仍会运行完毕。
5. 被略过的调用也必须有结果：在 dispatch 前遭中止的调用会取得合成错误，确保对话历史中的每个调用都有对应回复。
6. session log 只有一个写入者：loop 线程负责 append，worker thread 只运行 pipeline 并返回结果。

---

## 核心机制

本章添加 `scheduler.py`，并让 loop 的工具分支改由 scheduler 处理：

- **`execute_tool_calls(session, tools, calls, aborted)`**：负责推进 prepare、dispatch、finalize、finish 四个阶段。
- **`_batches(plan)`**：实现分批规则。连续的安全调用放在同一批，互斥调用则各自成批。
- `ToolDefinition` 的 **`is_concurrency_safe`**：registry 与 scope 通过 `is_safe()` 查找，因此同名工具覆写后，安全属性也会一起套用新的定义。
- **`Agent.cancel()`**：每个 turn 都有一个 `threading.Event`。scheduler 在每批开始前检查它；遭中止的 step 以 `"aborted"` 结束，turn 也随之结束。

每一个调用都走同样的四个阶段：

1. **prepare**：依模型给出的顺序先为每个调用写入 `tool/call`，再通过 `is_safe()` 取得安全判定。找不到名称的工具一律视为互斥。
2. **dispatch**：逐批交给 worker thread。每批开始前先检查 turn 是否已中止；若已中止，就不再派送后续工作。
3. **finalize**：loop 线程等待该批所有 future 完成。即使 turn 此时被取消，已开始的工作仍会运行完毕。
4. **finish**：依模型给出的顺序，为每个调用写入一笔 `tool/result`。从未 dispatch 的调用会取得合成结果：`{"is_error": true, "content": "aborted before dispatch"}`。

```text
reply: a (safe)   b (safe)   c (exclusive)   d (safe)

prepare   tool/call a, b, c, d   ◄ four rows, model order, nothing running
dispatch  batch [a b]   a ═══════════╗
                        b ═══════╗   ║   safe calls overlap
finalize                ── barrier ──┘
dispatch  batch [c]     c ═══════╗       exclusive: a batch of one
finalize                ── barrier
dispatch  batch [d]     d ═══╗
finalize                ── barrier
finish    tool/result a, b, c, d ◄ model order, though b finished before a
```

写成代码，四个阶段读起来也是同一个顺序：

```python
def execute_tool_calls(session, tools, calls, aborted):
    # prepare: a log row and a safety verdict per call, before anything runs
    plan = [(index, call, tools.is_safe(call)) for index, call in enumerate(calls)]
    for _index, call, _safe in plan:
        session.append("tool/call", call)  # log-only: before dispatch
    outcomes = {}  # index -> result dict, filled as batches finalize
    with ThreadPoolExecutor(max_workers=max(1, len(plan))) as pool:
        for batch in _batches(plan):
            # dispatch: a batch starts only if nothing has aborted the turn
            if aborted.is_set():
                break
            futures = [
                (index, pool.submit(tools.execute, call))
                for index, call, _safe in batch
            ]
            # finalize: the barrier; started work is never abandoned
            for index, future in futures:
                outcomes[index] = future.result()
    # finish: one result per call, model order; skipped calls answer too
    for index, call, _safe in plan:
        if index not in outcomes:  # never dispatched: answer anyway
            outcomes[index] = {
                "call_id": call.get("id"),
                "name": call.get("name"),
                "is_error": True,
                "content": ABORTED_BEFORE_DISPATCH,
            }
        session.append("tool/result", outcomes[index])
```

第 05 章那条 pipeline 完全没动：工作线程照样调用 `tools.execute(call)`，每个出口照样是一个 result。变的是谁负责 append。scheduler 跑在 loop 那条线程上，是 log 唯一的写入者；工作线程只算出 result dict，其他什么都不做，所以这份只能追加的 log 永远不需要上锁。

下面是一个被取消的 turn，log 是这样记的。`stop` 的实现是在一批跑到一半的时候，从自己的工作线程里调用 `agent.cancel()`：

```text
send("stop everything")
  │   7  assistant/message {"tool_calls": [stop, sibling, late, last]}
  │   8  tool/call    stop       ◄ all four rows before dispatch
  │   9  tool/call    sibling
  │  10  tool/call    late
  │  11  tool/call    last
  │  12  tool/result  stop     {"is_error": false, "content": "stopping"}
  │  13  tool/result  sibling  {"is_error": false, "content": "kept running"}
  │  14  tool/result  late     {"is_error": true,
  │                             "content": "aborted before dispatch"}
  │  15  tool/result  last     {"is_error": true,
  │                             "content": "aborted before dispatch"}
  │  16  step/end     {"reason": "aborted"}
  │  17  turn/end
```

`sibling` 已经 dispatch，因此仍会运行完毕。barrier 后的两个调用从未开始，但 finish 仍会为它们产生结果。如此一来，推导出的历史中每个调用都有回复，重放时也能还原取消状态。

### 改了什么

与第 05 章相比：

- `kernel.py`、`message.py`、`session_log.py`、`standin.py` 都完整沿用。`scheduler.py` 是唯一添加的源文件；其他改动都是把 scheduler 这条线穿过原本就有的文件，因此与第 05 章相比，diff 只包含本章添加的机制，不包含其他改动。
- `tools.py`：`ToolDefinition` 多了 `is_concurrency_safe`（默认 `False`），registry 和 scope 多了 `is_safe()`。pipeline 本身完全没动。
- `agent_loop.py`：原本一个一个跑完回复里调用的那个 for 循环，变成调用一次 `execute_tool_calls`。Agent 多了 `cancel()` 和每个 turn 一个的中止事件，而一个 step 现在可以用 `"aborted"` 这个理由结束。
- 一个回复带多个调用的时候，log 的形状变了：现在所有 `tool/call` 都会落在第一个 `tool/result` 之前（送出去跑之前就写好），而不是像以前那样一个调用配一个结果交错着写。
- `demo.py`：在线示例会注册一个可以平行跑的读取和一个互斥的写入，两个都故意跑得很慢，再把每个实现实际开始和结束的时间印出来，让你在时钟上就看得到它们叠在一起。

---

## 对照真正的 dsh

以下链接皆指向研究版本 [`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca)。scheduler 位于 loop 那个软件包里，不在 tool runtime 里：[`packages/core/agent-loop`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-loop)。

| Mini-dsh | 真正的 dsh | 说明 |
| --- | --- | --- |
| `scheduler.py` 里的 `execute_tool_calls` | [`packages/core/agent-loop/src/tool-calls.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-loop/src/tool-calls.ts)：`executeToolCalls` | loop 不会直接拿回复里的调用去跑 `ctx.tools.execute()`；推动它们的是 `executeToolCalls`，跑的一样是 `prepare / dispatch / finalize / finish` 这个四阶段的 scheduler。 |
| `is_concurrency_safe` | [`packages/core/tools/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/tools/src/index.ts)：`ToolDefinition` | `ToolDefinition.isConcurrencySafe`，每个 tool 自己声明；tool 没说话就是互斥。 |
| 那个合成出来的结果 | [`packages/core/tools/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/tools/src/index.ts)：`TOOL_ABORTED_BEFORE_DISPATCH` | 这是跟 `TOOL_ABORTED` 不一样的错误码，这样光看对话记录就分得出来，一个调用是被跳过的，还是跑到一半被打断的。 |
| `Agent.cancel()` + `threading.Event` | [`packages/core/agent/src/runtime-types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent/src/runtime-types.ts)：`Agent.cancel` | 真正的取消，是把一串 abort signal 融在一起，穿过整个 runtime；mini 只留每个 turn 一个事件，在每一批的边界上检查。 |
| finish 照 model 给的顺序 append | [`packages/core/agent-loop/src/tool-calls.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-loop/src/tool-calls.ts) | 结果是在 loop 里变成 session 事件的，不是在 registry 里；`tool/result` 事件还会带 `sourceEventSeqs`，把每个答案接回它对应的那几行，而 mini 靠的是 `call_id`。 |
| 那个 `ThreadPoolExecutor` | [`packages/core/tools/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/tools/src/index.ts)：`TOOL_RUNTIME_SCHEDULER` | runtime 是通过一个具名的 seam 去拿它的 scheduler，而不是写死一个 pool。 |

真正的 scheduler 还提供以下功能：

- **用合作的方式中止已经开跑的调用。** `TOOL_ABORTED` 是给送出去之后才被打断的调用用的：融在一起的 signal 会传进实现里面，而 timeout policy（[`packages/guard/timeout-policy`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/guard/timeout-policy)）会帮 `tools/execute` 加上一个期限，同时不会把 tool 的 promise 丢在那里不管。mini 根本不会去打断已经开跑的实现，所以它只有送出去之前的那一种中止码。
- **提早结束的方式更多。** 一个 result 可以带 `concludesTurn`，让 turn 提早结束。mini 唯一的提早出口是 `cancel()`。
- **从头到尾都是 async。** dsh 的 tool 实现是 async 的，所以叠在一起跑这件事，是在同一条线程里靠 promise 完成的；mini 的实现是普通的 Python callable，所以它是用一个线程池换到同样的重叠。
- **按下取消的是人。** 在真正的 dsh 里，取消通常是从 UI 来的，而 UI 不在本教学的实现范围内；mini 就把 `cancel()` 开成一个普通的方法，而 离线测试是从一个 tool 的实现里面按下去的。

---

## 常见失败模式

- **依完成顺序 append 会让 log 不稳定。** 如果 worker thread 完成后自行写入，同一个 turn 每次运行都可能得到不同的事件顺序。finish 改由 loop 线程依模型原始顺序 append，并行差异只反映在时间上，不会改变记录。
- **若要求工具自行标示互斥，默认值就不安全。** 实现者一旦忘记标示，共享状态便可能同时被修改。默认互斥最多只会牺牲性能；测试中的 `solo` 会确认未标示工具确实单独运行。
- **强制中断已开始的工作可能留下不完整副作用。** 写档工具若在中途被终止，可能留下半成品。scheduler 只阻止新批量开始；已 dispatch 的工作会完成并返回结果。
- **被略过的调用若没有结果，对话历史就不完整。** assistant 消息已列出所有调用，因此即使某些调用未开始，也必须产生合成结果。这延续第 05 章「每个调用都有回复」的规则。
- **让 worker thread 写 log 会增加同步复杂度。** prepare 与 finish 都在 loop 线程运行，worker thread 只负责计算结果，因此 session log 不需要为并行工具额外加锁。
- **安全判定在 prepare 后固定不变。** 即使工具在批量运行期间卸载，原计划中的位置仍保留；它不是已运行完毕，就是会得到明确结果。若运行中途重新查找，反而会让计划受到挂载时序影响。

---

## 动手验证

[`src/`](src/) 延续第 05 章，并加入：

- [`scheduler.py`](src/scheduler.py)（添加）：`execute_tool_calls`，把四个阶段一路推完的那支函数，还有 `_batches`，分批的规则。
- [`tools.py`](src/tools.py)：`ToolDefinition` 上的 `is_concurrency_safe`，registry 和 scope 上的 `is_safe()`。
- [`agent_loop.py`](src/agent_loop.py)：处理 tool 的那条分支改走 scheduler；Agent 多了 `cancel()` 和每个 turn 一个的中止事件；一个 step 现在可以用 `"aborted"` 结束。
- [`test.py`](src/test.py)：两个安全的调用要一起通过一道关卡，而那道关卡只有真的叠着跑才过得了，用这个证明它们真的重叠了；一个没标的 tool 夹在它们中间，自己一个人跑；就算快的那个先跑完，结果还是照 model 给的顺序落下；而一批跑到一半按下取消，已经开跑的会跑完，没开始的会拿到合成出来的结果，下一个 turn 从干净的状态重新开始。
- [`demo.py`](src/demo.py)：在线示例会先要两次可以平行跑的查找，再要一次互斥的保存，并且把每个实现实际开始和结束的时间，连同 log 中的完整运行记录一起印出来。

```bash
python sections/06-scheduler/src/test.py    # offline check, no key
```

在线示例需要根目录的 `requirements.txt` 和一把 key；没有设置 key 时会自动跳过：

```bash
pip install -r requirements.txt             # anthropic + python-dotenv
cp .env.example .env                        # then set ANTHROPIC_API_KEY
python sections/06-scheduler/src/demo.py
```

---

## 参考资料

- [`docs/tool-execution-pipeline.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/tool-execution-pipeline.md)：dsh 自己写的文档，讲 scheduler 推动的那条运行 pipeline。
- [`docs/subsystems/core.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/subsystems/core.md)：`executeToolCalls` 所在的那个 loop 软件包。
- [`docs/agent-lifecycle.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/agent-lifecycle.md)：tool 的运行在一个 turn 里面坐在什么位置，取消也一起讲。
