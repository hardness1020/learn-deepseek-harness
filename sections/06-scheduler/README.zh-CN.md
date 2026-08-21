<!-- source: README.md @ 8d86583 -->

# 06 · Scheduler

[English](README.md) | [繁體中文](README.zh-TW.md) | 简体中文

> 安全的调用叠在一起跑，互斥的调用自己站一边。现实世界爱照什么顺序结束，就照什么顺序，log 永远不会。

Section 05 是用一个 for 循环把回复里的调用跑完的：一个调用，一个答案，换下一个。以前每个回复只带一个调用，所以看不出差别。但真正的回复会一次要一批：你叫 model 去读三条笔记，它会一次全部要，而那个一个一个跑的循环，会把三次各一秒的读取变成三秒的等待。

最直觉的做法，是把每个调用全部丢给一个线程池，谁先回来就先 append 谁的结果。但这样一来，log 的顺序就要看线程的快慢了：同一个 turn 跑两次会得到两份不一样的对话记录，重放也就不再是重建，而是在赌谁先谁后。一个写入如果跟喂数据给它的那个读取叠在一起跑，它会读到一半新一半旧的东西。至于一个 turn 在半路被取消的时候，那些根本没开始的调用，正是 assistant 消息已经问出口的问题：Section 05 那份被撕开的对话记录，换一条路又走回来了。

所以：为什么可以并行跑的调用会叠在一起跑，互斥的调用会卡成一道关卡，而还没开始就被中止的调用会拿到一个合成出来的结果？

因为速度是要付代价的，但这个代价不能是 Section 05 那份契约，也就是对话记录。要做到这件事，scheduler 必须：

1. 先写 log，再开跑：任何东西送出去跑之前，每个调用的 `tool/call` 那一行都已经 append 好了；而每个 `tool/result` 落下的顺序都是 model 给的顺序，不管线程是照什么顺序结束的。
2. 安不安全这件事，由 tool 自己声明：要用 `is_concurrency_safe` 主动表态，默认一律互斥，因为只有写这个 tool 的人才知道它的实现碰了什么。
3. 只在同一批里面才叠着跑：连在一起的安全调用会一起送出去；互斥的调用自己就是一批，也就是一道关卡：排在它前面的要先跑完，排在它后面的要等。
4. 已经开跑的事情绝不半途丢下：取消是在两批之间才生效，已经送出去的实现会让它跑到自己结束。
5. 连跳过的调用也要回答：还没开始就被中止的调用会拿到一个合成出来的错误结果，因为重放出来的对话记录里，每一个问题都得有它的答案。
6. 只留一个写入者：只有在跑 loop 的那条线程能 append 进 session log；工作线程负责跑 pipeline，然后把结果交回来。

---

## Mechanism

一个新文件 `scheduler.py`，另外把 loop 处理 tool 的那条分支改道，绕过它走：

- **`execute_tool_calls(session, tools, calls, aborted)`**：整件事就是它在推。四个阶段：prepare、dispatch、finalize、finish。
- **`_batches(plan)`**：分批的规则。连在一起的安全调用共用一批；互斥的调用自己站一边。
- `ToolDefinition` 上的 **`is_concurrency_safe`**，通过 registry 和 scope 上的 `is_safe()` 查出来，所以同名盖掉这件事，套在安全与否上，跟套在其他东西上一模一样。
- **`Agent.cancel()`**：每个 turn 一个 `threading.Event`。scheduler 在每一批开始之前都会看它一眼；被砍掉的 step 会以 `"aborted"` 这个理由结束，turn 也跟着收掉。

每一个调用都走同样的四个阶段：

1. **prepare**：照 model 给的顺序，每个调用先拿到自己那一行 `tool/call`（只进 log，而且在任何东西开跑之前），再从 `is_safe()` 拿到一个安全判定。名字查不到的一律算互斥。
2. **dispatch**：一批一批送给工作线程。每一批开始前只问一句：这个 turn 有没有被中止？有的话就不再送了。
3. **finalize**：关卡就在这里。跑 loop 的那条线程会等这一批的每一个 future；已经开跑的事情绝不半途丢下，就算取消了也一样。
4. **finish**：每个调用一行 `tool/result`，照 model 给的顺序写。从来没被送出去的调用会拿到一个合成出来的结果：`{"is_error": true, "content": "aborted before dispatch"}`。

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

Section 05 那条 pipeline 完全没动：工作线程照样调用 `tools.execute(call)`，每个出口照样是一个 result。变的是谁负责 append。scheduler 跑在 loop 那条线程上，是 log 唯一的写入者；工作线程只算出 result dict，其他什么都不做，所以这份只能追加的 log 永远不需要上锁。

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

sibling 已经送出去了，所以它一路跑到自己结束。关卡后面那两个调用从来没开始，finish 还是替它们回答了。把历史推导出来，每一个问题都有它的答案：重放出来的还是同一个故事，连取消都一起还原。

### 改了什么

跟 Section 05 比起来：

- `kernel.py`、`message.py`、`session_log.py`、`standin.py` 都原封不动搬过来。`scheduler.py` 是唯一新增的源文件；其他改动都是把 scheduler 这条线穿过原本就有的文件，所以跟 05 的 diff 就是这个 section 的 Mechanism，没有别的。
- `tools.py`：`ToolDefinition` 多了 `is_concurrency_safe`（默认 `False`），registry 和 scope 多了 `is_safe()`。pipeline 本身完全没动。
- `agent_loop.py`：原本一个一个跑完回复里调用的那个 for 循环，变成调用一次 `execute_tool_calls`。Agent 多了 `cancel()` 和每个 turn 一个的中止事件，而一个 step 现在可以用 `"aborted"` 这个理由结束。
- 一个回复带多个调用的时候，log 的形状变了：现在所有 `tool/call` 都会落在第一个 `tool/result` 之前（送出去跑之前就写好），而不是像以前那样一个调用配一个结果交错着写。
- `demo.py`：Live demo 会注册一个可以并行跑的读取和一个互斥的写入，两个都故意跑得很慢，再把每个实现实际开始和结束的时间打印出来，让你在时钟上就看得到它们叠在一起。

---

## In real dsh

所有指过去的链接都固定在 Studied version [`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca) 上。scheduler 住在 loop 那个包里，不在 tool runtime 里：[`packages/core/agent-loop`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-loop)。

| Mini-dsh | 真正的 dsh | 说明 |
| --- | --- | --- |
| `scheduler.py` 里的 `execute_tool_calls` | [`packages/core/agent-loop/src/tool-calls.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-loop/src/tool-calls.ts)：`executeToolCalls` | loop 不会直接拿回复里的调用去跑 `ctx.tools.execute()`；推动它们的是 `executeToolCalls`，跑的一样是 `prepare / dispatch / finalize / finish` 这个四阶段的 scheduler。 |
| `is_concurrency_safe` | [`packages/core/tools/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/tools/src/index.ts)：`ToolDefinition` | `ToolDefinition.isConcurrencySafe`，每个 tool 自己声明；tool 没说话就是互斥。 |
| 那个合成出来的结果 | [`packages/core/tools/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/tools/src/index.ts)：`TOOL_ABORTED_BEFORE_DISPATCH` | 这是跟 `TOOL_ABORTED` 不一样的错误码，这样光看对话记录就分得出来，一个调用是被跳过的，还是跑到一半被打断的。 |
| `Agent.cancel()` + `threading.Event` | [`packages/core/agent/src/runtime-types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent/src/runtime-types.ts)：`Agent.cancel` | 真正的取消，是把一串 abort signal 融在一起，穿过整个 runtime；mini 只留每个 turn 一个事件，在每一批的边界上检查。 |
| finish 照 model 给的顺序 append | [`packages/core/agent-loop/src/tool-calls.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-loop/src/tool-calls.ts) | 结果是在 loop 里变成 session 事件的，不是在 registry 里；`tool/result` 事件还会带 `sourceEventSeqs`，把每个答案接回它对应的那几行，而 mini 靠的是 `call_id`。 |
| 那个 `ThreadPoolExecutor` | [`packages/core/tools/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/tools/src/index.ts)：`TOOL_RUNTIME_SCHEDULER` | runtime 是通过一个具名的 seam 去拿它的 scheduler，而不是写死一个 pool。 |

真正的 scheduler 在这个 section 的 Mechanism 之上，还多做了这些：

- **用合作的方式中止已经开跑的调用。** `TOOL_ABORTED` 是给送出去之后才被打断的调用用的：融在一起的 signal 会传进实现里面，而 timeout policy（[`packages/guard/timeout-policy`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/guard/timeout-policy)）会帮 `tools/execute` 加上一个期限，同时不会把 tool 的 promise 丢在那里不管。mini 根本不会去打断已经开跑的实现，所以它只有送出去之前的那一种中止码。
- **提早结束的方式更多。** 一个 result 可以带 `concludesTurn`，让 turn 提早结束。mini 唯一的提早出口是 `cancel()`。
- **从头到尾都是 async。** dsh 的 tool 实现是 async 的，所以叠在一起跑这件事，是在同一条线程里靠 promise 完成的；mini 的实现是普通的 Python callable，所以它是用一个线程池换到同样的重叠。
- **按下取消的是人。** 在真正的 dsh 里，取消通常是从 UI 来的，而 UI 落在 Ceiling 之上；mini 就把 `cancel()` 开成一个普通的方法，而 Offline check 是从一个 tool 的实现里面按下去的。

---

## Failure modes

- **谁跑完谁就 append，会让 log 的顺序变成一场抢快比赛。** 让工作线程各自跑完就 append，同一个 turn 每跑一次就生出一份不一样的对话记录，重放也就不再是重建了。finish 是在同一条线程上照 model 给的顺序 append 的，所以叠着跑这件事永远不会出现在故事里，只会出现在时钟上。
- **要自己标互斥的话，责任就反过来了。** 如果 tool 默认安全、要自己标成互斥，那每个忘记标的人都是在拿共用状态赌一把。默认互斥的意思是，忘记标的人最多只是慢一点；检查里那个 `solo` 就证明了一个没标的 tool 真的会自己一个人跑。
- **半途丢下已经开跑的事情，弄坏的比救回来的多。** 一个实现写到一半被砍掉，会留下半个文件，和一个没人敢信的结果。scheduler 只会拒绝开新的一批；已经送出去的，会跑完，然后把结果交回来。取消是在关卡上做的决定，不是半路冲上去把东西砍断。
- **被跳过的调用如果没有那一行，对话记录就破了一个洞。** assistant 消息上四个调用都写着；把没开始的那两个丢掉，推导出来的历史就会问了问题却永远不回答。那个合成出来的结果，就是 Section 05 的规则在取消之后还活着：不管一个调用最后怎么样，它都会拿到一行回答。
- **工作线程如果会写 log，到处都得上锁。** session log 从设计上就只有一个写入者：prepare 和 finish 跑在 loop 那条线程上，工作线程只负责算。叠着跑这件事被关在一个阶段里面，不会漏到每一个数据结构上。
- **安全判定就算过时了，也还是安全的。** 判定在 prepare 就定死了，所以一个 tool 就算在一批跑到一半时被卸载，位子还是留着；它的实现要么已经跑过，要么结果会说清楚发生了什么。跑到一半再重新查一次，等于让计划在关卡底下偷偷变动。

---

## 跑跑看

[`src/`](src/) 把 05 原封不动搬过来，再加上：

- [`scheduler.py`](src/scheduler.py)（新增）：`execute_tool_calls`，把四个阶段一路推完的那个函数，还有 `_batches`，分批的规则。
- [`tools.py`](src/tools.py)：`ToolDefinition` 上的 `is_concurrency_safe`，registry 和 scope 上的 `is_safe()`。
- [`agent_loop.py`](src/agent_loop.py)：处理 tool 的那条分支改走 scheduler；Agent 多了 `cancel()` 和每个 turn 一个的中止事件；一个 step 现在可以用 `"aborted"` 结束。
- [`test.py`](src/test.py)：两个安全的调用要一起通过一道关卡，而那道关卡只有真的叠着跑才过得了，用这个证明它们真的重叠了；一个没标的 tool 夹在它们中间，自己一个人跑；就算快的那个先跑完，结果还是照 model 给的顺序落下；而一批跑到一半按下取消，已经开跑的会跑完，没开始的会拿到合成出来的结果，下一个 turn 从干净的状态重新开始。
- [`demo.py`](src/demo.py)：Live demo 会先要两次可以并行跑的查询，再要一次互斥的保存，并且把每个实现实际开始和结束的时间，连同 log 自己的故事一起打印出来。

```bash
python sections/06-scheduler/src/test.py    # offline check, no key
```

Live demo 需要根目录的 `requirements.txt` 和一把 key；没有 key 的话，它会安静地跳过：

```bash
pip install -r requirements.txt             # anthropic + python-dotenv
cp .env.example .env                        # then set ANTHROPIC_API_KEY
python sections/06-scheduler/src/demo.py
```

---

## 出处

- [`docs/tool-execution-pipeline.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/tool-execution-pipeline.md)：dsh 自己写的文档，讲 scheduler 推动的那条运行 pipeline。
- [`docs/subsystems/core.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/subsystems/core.md)：`executeToolCalls` 所在的那个 loop 包。
- [`docs/agent-lifecycle.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/agent-lifecycle.md)：tool 的运行在一个 turn 里面坐在什么位置，取消也一起讲。
