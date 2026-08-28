<!-- source: README.md @ 3705bd7 -->

# 08 · System prompt

[English](README.md) | [繁體中文](README.zh-TW.md) | 简体中文

> harness 中有多个模块会共同组成 system prompt，而这些文字在每个 step 送出时必须完全一致。因此，会随 step 变动的状态不能写入 system prompt。

第 07 章送出的请求已能正确反映历史，但内容仍然很简单。`_step()` 直接从 tool registry 取得 schema，system prompt 则是空的。模型不知道自己的角色、回应方式，也不知道当前运行环境。

harness 中的多个部分都可能提供 prompt 内容。Mini-dsh 提供身份说明，persona plugin 定义语气，工具层提供 schema 列表。这些模块应能独立注册内容，而组装后的顺序必须稳定。

但时间、工作目录等状态会持续变化，模型需要的是当下快照。如果将这些内容写进 system prompt，每个 step 都会产生不同的 prompt 前缀，使模型端的 prompt cache 无法命中。

如果只在发送请求时临时附加动态状态，这些内容又不会进入 log，之后便无法重建模型实际看到的数据。这会破坏第 02 章创建的可重放性。

因此，本章要回答的问题是：为什么动态状态要作为 user 消息重新发送，而不是写进 system prompt？

system prompt 需要保持稳定，log 则必须完整记录模型看过的动态状态。组装过程因此必须：

1. 使用单一 registry 管理四种 provider：固定的 system sections、动态 context、`{{name}}` 对应的 variable，以及 tool schema。每次注册都会返回撤销函数。
2. 组装结果必须稳定：每笔内容都有数字顺序，同分时依注册顺序排列，确保相同注册永远产生相同文本。
3. 变量代入必须严格：`{{name}}` 若不存在或尚未设置，就取消这次 request，避免送出残缺 prompt。
4. 每次组装会产生 system 文本、当次 request 的工具列表，以及 runtime-context 快照。
5. 快照以 `user/message` 送出，只有内容变更时才重发。比对基准直接取自 log 中最后一笔快照，不维护额外状态。
6. 每个 step 都在重新推导历史的同一个边界进行一次组装。

---

## 核心机制

本章添加 `system_prompt.py`，并将 request 组装集中到这里：

- **`SystemPrompt`**：prompt registry。`section()`、`context()`、`variable()`、`tools()` 用来注册 provider，并依 kernel 惯例返回撤销函数。内置的 `harness:identity` 使用 order -100，因此 plugin 提供的文本默认排在它后面。
- **`assemble(assemble_context)`**：依 `(order, 注册顺序)` 解析所有 provider，返回 `system`、`tools` 与 `runtime_context`。
- **工具桥接**：plugin 会注册一个 tool schema provider，从 assemble context 取得 agent 在目前作用域可见的工具，让工具列表也成为 prompt 组装结果的一部分。
- **`latest_snapshot(session)`**：负责去重。拿来比对的那份快照是 log 的投影，也就是 payload 带着 `"kind": "runtime-context"` 的最后一笔 `user/message`。

```python
def assemble(self, assemble_context):
    """Resolve every provider, in order: the request's three artifacts."""
    sections = [self._render(e["text"], assemble_context) for e in _ordered(self._sections)]
    contexts = [e["provider"](assemble_context) for e in _ordered(self._contexts)]
    return {
        "system": "\n\n".join(text for text in sections if text),
        "tools": [s for provider in self._tools for s in provider(assemble_context)],
        "runtime_context": "\n".join(text for text in contexts if text),
    }
```

在 `_step()` 里面，组装就接在 inbox 认领后面，位置是第 04 章本来就会把所有东西重新推导一次的那个边界。快照只有跟最后一笔快照不一样，才会进 log；同时 Model seam 多了第三个值：

```python
assembly = self.prompt.assemble({"tools": self.tools})
snapshot = assembly["runtime_context"]
if snapshot and snapshot != latest_snapshot(self.session):
    self.session.append("user/message", {"content": snapshot, "kind": "runtime-context"})
messages = self.session.derive_messages()  # re-derived, never cached
```

provider 会在每个 step 重新计算内容，只有变更过的快照会写入 log：

```text
registered, ordered              assemble({"tools": scope}), every step

sections  -100 harness:identity ─┐
             0 persona           ├─► system text ────► request, byte-identical
variables  {{user}} = "Ada"     ─┘                     every step
tool providers  the bridge ──────► tool list ────────► request
contexts     0 time: 10:01      ─┐
            10 cwd: /home/ada    ├─► snapshot ─► same as the last snapshot
                                 ┘               row in the log?
                                                 ├─ yes: nothing appended
                                                 └─ no:  user/message row,
                                                         "kind": "runtime-context"
```

以下是实际运行时的 log。`tick` 工具会在 turn 中途调整仿真时钟；两次 request 的 system 文本都维持 61 个字符，内容完全相同，但 runtime-context 快照会因时间变化而重发一次：

```text
send("go")
  │   0  turn/start
  │   1  step/start
  │   2  user/message   "go"                  ◄ claimed at the boundary
  │   3  user/message   "time: 10:00"         ◄ snapshot, first reading
  │   4  request/header system 61 chars, tools ["tick"]
  │   5  assistant/message {"tool_calls": [tick]}
  │   6  tool/call     tick
  │   7  tool/result   "ticked"               ◄ the clock now says 10:01
  │   8  step/end      {"reason": null}
  │   9  step/start
  │  10  user/message   "time: 10:01"         ◄ changed: re-emitted
  │  11  request/header system 61 chars, tools ["tick"]
  │  12  assistant/chunk "do"
  │  13  assistant/chunk "ne"
  │  14  assistant/message "done"
  │  15  step/end      {"reason": "completed"}
  │  16  turn/end
```

如果时钟没有变动，seq 10 就不会出现：第二个 step 发现快照与上一笔相同后，不会追加任何事件。模型看过的读数都以普通 `user` 消息保存在历史中，因此可以持久化与重放。

### 改了什么

与第 07 章相比：

- `inbox.py`、`kernel.py`、`message.py`、`scheduler.py`、`session_log.py`、 `tools.py` 完整沿用。`system_prompt.py` 是唯一的新源代码文件；其他改动都是把组装接进 `agent_loop.py`，因此与第 07 章相比，diff 只包含本章添加的机制，不包含其他改动。
- `agent_loop.py`：`Agent` 和 `AgentRegistry.create()` 多了一个 `prompt` 参数。`_step()` 每个 step 组装一次，快照变了就追加一笔，tool 列表改成从组装的结果拿、不再直接跟 registry 要，并且把 system 文本经由 Model seam 传下去。
- `standin.py`：Model seam 的签名多了 `system=""`，就一行。Scripted stand-in 还是被动的：它从来不去看 request 里有什么，system 文本也一样不看。
- log 的长相变了：`request/header` 现在会记下组装出来的 system 文本，而 `user/message` 的 payload 可能带着 `"kind": "runtime-context"`，用来标记这是一笔快照。推导历史的时候，两种都当成普通的 `user` 消息。
- `demo.py`：在线示例注册一段 persona 文本，把真的时钟和 cwd 当成 context 收进来，再放一个很慢的 tool，慢到时钟会在 turn 中途走动，所以重发这件事会发生在一次实际模型调用上。

---

## 对照真正的 dsh

以下链接皆指向研究版本 [`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca)。registry 位于 core 的 system-prompt 软件包里，快照去重则在 loop 里： [`packages/core/system-prompt`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/system-prompt)。

| Mini-dsh | 真正的 dsh | 说明 |
| --- | --- | --- |
| `system_prompt.py` 里的 `SystemPrompt` | [`packages/core/system-prompt/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/system-prompt/src/index.ts)：`SystemPrompt` | 一样是 `section() / context() / variable() / tools()` 后面那四种 provider，每一个都返回一个 Cordis 的 effect disposer，也就是 mini 那个撤销函数在真实世界里的样子。 |
| `assemble()` 返回三样东西 | [`index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/system-prompt/src/index.ts)：`PromptAssembly`、`renderPrompt` | 组装先解成一个 `PromptAssembly`，走过 `system-prompt/assemble` 这个 waterfall，再算出 `system` 字符串、这次 request 的 tool 列表，以及 runtime-context 快照。 |
| 内置 identity 的 `order=-100` | [`index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/system-prompt/src/index.ts)：`'harness:identity'` | 内置的 identity 那一段坐在 order -100，对外导出的 `PERSONA_SECTION` 在 0，tool 的指引在 100 到 199。排序就是一个数字字段 `order`，不是什么阶段枚举。 |
| `{{name}}` 的严格代入 | [`index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/system-prompt/src/index.ts) | `{{variable}}` 是严格代入：名字不认得，或值是 undefined，就直接丢出例外，跟 mini 那条「不合格就不送」的规则一模一样。 |
| `latest_snapshot(session)` | [`packages/core/agent-loop/src/runtime-context.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-loop/src/runtime-context.ts)：`RuntimeContextProjection` | 拿来比对的快照是一份投影；只有跟它不一样的时候，快照才会以 `user/message` 的身份发出去，永远不会变成 system 文本。 |
| `_step()` 里面的组装 | [`packages/core/agent-loop/src/agent.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-loop/src/agent.ts)：`preStep` | 组装每个 step 做一次，发生在 `preStep` 里面、`agent/pre-step` 这个 hook 之前，跟 mini 用的是同一个边界（第 230 行）。 |
| `system_prompt_plugin` 的工具桥接 | [`packages/core/tools/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/tools/src/index.ts)：`ctx.systemPrompt.tools(...)` | 工具会将 schema 注册为 prompt provider（第 832 到 836 行）。Mini-dsh 在 prompt plugin 中完成桥接；真正的 dsh 则由 tools 软件包主动注册。 |
| 检查里用的 time context | [`packages/context/time-context/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/context/time-context/src/index.ts) | 有一整个软件包系列都用这种方式提供 context；`agent-instructions` 也是走同一条通道，把工作区的指示送进来。 |

真正的 system-prompt 这一层还提供以下功能：

- **组装前后有事件。**`system-prompt/assemble` 是一个会依 scope 过滤的 waterfall，可以在组装还在进行的时候就把结果改掉，而 `system-prompt/change` 会公告 registry 有变动。mini 的组装没有任何 hook。
- **tool 的顺序有明确规则。**真正的 dsh 在排这次 request 的 tool 列表时，会照一个写死的常数 `TOOL_ORDER_REST` 来排；mini 就只靠注册顺序。
- **registry 之外还有一条 context 通道。**`packages/context` 底下大部分的东西根本不走 `systemPrompt.context()`：`agent-instructions`、`time-context`、 `tmux-context` 都是从 `agent/pre-step` 的 listener 直接追加 `UserMessage`。真正会去调用 registry 那个 `context()` 的，是 sandbox 政策、核准政策，还有 subagent 的委派。真正的 sandbox 隔离超出本教学的实现范围；mini 那个改写 argv 的替身，要等第 10 章讲 capability seam 的时候才会出现。
- **section 可以延后完成。** 真正的 `PromptSection` 可声明 `complete?`，让组装先继续进行，较慢的 provider 之后再补上内容。Mini-dsh 的 provider 则全部同步运行。

---

## 常见失败模式

- **把时钟放进 system 文本会让每个 step 的前缀缓存失效。** 只要时间戳持续变动，prompt 前缀就无法重用。将 section 与 context 分开，可以从结构上确保动态内容不会进入 system 文本。
- **只在 request 中临时附加文本，重放时就会遗失。** 动态状态必须以 `user/message` 快照写入 log，system 文本则记在 `request/header`，才能完整重建模型实际收到的内容。
- **快照未变仍重发，会无端增加历史长度。** 每次 request 都多带一笔相同数据，却没有添加信息。系统会在边界与最后一笔快照比较，只在变更时追加。
- **将比对快照放在内存，重启后会与 log 不一致。** 内存状态会消失，第一个 step 可能重送模型已看过的快照。Mini-dsh 直接从 log 推导比对基准，让去重与重放维持一致。
- **宽松代入可能送出未填值的 prompt。** `{{typo}}` 若原样送给模型，只会形成无意义内容。严格代入会在送出 request 前抛错，log 也会显示 step 停在 `request/header` 之前。
- **provider 缺少稳定顺序，组装结果就可能改变。** 若依赖 dict 顺序或完成时间，同一组注册可能产生不同 prompt。使用数字 order，并以注册顺序处理同分项目，才能得到稳定结果。

---

## 动手验证

[`src/`](src/) 延续第 07 章，并加入：

- [`system_prompt.py`](src/system_prompt.py)（新的）：`SystemPrompt`，四种 provider，每一次注册都给一个撤销函数；`assemble()`；`latest_snapshot()`；还有那个 plugin，内置 identity 和 tool schema 的桥都在里面。
- [`agent_loop.py`](src/agent_loop.py)：`_step()` 每个 step 组装一次，快照变了就追加一笔，并把 system 文本经由 Model seam 传下去；`Agent` 和 `create()` 多了 `prompt` 参数。
- [`standin.py`](src/standin.py)：seam 的签名多了 `system=""`；Scripted stand-in 一样不去看它。
- [`test.py`](src/test.py)：离线测试证明三样东西会落在同一次 request 里； turn 中途 tick 一下会让快照重发，而 system 文本一个字节都没变；去重在同一个 turn 内和跨 turn 都成立；`{{variable}}` 不认得或没设值，会让这个 step 停在任何 request 送出去之前；每一次注册都撤销得掉。
- [`demo.py`](src/demo.py)：在线示例在内置 identity 上面叠一段 persona，把真的时钟和 cwd 拍成快照，再让一个很慢的 tool 逼出一次 turn 中途的重发，整段跑在实际模型调用上。

```bash
python sections/08-system-prompt/src/test.py    # offline check, no key
```

在线示例需要根目录的 `requirements.txt` 和一把 key；没有设置 key 时会自动跳过：

```bash
pip install -r requirements.txt         # anthropic + python-dotenv
cp .env.example .env                    # then set ANTHROPIC_API_KEY
python sections/08-system-prompt/src/demo.py
```

---

## 参考资料

- [`docs/subsystems/system-prompt.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/subsystems/system-prompt.md)： dsh 自己带你走一遍那四种 provider，还有算出来的那三样东西。
- [`packages/context/README.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/context/README.md)： context 这一整个软件包系列，还有里面哪些成员走 registry、哪些走 pre-step 那条通道。
