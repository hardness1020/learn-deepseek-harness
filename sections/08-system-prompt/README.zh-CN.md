<!-- source: README.md @ 55e829b -->

# 08 · System prompt

[English](README.md) | [繁體中文](README.zh-TW.md) | 简体中文

> harness 里有好几块都各自掌管一段要告诉 model 的文本，而且每个 step 送出的字必须完全一样。所以会在 step 之间变动的东西，不能放进那段文本里。

Section 07 送出去的 request 很诚实，但也很空。`_step()` 直接从 tool registry 捞 schema，system 文本则是一个字都不带：没有人告诉 model 它是谁、该怎么表现、现在外面的世界长什么样。

这些缺席的文本，主人不只一个。Mini-dsh 管自己的身份那一行；某个 persona plugin 管语气；tool 那一层管 schema 列表。每一方都想把自己那一块放进去，又不想为了这件事去跟别人协调；而且每一块在每一次 request 里，都得落在同一个位置。

而且有些状态是会变的。时钟、工作目录这种：model 要的是当下的读数；但只要把它写死在 system 文本里，就不会有任何两个 step 送出一样的 prompt。model 那一端是靠稳定的 prompt 前缀在做缓存，所以 system 文本里只要有一个时间戳，每个 step 的缓存都会落空。

另一个直觉的做法更糟：把动态文本从旁边补进 request，它就永远不会进到 log 里。重放的时候，你重建不出 model 真正看到的东西，而那正是 Section 02 的全部重点。

所以：为什么动态状态是一条重新发出的 user 消息，而不是写进 system 文本里？

因为 system 文本必须待着不动，而 log 必须是完整的故事。要做到这件事，组装的过程必须：

1. 只留一个 registry，里面有四种 provider：sections（固定不动的 system 文本）、context（动态状态）、variable（`{{name}}` 要填的值），还有 tool schema 的 provider。每一次注册都会返回它自己的撤销函数。
2. 算出来的结果要固定：每一条有一个数字顺序，同分就照注册顺序排，所以同样的注册永远算出同样的文本。
3. 代入变量要严格：`{{name}}` 对应的变量不认得，或根本没设值，就直接不送这次 request，而不是送一个带着洞的 prompt 出去。
4. 一次组装产出三样东西：system 文本、这次 request 的 tool 列表，还有一份 runtime-context 快照。
5. 快照用一条 `user/message` 送出去，而且只有变了才重发。拿来比对的那份快照，就是 log 里最后一条快照本身，不另外存一份状态。
6. 每个 step 组装一次，就在边界上，跟历史重新推导的位置同一个地方。

---

## Mechanism

一个新文件 `system_prompt.py`，再把 request 的组装改道，让它走这里：

- **`SystemPrompt`**：那个 registry。`section()`、`context()`、`variable()`、 `tools()` 负责把 provider 收进来；每一个都照 kernel 的做法，返回自己的撤销函数。内建的 `harness:identity` 这一段坐在 order -100，所以 plugin 的文本默认会排在它后面。
- **`assemble(assemble_context)`**：照 `(order, 注册顺序)` 把每个 provider 解出来，返回那三样东西：`system`、`tools`、`runtime_context`。
- **那座桥**：plugin 注册一个 tool schema 的 provider，从 assemble context 里拿出 agent 在作用域内看得到的那些 tool，所以这次 request 的 tool 列表，也算是 prompt 组装出来的东西之一。
- **`latest_snapshot(session)`**：负责去重。拿来比对的那份快照是 log 的投影，也就是 payload 带着 `"kind": "runtime-context"` 的最后一条 `user/message`。

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

在 `_step()` 里面，组装就接在 inbox 认领后面，位置是 Section 04 本来就会把所有东西重新推导一次的那个边界。快照只有跟最后一条快照不一样，才会进 log；同时 Model seam 多了第三个值：

```python
assembly = self.prompt.assemble({"tools": self.tools})
snapshot = assembly["runtime_context"]
if snapshot and snapshot != latest_snapshot(self.session):
    self.session.append("user/message", {"content": snapshot, "kind": "runtime-context"})
messages = self.session.derive_messages()  # re-derived, never cached
```

provider 在一边把东西算出来；只有变过的快照会跨进 log：

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

下面是一次真的运行，照 log 记下来的样子。一个叫 `tick` 的 tool 在 turn 中途拨动一个假时钟；两次 request 的 system 文本都是 61 个字符，一个字节都不差，而快照重发了一次：

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

时钟要是没动，seq 10 根本不会出现：第二个 step 会发现快照跟最后一条快照一样，什么都不追加。model 看过的那两个读数，在推导出来的历史里都是普通的 `user` 记录，存得住，也重放得出来。

### 改了什么

跟 Section 07 比起来：

- `inbox.py`、`kernel.py`、`message.py`、`scheduler.py`、`session_log.py`、 `tools.py` 原封不动搬过来。`system_prompt.py` 是唯一的新源代码文件；其他改动都是把组装接进 `agent_loop.py`，所以跟 07 的 diff 刚好就是这个 Section 的 Mechanism，没有别的。
- `agent_loop.py`：`Agent` 和 `AgentRegistry.create()` 多了一个 `prompt` 参数。`_step()` 每个 step 组装一次，快照变了就追加一条，tool 列表改成从组装的结果拿、不再直接跟 registry 要，并且把 system 文本经由 Model seam 传下去。
- `standin.py`：Model seam 的签名多了 `system=""`，就一行。Scripted stand-in 还是被动的：它从来不去看 request 里有什么，system 文本也一样不看。
- log 的长相变了：`request/header` 现在会记下组装出来的 system 文本，而 `user/message` 的 payload 可能带着 `"kind": "runtime-context"`，用来标记这是一条快照。推导历史的时候，两种都当成普通的 `user` 消息。
- `demo.py`：Live demo 注册一段 persona 文本，把真的时钟和 cwd 当成 context 收进来，再放一个很慢的 tool，慢到时钟会在 turn 中途走动，所以重发这件事会发生在一次真的 model 调用上。

---

## In real dsh

所有指过去的链接都固定在 Studied version [`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca) 上。registry 住在 core 的 system-prompt 包里，快照去重则在 loop 里： [`packages/core/system-prompt`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/system-prompt)。

| Mini-dsh | 真正的 dsh | 说明 |
| --- | --- | --- |
| `system_prompt.py` 里的 `SystemPrompt` | [`packages/core/system-prompt/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/system-prompt/src/index.ts)：`SystemPrompt` | 一样是 `section() / context() / variable() / tools()` 后面那四种 provider，每一个都返回一个 Cordis 的 effect disposer，也就是 mini 那个撤销函数在真实世界里的样子。 |
| `assemble()` 返回三样东西 | [`index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/system-prompt/src/index.ts)：`PromptAssembly`、`renderPrompt` | 组装先解成一个 `PromptAssembly`，走过 `system-prompt/assemble` 这个 waterfall，再算出 `system` 字符串、这次 request 的 tool 列表，以及 runtime-context 快照。 |
| 内建 identity 的 `order=-100` | [`index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/system-prompt/src/index.ts)：`'harness:identity'` | 内建的 identity 那一段坐在 order -100，对外导出的 `PERSONA_SECTION` 在 0，tool 的指引在 100 到 199。排序就是一个数字字段 `order`，不是什么阶段枚举。 |
| `{{name}}` 的严格代入 | [`index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/system-prompt/src/index.ts) | `{{variable}}` 是严格代入：名字不认得，或值是 undefined，就直接抛出异常，跟 mini 那条“不合格就不送”的规则一模一样。 |
| `latest_snapshot(session)` | [`packages/core/agent-loop/src/runtime-context.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-loop/src/runtime-context.ts)：`RuntimeContextProjection` | 拿来比对的快照是一份投影；只有跟它不一样的时候，快照才会以 `user/message` 的身份发出去，永远不会变成 system 文本。 |
| `_step()` 里面的组装 | [`packages/core/agent-loop/src/agent.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/agent-loop/src/agent.ts)：`preStep` | 组装每个 step 做一次，发生在 `preStep` 里面、`agent/pre-step` 这个 hook 之前，跟 mini 用的是同一个边界（第 230 行）。 |
| `system_prompt_plugin` 里的那座桥 | [`packages/core/tools/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/core/tools/src/index.ts)：`ctx.systemPrompt.tools(...)` | tool 把自己的 schema 注册成一个 prompt provider（第 832 到 836 行）。mini 把这座桥收进 prompt 的 plugin 里；真正的 dsh 则是从 tools 包那一侧注册。 |
| 检查里用的 time context | [`packages/context/time-context/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/context/time-context/src/index.ts) | 有一整个包家族都用这种方式提供 context；`agent-instructions` 也是走同一条通道，把工作区的指示送进来。 |

真正的 system-prompt 这一层，在这个 Section 的 Mechanism 之上，还多做了这些：

- **组装前后有事件。**`system-prompt/assemble` 是一个会按 scope 过滤的 waterfall，可以在组装还在进行的时候就把结果改掉，而 `system-prompt/change` 会公告 registry 有变动。mini 的组装没有任何 hook。
- **tool 的顺序有明确规则。**真正的 dsh 在排这次 request 的 tool 列表时，会照一个写死的常量 `TOOL_ORDER_REST` 来排；mini 就只靠注册顺序。
- **registry 之外还有一条 context 通道。**`packages/context` 底下大部分的东西根本不走 `systemPrompt.context()`：`agent-instructions`、`time-context`、 `tmux-context` 都是从 `agent/pre-step` 的 listener 直接追加 `UserMessage`。真正会去调用 registry 那个 `context()` 的，是 sandbox 策略、审批策略，还有 subagent 的委派。真正的 sandbox 隔离在 Ceiling 之上；mini 那个改写 argv 的替身，要等 Section 10 讲 capability seam 的时候才会出现。
- **有些 section 可以慢慢来。**真正的 `PromptSection` 可以声明 `complete?`，让组装先往下走，慢的 provider 之后再把内容补上。mini 的 provider 都是同步的。

---

## Failure modes

- **system 文本里放一个时钟，每个 step 的缓存都会落空。**model 那一端是靠稳定的 prompt 前缀做缓存，而 system 文本就排在前缀的最前面。只要有一个时间戳每个 step 重算一次，就没有任何一次 request 用得到那个前缀。section 和 context 分成两边，等于从结构上就把所有会变的字节挡在 system 文本之外。
- **文本从旁边补进 request，就会从记录里消失。**状态补进了 request，却没有留下任何一条 log，重放的时候就重建不出 model 看到的东西。快照是一条 `user/message`，就是普通的推导历史；连 system 文本都会记在 `request/header` 上，所以 log 还是完整的故事。
- **没变也重发，历史会被灌爆。**每个 step 都把读数追加一次，等于后面每一次 request 都多背一条，却没多带任何信息。边界会拿它跟最后一条快照比一下，变了才追加。
- **比对用的快照放在内存里，它会跟 log 对不上。**重新开起来之后内存是空的，log 却不是，于是第一个 step 又把 model 早就看过的快照发一次。mini 是直接从 log 推出比对用的那份快照，所以去重和重放天生就对得上。
- **代入太宽松，会送出一个带洞的 prompt。**一个 `{{typo}}` 就这样以大括号的原样送到 model 面前，读起来就是一句没有意义的话。严格代入会改成抛出异常，而 log 上看得到这个 step 停在 `request/header` 之前：这次根本没有 request 送出去。
- **provider 没有顺序，文本就会乱跳。**如果算的时候照的是 dict 顺序或谁先跑完，同样的注册在不同次运行就可能算出不同的 prompt，前缀缓存又落空一次。一个数字顺序，同分照注册顺序，每次算出来的文本都一样。

---

## 跑跑看

[`src/`](src/) 把 07 搬过来，然后加上：

- [`system_prompt.py`](src/system_prompt.py)（新的）：`SystemPrompt`，四种 provider，每一次注册都给一个撤销函数；`assemble()`；`latest_snapshot()`；还有那个 plugin，内建 identity 和 tool schema 的桥都在里面。
- [`agent_loop.py`](src/agent_loop.py)：`_step()` 每个 step 组装一次，快照变了就追加一条，并把 system 文本经由 Model seam 传下去；`Agent` 和 `create()` 多了 `prompt` 参数。
- [`standin.py`](src/standin.py)：seam 的签名多了 `system=""`；Scripted stand-in 一样不去看它。
- [`test.py`](src/test.py)：Offline check 证明三样东西会落在同一次 request 里； turn 中途 tick 一下会让快照重发，而 system 文本一个字节都没变；去重在同一个 turn 内和跨 turn 都成立；`{{variable}}` 不认得或没设值，会让这个 step 停在任何 request 送出去之前；每一次注册都撤销得掉。
- [`demo.py`](src/demo.py)：Live demo 在内建 identity 上面叠一段 persona，把真的时钟和 cwd 拍成快照，再让一个很慢的 tool 逼出一次 turn 中途的重发，整段跑在真的 model 调用上。

```bash
python sections/08-system-prompt/src/test.py    # offline check, no key
```

Live demo 需要根目录的 `requirements.txt` 和一把 key；没有 key 的话，它会安静地跳过：

```bash
pip install -r requirements.txt         # anthropic + python-dotenv
cp .env.example .env                    # then set ANTHROPIC_API_KEY
python sections/08-system-prompt/src/demo.py
```

---

## 出处

- [`docs/subsystems/system-prompt.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/subsystems/system-prompt.md)： dsh 自己带你走一遍那四种 provider，还有算出来的那三样东西。
- [`packages/context/README.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/context/README.md)： context 这一整个包家族，还有里面哪些成员走 registry、哪些走 pre-step 那条通道。
