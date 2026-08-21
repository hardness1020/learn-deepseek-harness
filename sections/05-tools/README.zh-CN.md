<!-- source: README.md @ 4a394ca -->

# 05 · Tools

[English](README.md) | [繁體中文](README.zh-TW.md) | 简体中文

> 这条 pipeline 有四种说不的方法，回答却只有一种：一个 result。连根本没跑到的调用，也会拿到一个。

Section 04 的 loop 只会讲话。每个 step 都以 `"completed"` 结束，因为 model 除了回一句之外没别的事好做。tool 改变了这件事：model 会叫 mini-dsh 去跑某个东西，而且要先拿到结果，才能继续往下走。

最直觉的做法，是弄一个装着函数的 dict。用名字查出来、调用它、把它返回的东西 append 进去。名字查不到就抛出异常。参数不对就抛出异常。政策说不行，那就在调用之前先抛出异常。

但这些异常每一个都落在一个 turn 的中间。带着那次调用的 assistant 消息早就进了 log；异常会一路把 `send()` 拆回去，留下一个没有答案的问题。下一次推导出来的东西，会让 model 看到一段讲到一半就断掉的对话记录，而 replay 只会把同一个坏掉的故事再重建一次。

所以：为什么被拒绝或出错的调用，还是会产生一条正常的 `tool/result`？

因为对话记录就是那份契约。model 一定要看到一段在同一个 turn 里前后对得上的历史，replay 也一定要能把它重建回来，所以不管一个调用最后怎么样，它都会拿到一行回答。要撑住这件事，tool 这一层必须：

1. 把 tool 放在一个**有作用域的 registry** 里：一层 global，加上每个 agent 作用域各一层；作用域那一层会盖掉 global 的同名 tool，而套用到这个作用域的每一条限制，都会跟目前看得到的那组 tool 取交集。
2. 每一个调用都走同一条固定的 pipeline：**pre -> ask -> guard -> execute -> post**，永远照这个顺序。
3. 每一关都可以说不，但不准有任何异常穿过边界跑出去：被拒绝、跑爆、参数不对、名字查不到，出来的形状都一样，就是 `{call_id, name, is_error, content}`。
4. ask 这道门默认是关的：pre 的投票（`allow` / `ask` / `deny`）只会越收越紧，而一个没有人可以批准的 `ask`，就等于 deny。
5. 把结果穿回 loop 里：送去跑之前先 append 一行只进 log 的 `tool/call`，跑完之后再把 `tool/result` append 进 surface；而回复里带了调用的那个 step，会以 `None` 这个理由结束，意思是再绕一圈。
6. 每一次注册都交回一个撤销用的 undo，因为一切都是 plugin，而且每一次注册都可以反向撤销。

---

## Mechanism

一个新文件 `tools.py`，另外把 tool 这条线穿过原本就有的那些文件：

- **`ToolDefinition`**：model 看得到的部分（名字、说明、参数），加上真正做事的实现，`execute(args) -> content`。
- **`ToolRegistry`**：`tools` service。以作用域为 key 的层、限制条目、hook 列表，还有 `execute()` 里那条 pipeline。`register` / `restrict` / `pre` / `guard` / `post` 每一个都会返回自己的 undo。
- **`ToolScope`**：一个 agent 看到的 registry，也就是它自己那一层叠在 global 那一层上面。Agent 拿的是这个，永远不是 registry 本身。
- **loop 新长出来的那条分支**：`_step()` 现在会把 schema 跟着请求一起送出去，把回复里的那些调用跑一遍，然后在 turn 需要再绕一圈的时候交出 `None`。

这条 pipeline 就是一个漏斗。每一关都可以把调用挡下来，但所有出口都走同一扇门：

```text
call {"id", "name", "args"}
  │ resolve   unknown name ────────────────────────┐
  │ pre       votes tighten: allow < ask < deny ───┤
  │ ask       no approver, no approval ────────────┤
  │ guard     deny-only reasons ───────────────────┤
  │ execute   bad args, or the body raises ────────┤
  ▼                                                ▼
{"is_error": false, "content"}      {"is_error": true, "content"}
        └───────────────┬──────────────────────────┘
                      post (review, may replace)
                        ▼
             one shape, appended as tool/result
```

写成代码，这个漏斗就是一连串提早 return：

```python
def _run(self, call, scope):
    name = call.get("name")
    tool = self._visible(scope).get(name)
    if tool is None:
        return self._result(call, True, f"unknown tool '{name}'")
    decision = "allow"
    for hook in list(self._pre):
        vote = hook(call)
        if vote is not None and _RANK[vote] > _RANK[decision]:
            decision = vote  # votes only tighten, never loosen
    if decision == "deny":
        return self._result(call, True, "denied before execution")
    if decision == "ask":
        approved = self.asker is not None and self.asker(call)
        if not approved:
            return self._result(call, True, "approval was asked and not given")
    for check in list(self._guards):
        reason = check(call)
        if reason:
            return self._result(call, True, f"denied: {reason}")
    ...
    try:
        return self._result(call, False, str(tool.execute(args)))
    except Exception as exc:  # the body may fail; the pipeline may not
        return self._result(call, True, f"{type(exc).__name__}: {exc}")
```

loop 把这个漏斗接进 section 04 的 step 里。这就是 agent loop 当初空在那里、等人来接的 `None` 那条分支：

```python
if not final.tool_calls:
    self.session.append("step/end", {"reason": "completed"})
    return "completed"
for call in final.tool_calls:
    self.session.append("tool/call", call)  # log-only: before dispatch
    result = self.tools.execute(call)
    self.session.append("tool/result", result)  # joins the surface
self.session.append("step/end", {"reason": None})
return None  # tool calls ran: go around again
```

下面是一个 model 调用了 tool 的 turn，log 是这样记的：

```text
send("what is the wifi password?")
  │   0  user/message
  │   1  turn/start
  ├─ step ────────────────────────────────────────────────
  │   2  step/start
  │   3  request/header    {"messages": 1, "tools": ["lookup"]}
  │   4  assistant/chunk   x 3
  │   7  assistant/message {"content": "Checking.", "tool_calls": [c1]}
  │   8  tool/call         c1: lookup {"key": "wifi"}     ◄ log-only
  │   9  tool/result       {"call_id": "c1", "is_error": false,
  │                         "content": "hunter2"}          ◄ surface
  │  10  step/end          {"reason": null}
  ├─ reason is None ► go around
  │  11  step/start
  │  12  request/header    {"messages": 3, "tools": ["lookup"]}
  │      ...
  │  17  step/end          {"reason": "completed"}
  │  18  turn/end
```

结果进了 surface，所以第二次推导出来的是 `user`、`assistant`（带着它发出的调用）、`tool`：model 把自己发的调用和拿回来的答案，都当成普通的历史在读。section 02 早就默默替这件事铺好路了：从 surface 存在的那一天起，`tool/result` 就在 `SURFACE_TYPES` 里面。

现在把同一个 turn 再跑一次，换成一个会拒绝的 guard、一段会跑爆的实现，或是一个根本不存在的名字。log 记下来的故事形状一模一样，只有 `is_error` 和 `content` 不同。turn 活下来了，model 读得到哪里出错，而 Offline check 会把四种失败全塞进同一个 step，用来证明没有任何异常逃得出 `send()`。

作用域是这个 Mechanism 的另一半。`request/header` 现在会记下每次请求提供了哪些 tool，所以光看 log 就知道一个作用域看到了什么：作用域那一层盖掉了 global 的某个名字，而某条限制把 agent b 缩到只剩 `["where"]`，agent a 却还是什么都看得到。被限制掉的 tool 不是“被拒绝”，它对那个作用域来说根本不存在；硬要调用它，拿回来的是 `unknown tool`，跟其他任何一个一样，是个正常的结果。

### 改了什么

跟 section 04 比：

- `kernel.py` 原封不动搬过来。`tools.py` 是唯一新增的源文件；其他改动都是把 tool 这条线穿过原本就有的文件，所以跟 04 的 diff 就是这个 section 的 Mechanism，没有别的。
- `message.py`：`Message` 多了 `tool_calls`（assistant 用）和 `call_id`（tool 用），两个都有默认值，所以 section 04 的每一个 Message 读起来都跟以前一样。
- `standin.py`：Model seam 多了一个 `tools` 参数，Scripted stand-in 直接忽略它；而事先写好的回应可以是一个带 `tool_calls` 的 dict，这样会用到 tool 的 turn 也能离线写成脚本。
- `session_log.py`：`derive_messages()` 会把冻起来的 payload 里的 `tool_calls` 和 `call_id` 解冻，放回 Message 上。`SURFACE_TYPES` 完全没动。
- `agent_loop.py`：Agent 现在除了 session 和 Model seam，还会收下自己的 `ToolScope`；step 会把 schema 跟着请求一起送出去、记进 `request/header`、把调用丢进 pipeline 跑，并且把 section 04 空在那里的 `reason None` 那条分支补上。
- `demo.py`：Live demo 现在会真的用到 tool，中间还有一次 guard 拒绝，model 得自己读懂再解释给你听。

---

## In real dsh

下面所有指过去的链接，都固定在 Studied version [`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca)。tool 这一层住在 [`packages/core/tools`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/tools)，作用域的部分在 [`packages/core/scope`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/scope)。

| Mini-dsh | 真正的 dsh | 说明 |
| --- | --- | --- |
| `ToolRegistry` + `ToolScope` | [`packages/core/tools/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/tools/src/index.ts)：`ToolRuntime`；[`packages/core/scope/src/store.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/scope/src/store.ts)：`ScopedLayers` | `ctx.tools` 是一个底下垫着 `ScopedLayers` 的 registry：一层 global，加上每个 agent 一层作用域，同名会被盖掉，限制会取交集，全都通过 `register` / `restrict` 做。 |
| `ToolDefinition` | [`packages/core/tools/src/schema.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/tools/src/schema.ts)：`defineTool()` | `ToolDefinition extends ToolSchema`（schema 这个类型住在 [`packages/llm/llm/src/types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/llm/llm/src/types.ts)），再多加上有类型的参数、一组输出 `{schema, render}`、`timeoutMs`、`isConcurrencySafe`、`finalizeContent`。 |
| `pre()` 的投票 | [`packages/core/tools/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/tools/src/index.ts)：`tools/pre-execute` | 一个 waterfall 事件，产出 `PreToolDecision = allow \| deny \| ask`；`ask` 要的那个批准由 policy plugin 回答，再往上到 Ceiling 之上，就是 UI 在回答。 |
| `guard()` | [`packages/core/tools/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/tools/src/index.ts)：`ToolGuard` | `(execution) => string \| undefined`，只能拒绝，而且是同步的，在批准之后才在 pipeline 里跑。这跟 `packages/guard/*` 那些 plugin 不一样，那些只是普通的事件监听器。 |
| `post()` 的复审 | [`packages/core/tools/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/tools/src/index.ts)：`tools/post-execute` | 一个 waterfall，产出 `PostToolDecision = accept \| block`，另外还负责往上补东西（重复调用同一个 tool 的提醒就是搭这班车）。 |
| 那个 result dict | [`packages/core/tools/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/tools/src/index.ts)：`ToolExecutionSuccess` / `ToolExecutionFailure` | 一样是这个二分法，`isError: false \| true`，在变成 `tools/result` 事件之前会先被冻起来。 |
| loop 里那个一个一个跑的 for 循环 | [`packages/core/agent-loop/src/tool-calls.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-loop/src/tool-calls.ts)：`executeToolCalls` | 真正的 loop 从来不会直接调用 `ctx.tools.execute()`；推动这些调用的是一个四阶段的 scheduler。那个 scheduler 就是 section 06 的 Mechanism。 |

真正的 tool 这一层，在这个 section 的 Mechanism 之上多做了什么：

- **运行那一段外面还包了一层 waterfall。** `tools/execute` 把实现包起来，让 plugin 可以帮它设时间上限：timeout policy（[`packages/guard/timeout-policy`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/guard/timeout-policy)）自己定义了 `TOOL_TIMEOUT`，而且是用合作的方式包住，不会把 tool 的 promise 丢在那里不管。mini 是直接把实现跑下去。
- **从头到尾都有类型的 schema。** `defineTool()` 会拿真的 schema 去验参数，输出也一起验；`finalizeContent` 则决定 model 读到的东西长什么样。mini 只验参数名字对不对得上。
- **可以并行送出去跑。** `executeToolCalls` 跑的是一个 `prepare / dispatch / finalize / finish` 的 scheduler：可以并行跑的调用会叠在一起跑，互斥的调用会卡成一道关卡，而还没开始就被中止的调用会拿到一个合成出来的结果（`TOOL_ABORTED_BEFORE_DISPATCH`），这样 replay 才还算数。这一整套都是 section 06 的事。
- **result 能做的事更多。** 一个 result 可以带 `concludesTurn`，让 turn 提早结束；`tools/result` 事件还会记下 `sourceEventSeqs`；而且只要看得到的那组 tool 有变动，runtime 就会发出 `tools/change`。
- **`ask` 真的有人回答。** 人看到的那个批准提示是 UI，落在 Ceiling 之上；mini 把这个 seam 收成一个 `asker` callable，Offline check 直接在代码里回答它。

---

## Failure modes

- **用异常来拒绝，会把对话记录撕开。** pipeline 说不的时候，带着那次调用的 assistant 消息早就在 log 里了。不回答而是抛异常，推导出来的历史就会停在一个 model 永远等不到回音的问题上；replay 只会把同一个洞再挖一次。不管判决是什么，那一行 result 就是回答。
- **默默跳过，model 什么都学不到。** 把被拒绝的调用直接丢掉，model 要么永远等下去，要么永远重发。`is_error` 加上一个理由才是信息：检查里的 model 在同一个 step 里读到四种不同的失败，还是把 turn 走完了。
- **没有人批准的 ask，只能拒绝。** 如果默认放行，那一个什么都还没设置的 mini-dsh 反而是最宽松的。这道门默认是关的，而检查会证明：只要有人来回答，同一个调用就跑得起来。
- **guard 如果能放行，它们就会互相打架。** guard 只能拒绝，所以方向是单一的：任何一个 guard 都只会让能跑的事情变少，顺序因此永远不重要。一个能放行的 guard，会依照注册的先后去盖掉另一个的拒绝。
- **不能信任的是实现那一段。** 一个会抛异常的 tool 是很平常的事，接住它，包成 result 送回去。不能抛异常的是 pipeline 自己，所以参数不对和名字查不到也一样要变成 result，而不是拿 assert 去挡。
- **撤不掉的注册会活得比它的 plugin 还久。** `register` / `restrict` / `guard` 每一个都会交回自己的 undo，让 fiber 去收。检查会在对话进行到一半时卸载一个 tool plugin：下一行 `request/header` 什么都没提供，而去调用那个已经消失的 tool，也不过就是另一个正常的结果。
- **不取交集，作用域就只会越长越大。** 盖掉只能新增或替换，真正让范围变小的是限制。把所有适用的限制都取交集，代表任何一层都能把一个作用域圈起来；section 12 让 subagent 只拿到父层 tool 的一部分，靠的就是这件事。

---

## 跑跑看

[`src/`](src/) 把 04 原封不动搬过来，再加上：

- [`tools.py`](src/tools.py)（新增）：`ToolDefinition`、带着 pre/ask/guard/execute/post pipeline 的 `ToolRegistry`、`ToolScope`，还有提供 `tools` service 的 plugin。
- [`agent_loop.py`](src/agent_loop.py)：step 会把 tool 的 schema 跟着请求一起送出去，append `tool/call` 和 `tool/result` 两行，并在 turn 需要再绕一圈时以 `None` 这个理由结束。
- [`message.py`](src/message.py)、[`standin.py`](src/standin.py)、[`session_log.py`](src/session_log.py)：tool 这条线，细节就是“改了什么”列的那几条。
- [`test.py`](src/test.py)：一个用到 tool 的 turn 会再绕一圈，整个故事照顺序落在 log 上；四种失败形状都变成四个正常的结果；ask 这道门默认是关的，而且会盖过比较松的投票；post 的复审会改写一个结果；作用域的盖掉和限制，都看得到写在 `request/header` 上；卸载一个 tool plugin，会在对话进行到一半时把它的注册反向撤销。
- [`demo.py`](src/demo.py)：Live demo 会真的用到 tool。model 走 pipeline 去读一条笔记，接着撞上一次 guard 拒绝，再把 tool 告诉它的话讲出来，最后把 log 自己的故事打印出来。

```bash
python sections/05-tools/src/test.py        # offline check, no key
```

Live demo 需要根目录的 `requirements.txt` 和一把 key；没有 key 的话，它会安静地跳过：

```bash
pip install -r requirements.txt             # anthropic + python-dotenv
cp .env.example .env                        # then set ANTHROPIC_API_KEY
python sections/05-tools/src/demo.py
```

---

## 出处

- [`docs/subsystems/tools.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/subsystems/tools.md)：dsh 自己写的文档，讲 tool runtime。
- [`docs/tool-execution-pipeline.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/tool-execution-pipeline.md)：那条固定的 pipeline，一关一关讲。
- [`docs/subsystems/scope.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/subsystems/scope.md)：有作用域的层、盖掉，还有限制。
