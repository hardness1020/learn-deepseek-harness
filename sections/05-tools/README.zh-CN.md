<!-- source: README.md @ 3705bd7 -->

# 05 · Tools

[English](README.md) | [繁體中文](README.zh-TW.md) | 简体中文

> 工具调用会在运行前就写入 log。如果工具失败后没有写回结果，对话中就会留下一个没有回答的调用。因此，每次调用都必须产生对应的 `tool/result`，成功与失败都一样。

第 04 章的 loop 只能处理文本回复，所以每个 step 都会以 `"completed"` 结束。加入工具后，模型可以要求 Mini-dsh 运行操作，取得结果后再继续下一个 step。

最简单的实现，是用一个 dict 保存函数，依名称查找、运行，再将返回值写入 log。名称不存在、参数错误或政策拒绝时，就直接抛出例外。

但这些例外都发生在 turn 中间。包含工具调用的 assistant 消息已经写入 log，例外却会直接中断 `send()`，留下没有结果的调用。之后无论推导历史或重放 log，都只能得到一段不完整的对话。

因此，本章要回答的问题是：为什么被拒绝或运行失败的调用，仍然必须产生正常的 `tool/result`？

对话记录必须保持前后完整，重放时也必须能重建相同结果。所以无论调用成功、被拒绝还是运行失败，都要有一笔对应结果。工具层需要满足：

1. 工具保存在**具有作用域的 registry** 中：除了 global 层，每个 agent 也有自己的层。agent 层可以覆写同名 global 工具，而所有适用限制会与当前可见工具取交集。
2. 每次调用都固定经过 **pre -> ask -> guard -> execute -> post** 处理流程。
3. 任何阶段都可以拒绝调用，但不允许例外穿过边界。未知工具、参数错误、政策拒绝与运行例外，最后都会转成 `{call_id, name, is_error, content}`。
4. ask 默认关闭。pre 阶段的 `allow` / `ask` / `deny` 投票只能变得更严格；如果没有核准者，`ask` 就视为 `deny`。
5. 工具调用前先将 `tool/call` 写入 log，运行后再将 `tool/result` 加入 surface。包含工具调用的 step 返回 `None`，让 loop 继续下一个 step。
6. 每次注册都返回对应的 undo，让 plugin 卸载时可以完整撤销。

---

## 核心机制

本章添加 `tools.py`，并调整既有组件，让工具调用能完整走过 agent loop：

- **`ToolDefinition`**：model 看得到的部分（名字、说明、参数），加上真正做事的实现，`execute(args) -> content`。
- **`ToolRegistry`**：`tools` service。以作用域为 key 的层、限制条目、hook 列表，还有 `execute()` 里那条 pipeline。`register` / `restrict` / `pre` / `guard` / `post` 每一个都会返回自己的 undo。
- **`ToolScope`**：一个 agent 看到的 registry，也就是它自己那一层叠在 global 那一层上面。Agent 拿的是这个，永远不是 registry 本身。
- **loop 的工具分支**：`_step()` 会将 schema 随请求送出、运行回复中的工具调用，并在 turn 需要继续下一个 step 时返回 `None`。

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

loop 会把这条 pipeline 接进第 04 章的 step。当工具运行完成、模型还需要根据结果继续回答时，step 便返回 `None`：

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

以下是一个模型调用工具的 turn，以及对应的 log：

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

结果会加入 surface，因此第二次推导出的历史依序包含 `user`、带有调用信息的 `assistant`，以及 `tool`。模型可以像阅读一般对话一样，看到自己提出的调用与工具返回结果。第 02 章已将 `tool/result` 纳入 `SURFACE_TYPES`，所以不需再修改 surface 规则。

若改成会拒绝的 guard、会抛出例外的实现，或不存在的工具名称，log 的事件结构仍然相同，只有 `is_error` 与 `content` 不同。turn 不会因此中断，模型也能读到错误原因。离线测试会在同一个 step 中涵盖四种失败，确认例外不会穿过 `send()` 边界。

作用域是这项机制的另一半。`request/header` 会记下每次请求提供了哪些工具，因此只看 log 就能确认各作用域的可见范围。例如，agent 层可以覆写 global 的同名工具，限制也能让 agent b 只看到 `["where"]`，同时不影响 agent a。被限制的工具在该作用域中等同不存在；若仍尝试调用，系统会返回一般的 `unknown tool` 错误结果。

### 改了什么

与第 04 章相比：

- `kernel.py` 完整沿用。`tools.py` 是唯一添加的源文件；其他改动都是把 tool 这条线穿过原本就有的文件，因此与第 04 章相比，diff 只包含本章添加的机制，不包含其他改动。
- `message.py`：`Message` 多了 `tool_calls`（assistant 用）和 `call_id`（tool 用），两个都有默认值，所以第 04 章的每一个 Message 读起来都跟以前一样。
- `standin.py`：Model seam 多了一个 `tools` 参数，Scripted stand-in 直接忽略它；而事先写好的回应可以是一个带 `tool_calls` 的 dict，这样会用到 tool 的 turn 也能离线写成脚本。
- `session_log.py`：`derive_messages()` 会把冻起来的 payload 里的 `tool_calls` 和 `call_id` 解冻，放回 Message 上。`SURFACE_TYPES` 完全没动。
- `agent_loop.py`：Agent 现在除了 session 和 Model seam，还会收下自己的 `ToolScope`；step 会把 schema 跟着请求一起送出去、记进 `request/header`、把调用丢进 pipeline 跑，并且把第 04 章空在那里的 `reason None` 那条分支补上。
- `demo.py`：在线示例现在会真的用到 tool，中间还有一次 guard 拒绝，model 得自己读懂再解释给你听。

---

## 对照真正的 dsh

以下链接皆指向研究版本 [`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca)。tool 这一层位于 [`packages/core/tools`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/tools)，作用域的部分在 [`packages/core/scope`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/scope)。

| Mini-dsh | 真正的 dsh | 说明 |
| --- | --- | --- |
| `ToolRegistry` + `ToolScope` | [`packages/core/tools/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/tools/src/index.ts)：`ToolRuntime`；[`packages/core/scope/src/store.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/scope/src/store.ts)：`ScopedLayers` | `ctx.tools` 是一个底下垫着 `ScopedLayers` 的 registry：一层 global，加上每个 agent 一层作用域，同名会被盖掉，限制会取交集，全都通过 `register` / `restrict` 做。 |
| `ToolDefinition` | [`packages/core/tools/src/schema.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/tools/src/schema.ts)：`defineTool()` | `ToolDefinition extends ToolSchema`（schema 这个类型位于 [`packages/llm/llm/src/types.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/llm/llm/src/types.ts)），再多加上有类型的参数、一组输出 `{schema, render}`、`timeoutMs`、`isConcurrencySafe`、`finalizeContent`。 |
| `pre()` 的投票 | [`packages/core/tools/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/tools/src/index.ts)：`tools/pre-execute` | 一个 waterfall 事件，产出 `PreToolDecision = allow \| deny \| ask`；`ask` 所需的核准由 policy plugin 处理，实际 UI 交互不在本教学的实现范围内。 |
| `guard()` | [`packages/core/tools/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/tools/src/index.ts)：`ToolGuard` | `(execution) => string \| undefined`，只能拒绝，而且是同步的，在批准之后才在 pipeline 里跑。这跟 `packages/guard/*` 那些 plugin 不一样，那些只是普通的事件监听器。 |
| `post()` 的复审 | [`packages/core/tools/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/tools/src/index.ts)：`tools/post-execute` | 一个 waterfall，产出 `PostToolDecision = accept \| block`，也能在结果中补充提醒，例如侦测到重复调用同一个工具时加入说明。 |
| 统一的 result dict | [`packages/core/tools/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/tools/src/index.ts)：`ToolExecutionSuccess` / `ToolExecutionFailure` | 同样使用 `isError: false \| true` 区分成功与失败，并在转成 `tools/result` 事件前设为不可变数据。 |
| loop 里那个一个一个跑的 for 循环 | [`packages/core/agent-loop/src/tool-calls.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-loop/src/tool-calls.ts)：`executeToolCalls` | 真正的 loop 从来不会直接调用 `ctx.tools.execute()`；推动这些调用的是一个四阶段的 scheduler。那个 scheduler 就是第 06 章的机制。 |

真正的 tool 这一层还提供以下功能：

- **运行那一段外面还包了一层 waterfall。** `tools/execute` 把实现包起来，让 plugin 可以帮它设时间上限：timeout policy（[`packages/guard/timeout-policy`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/guard/timeout-policy)）自己定义了 `TOOL_TIMEOUT`，而且是用合作的方式包住，不会把 tool 的 promise 丢在那里不管。mini 是直接把实现跑下去。
- **从头到尾都有类型的 schema。** `defineTool()` 会拿真的 schema 去验参数，输出也一起验；`finalizeContent` 则决定 model 读到的东西长什么样。mini 只验参数名字对不对得上。
- **可以平行送出去跑。** `executeToolCalls` 跑的是一个 `prepare / dispatch / finalize / finish` 的 scheduler：可以平行跑的调用会叠在一起跑，互斥的调用会卡成一道关卡，而还没开始就被中止的调用会拿到一个合成出来的结果（`TOOL_ABORTED_BEFORE_DISPATCH`），这样重放才还算数。这一整套都是第 06 章的事。
- **result 能做的事更多。** 一个 result 可以带 `concludesTurn`，让 turn 提早结束；`tools/result` 事件还会记下 `sourceEventSeqs`；而且只要看得到的那组 tool 有变动，runtime 就会发出 `tools/change`。
- **`ask` 真的有人回答。** 人看到的那个批准提示是 UI，不在本教学的实现范围内；mini 把这个 seam 收成一个 `asker` callable，离线测试直接在代码里回答它。

---

## 常见失败模式

- **用例外表示拒绝，会留下不完整的对话。** 工具调用出现时，对应的 assistant 消息已写入 log；如果系统只抛例外而不产生结果，模型历史就会停在一个永远没有回复的调用。无论成功或失败，都必须补上一笔 result。
- **直接跳过调用，模型无法知道发生了什么。** 被拒绝的调用若没有结果，模型可能持续等待或重复提出相同请求。`is_error` 加上明确原因，才能让模型判断下一步。
- **没有人处理的 ask 必须默认拒绝。** 若默认放行，未设置任何政策的 Mini-dsh 反而最宽松。测试会确认 ask 在没有核准者时遭拒，提供 `asker` 后才会运行。
- **guard 如果能放行，它们就会互相打架。** guard 只能拒绝，所以方向是单一的：任何一个 guard 都只会让能跑的事情变少，顺序因此永远不重要。一个能放行的 guard，会依照注册的先后去盖掉另一个的拒绝。
- **工具实现不能被视为可靠边界。** 工具抛出例外是正常的失败情况，pipeline 必须捕捉并转成 result。参数错误和名称不存在也要用相同方式处理，而不是通过 assert 中断流程。
- **撤不掉的注册会活得比它的 plugin 还久。** `register` / `restrict` / `guard` 每一个都会交回自己的 undo，让 fiber 去收。检查会在对话进行到一半时卸载一个 tool plugin：下一行 `request/header` 什么都没提供，而去调用那个已经消失的 tool，也不过就是另一个正常的结果。
- **不取交集，作用域就只会愈长愈大。** 盖掉只能添加或替换，真正让范围变小的是限制。把所有适用的限制都取交集，代表任何一层都能把一个作用域圈起来；第 12 章让 subagent 只拿到父层 tool 的一部分，靠的就是这件事。

---

## 动手验证

[`src/`](src/) 延续第 04 章，并加入：

- [`tools.py`](src/tools.py)（添加）：`ToolDefinition`、带着 pre/ask/guard/execute/post pipeline 的 `ToolRegistry`、`ToolScope`，还有提供 `tools` service 的 plugin。
- [`agent_loop.py`](src/agent_loop.py)：step 会把 tool 的 schema 跟着请求一起送出去，append `tool/call` 和 `tool/result` 两行，并在 turn 需要再绕一圈时以 `None` 这个理由结束。
- [`message.py`](src/message.py)、[`standin.py`](src/standin.py)、[`session_log.py`](src/session_log.py)：tool 这条线，细节就是「改了什么」列的那几条。
- [`test.py`](src/test.py)：一个用到 tool 的 turn 会再绕一圈，完整流程照顺序落在 log 上；四种失败形状都变成四个正常的结果；ask 这道门默认是关的，而且会盖过比较松的投票；post 的复审会改写一个结果；作用域的盖掉和限制，都看得到写在 `request/header` 上；卸载一个 tool plugin，会在对话进行到一半时把它的注册反向撤销。
- [`demo.py`](src/demo.py)：在线示例会真的用到 tool。model 走 pipeline 去读一则笔记，接着撞上一次 guard 拒绝，再把 tool 告诉它的话讲出来，最后把 log 中的完整运行记录印出来。

```bash
python sections/05-tools/src/test.py        # offline check, no key
```

在线示例需要根目录的 `requirements.txt` 和一把 key；没有设置 key 时会自动跳过：

```bash
pip install -r requirements.txt             # anthropic + python-dotenv
cp .env.example .env                        # then set ANTHROPIC_API_KEY
python sections/05-tools/src/demo.py
```

---

## 参考资料

- [`docs/subsystems/tools.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/subsystems/tools.md)：dsh 自己写的文档，讲 tool runtime。
- [`docs/tool-execution-pipeline.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/tool-execution-pipeline.md)：那条固定的 pipeline，一关一关讲。
- [`docs/subsystems/scope.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/subsystems/scope.md)：有作用域的层、盖掉，还有限制。
