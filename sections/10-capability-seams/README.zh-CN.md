<!-- source: README.md @ 3705bd7 -->

# 10 · Capability seams

[English](README.md) | [繁體中文](README.zh-TW.md) | 简体中文

> 如果把能力直接写进工具实现，模型看到的 schema、能力契约与实际运行环境就会绑在一起。但若一开始就把所有能力拆成三层，又会产生大量没有替换需求的抽象。只有当使用端不应知道实际由哪个后端运行时，才需要创建 capability seam。

到了第 10 章，Mini-dsh 仍然不会访问 session log 以外的资源。无论是读取文件还是运行指令，第一个实际能力都需要一个落点，而最直接的位置就是工具本体。

但这样会把三个独立决策写进同一个函数：模型看到的 schema、对外契约，以及真正运行工作的后端。离线测试可能需要内存文件系统，本机环境需要真实磁盘，受限环境则可能禁止文件访问。如果为每种环境重写工具，模型用来规划的 schema 也会一起变动。

但过早抽象也会让架构变得笨重。如果每个能力一开始就拆成接口、后端软件包和工具软件包，harness 会充满只有一种实现、从未真正被替换的抽象层。

因此，本章要回答：一个能力在什么时候，才值得拆成 Definition、Provider 和 Consumer 三个角色？

答案是：当某个使用端不应知道实际由哪个后端提供能力时，或当第二种后端已经出现时。一旦决定创建 seam，它必须满足：

1. 每个 seam 只定义一次：包含抽象基类、ctx key 与专用词汇，只描述契约，不负责实现。
2. Provider 一律以 plugin 挂载：每个 key 只对应一份实现，撤销动作由 fiber 管理；fs、shell、sandbox 等独占 key 若重复挂载，立即报错。
3. Consumer 不应知道 Provider：工具要到运行时才解析 ctx key，并且只使用抽象基类定义的方法。更换后端不会改变模型看到的 schema。
4. sandbox 是运行围篱，不是模型工具：它只提供 `confine(argv, policy)`，由其他 seam 的 Provider 调用；遇到未知 policy 时一律拒绝。
5. llm 要折在一起：Definition 和 Consumer 放在同一个 service 里，adapter 就是符合 Model seam 形状的普通 callable，用名字分成很多个，每次调用才解一次名字。
6. 错误要在工具边界转换：找不到 Provider 或 policy 遭拒时，都返回一般 `is_error` 结果，让 turn 正常收尾。

---

## 核心机制

只添加一个文件 `capabilities.py`，前面沿用的文件都没有修改：

- **Definition**：`FileSystem`（read、write）、`ShellExecutor`（run）、`SandboxProvider`（confine）三个抽象基类，各自指名一个 ctx key。抽象基类、key、词汇，这三样就是这个角色的全部；Definition 不带任何真的会做事的代码。
- **`provider()`**：将 Provider 包成 plugin factory。kernel 的 `provide()` 会返回撤销函数并拒绝重复 key，因此独占 seam 不需要额外管理生命周期或重复挂载检查。
- **`capability_tools_plugin`**：这里放的是 Consumer。`read`、`write`、`shell` 三个 tool 要到运行的当下才用 `ctx.get()` 去解自己的 seam，而且只讲抽象基类的动词；没有任何一个 tool 去 import Provider。这条 import 的纪律就是 seam 本身。
- **两个转折**：sandbox 这个 seam 有 Provider 却没有 tool，llm 这个 seam 有 service 却没有抽象基类。每一个转折，都是同一个设计问题换另一种方式回答。

先看 sandbox 这个转折。它唯一的动词会照指定的 policy 改写一组 argv，遇到不认识的 policy 就直接拒绝，而不是让 argv 没被围住就过去：

```python
def confine(self, argv, policy):
    if policy not in self._policies:  # fail closed: never run unfenced
        raise ValueError(f"unknown sandbox policy '{policy}'")
    return [SANDBOX_ARGV_MARKER, "--policy", policy, "--", *argv]
```

`confine` 不会出现在模型可见的工具中。sandbox 的 Consumer 是其他 seam 的 Provider，它负责限制模型已通过其他 schema 核准的工作：

```python
class SandboxedShellExecutor(ShellExecutor):
    """Provider built on another seam: run everything through the fence."""

    def run(self, argv):
        return self._inner.run(self._sandbox.confine(argv, self._policy))
```

llm 这个转折折的是另一个方向。它的 Consumer 就是 agent loop 自己，也就是从第 04 章开始每个 Agent 都收的那个 `model` 参数，所以另外帮 Consumer 开一个家，只会画出一条永远没人跨过去的界线。而且 Model seam 本身就已经是契约了：一个先串出好几个 chunk、最后给一则 Message 的普通 callable，根本不需要抽象基类。剩下的只有数量这件事：一份用名字记住 adapter 的 registry，加上 `model(name)` 晚一点才解名字，这样连正在跑的 agent 都换得掉：

```python
def model(self, name):
    """The Model seam bound to an adapter name, resolved per call."""

    def seam(messages, tools=(), system=""):
        adapter = self._adapters.get(name)
        if adapter is None:
            raise LookupError(f"no llm adapter registered under '{name}'")
        return adapter(messages, tools, system)

    return seam
```

```text
the three roles, one seam (fs)

Definition   FileSystem ABC: read, write; one ctx key "fs"
Provider     provide("fs", MemoryFileSystem({...}))   undo on the fiber;
                                                      a second mount raises
Consumer     read/write tools: ctx.get("fs") per call, the ABC's verbs only

the sandbox bend: consumed by a provider, never by a tool

shell tool ──► ctx.get("shell").run(["echo", "hi"])
                 SandboxedShellExecutor              a shell provider,
                   │ confine(["echo", "hi"], ...)    consuming the sandbox seam
                   │  ├─ known policy: prepend the fence marker
                   │  └─ unknown policy: raise; fail closed, nothing runs
                 EchoShellExecutor.run(fenced argv)  the inner provider
tool/result   "mini-sandbox --policy read-only -- echo hi"
```

以下是实际运行时的 log。两个 turn 读取同一路径；中间卸载第一个 fs Provider，再由另一份实现接手相同 key。agent 本身完全不需修改：

```text
send("read it")                 provide("fs", A), notes.txt = "alpha"
  │   0  turn/start
  │   1  step/start
  │   2  user/message   "read it"
  │   3  request/header tools [read, write, shell]
  │   4  assistant/message {"tool_calls": [read "notes.txt"]}
  │   5  tool/call      read {"path": "notes.txt"}
  │   6  tool/result    "alpha"                  ◄ the machine's answer
  │   7  step/end       {"reason": null}
  │   8  step/start
  │   9  request/header tools [read, write, shell]
  │  10  assistant/chunk "do"
  │  11  assistant/chunk "ne"
  │  12  assistant/message "done"
  │  13  step/end       {"reason": "completed"}
  │  14  turn/end

A's undo runs; provide("fs", B), notes.txt = "beta"

send("read it again")
  │  15  turn/start
  │  ...
  │  18  request/header tools [read, write, shell] ◄ byte-identical offer,
  │  ...                                             same system text
  │  21  tool/result    "beta"                     ◄ only the machine changed
  │  ...
  │  29  turn/end
```

这个对比正好说明 seam 的作用：更换前后，log 中的 `request/header` 完全相同，只有 `tool/result` 反映后端差异。

### 改了什么

与第 09 章相比：

- 所有既有文件都完整沿用：`agent_loop.py`、`inbox.py`、`kernel.py`、`message.py`、`scheduler.py`、`session_log.py`、`skills.py`、`standin.py`、`system_prompt.py`、`tools.py`。`capabilities.py` 是唯一添加的源代码文件，因此与第 09 章相比，diff 只包含本章添加的机制，不包含其他改动。
- 这项机制一样是纯粹的 plugin：Consumer 从第 05 章的 registry 进来，Provider 从 kernel 的 `provide()` 进来，折起来的 llm 则走 loop 从第 04 章就一直在收的那个 model 参数。要做这个拆分不用加任何框架，只要守住谁可以 import 谁。
- Model seam 多了一个 service 当家，形状却没变：`llm.model(name)` 还是那个先串 chunk、最后给一则 Message 的普通 callable，所以 `ScriptedModel` 和 `live_model` 一行都不用改就能注册成 adapter。
- log 没有多出任何新的事件类型。换后端这件事，只会表现成同样的 `request/header` 底下，`tool/result` 那几行不一样。
- `demo.py`：在线示例通过 llm runtime 挂上真正的 Anthropic adapter，在两个 turn 之间换掉 fs 的后端，再让 model 自己说出 sandbox 替身围出来的 argv 长什么样。

---

## 对照真正的 dsh

以下链接皆指向研究版本 [`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca)。每个 seam 都是一组软件包系列：[`packages/fs`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/fs)、[`packages/shell`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/shell)、[`packages/sandbox`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/sandbox)、[`packages/llm`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/llm)。

| Mini-dsh | 真正的 dsh | 说明 |
| --- | --- | --- |
| `FileSystem` 抽象基类，一个 `"fs"` key | [`packages/fs/fs/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/fs/fs/src/index.ts)：`FileSystem` | 真正的 Definition 是 `abstract class FileSystem extends Service`，它拥有 `ctx.fs`（第 86 行）：继承 `Service` 会把 key 和契约一起带进来，不会只留下一个光秃秃的接口。 |
| `provider("fs", MemoryFileSystem(...))` | [`packages/fs/fs-local/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/fs/fs-local/src/index.ts)：`LocalFileSystem`、[`packages/fs/fs-sandbox/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/fs/fs-sandbox/src/index.ts)：`SandboxedFileSystem` | 出货的 Provider。有 sandbox 的那个 fs 会通过 `ctx.sandboxPolicy`（第 127 行）把路径围起来，那是 sandbox 的第二个对外接口，Mini-dsh 把它折进 `confine` 的 policy 名字里。 |
| `read`/`write` 这两个 tool | [`packages/fs/tool-fs/src/read.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/fs/tool-fs/src/read.ts) 和它的邻居 | 这里是 Consumer：`read`、`write`、`edit`、`read_image`，另外 `glob` 和 `grep` 放在 `packages/fs` 的别处。没有任何一份 tool schema 提到后端的名字。 |
| `ShellExecutor`，独占挂载 | [`packages/shell/shell/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/shell/shell/src/index.ts)：`ShellExecutor` | `ctx.shell`（第 65 行）在一个 context 里只准一份实现；注册第二次就丢例外（第 48 到 50 行）。mini 这边是 kernel 的 `provide()` 给出同样的拒绝。 |
| `SandboxedShellExecutor` | [`packages/shell/bash-sandbox/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/shell/bash-sandbox/src/index.ts)：`SandboxBashExecutor` | 它会调用 `ctx.sandbox.confine(['bash', '-c', command], policy)`（第 178 行）：一个用到 sandbox seam 的 shell Provider，也就是 mini 那个外面再包一层的做法，只是后面接的是真机器。 |
| `ArgvRewriteSandbox.confine` | [`packages/sandbox/sandbox/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/sandbox/sandbox/src/index.ts)：`SandboxProvider` | `confine(argv, policy)` 是这个 Definition 唯一的抽象方法（第 158 行）；这个 seam 不拥有任何 tool，也不拥有任何事件。 |
| `LlmRuntime` | [`packages/llm/llm/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/llm/llm/src/index.ts)：`LlmRuntime`、`LlmAdapter` | Definition 和 Consumer 折在同一个软件包里：`ctx.llm`（第 284 行）是给 loop 用的，adapter 则继承 `LlmAdapter`（第 180 行）。像 [`llm-deepseek`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/llm/llm-deepseek/src/index.ts) 这样的 Provider 通过 `ctx.llm.registerAdapter` 注册进来。 |

真正的 seam 还提供以下功能：

- **真正的隔离机制。** `sandbox-local` 会串接各平台运行器：Linux 使用 `bwrap` 与 `landlock`，Darwin 使用 `seatbelt`（第 160 行），另有 Windows ACL Provider。Mini-dsh 只用 argv 改写保留 seam 形状与 fail-closed 规则，并没有实现真正的隔离。
- **事件由 seam 自己拥有。** fs 的 Definition 自己拥有 `fs/write-intent` 和 `fs/edit-intent` 两个 waterfall，再加一个 `fs/observed` 的 emit，所以在任何 Provider 看到这次写入之前，plugin 就可以否决它或改写它；llm 拥有一个给中介层用的 `llm/stream` waterfall。shell 和 sandbox 一个事件都没有：一个 Definition 对外的样子，就是它那几个动词，加上它自己声明的那些事件。
- **adapter 的分流。** `registerAdapter(providers, adapter)` 绑的是 model 名字的前缀，runtime 再照每个请求的 model id 去分流。mini 是在建 agent 的时候绑一个名字，每次调用才去解；晚绑这件事两边一样，只是拿来分流的键小很多。
- **架构笔记明确定义拆分时机。** dsh 不会预先拆分能力；只有一个 Provider 与一个 Consumer 时先放在同一软件包，等第二种实现出现再创建 seam。`dsh-llm` 是长期例外，因为它的 Consumer 就是 loop。

---

## 常见失败模式

- **工具直接 import Provider 会让 seam 失去意义。** 如果 `read` 自行创建后端或直接读取磁盘，更换环境就必须修改工具，也可能产生不同 schema。正确做法是每次调用都解析 `"fs"`，并只使用抽象契约中的方法。
- **独占 Provider 重复挂载若未报错，行为就取决于挂载顺序。** 两个 shell 同时存在时，系统无法明确判断哪个负责运行。独占 key 应在第二次挂载时立即拒绝，提早暴露设置错误。
- **sandbox 遇到错误时放行，比完全没有隔离更危险。** 未知 policy 若直接返回原 argv，设置错误就会在没有隔离的情况下运行。`confine()` 应直接抛错，再由工具 pipeline 转成 `is_error`，确保指令不会运行。
- **将 sandbox 暴露成模型工具，会把隔离决策交给模型。** `confine` 不应出现在 schema 中；它应位于 Provider 内部，强制套用在已核准的工作上，模型无法选择略过。
- **提前拆分只是多余的重量。** 帮 llm 开一个抽象基类，可是它的 Consumer 只有一个，而且永远不会变，那只是多画一条没人会跨的界线；adapter 早就以普通 callable 的身份躲在 `model(name)` 后面换来换去了。三份拆分是靠一个不能知道自己 Provider 是谁的 Consumer 换来的，不是靠对称好看。

---

## 动手验证

[`src/`](src/) 延续第 09 章，并加入：

- [`capabilities.py`](src/capabilities.py)（添加）：三个 seam 的抽象基类和它们的 Provider（`MemoryFileSystem`、`EchoShellExecutor`、`ArgvRewriteSandbox`、`SandboxedShellExecutor`）、`provider()` 这个 plugin 工厂、折起来的 `LlmRuntime`，还有那几个 Consumer tool。
- [`test.py`](src/test.py)：离线测试证明几件事：在 schema 一模一样的前提下，换后端会换出不同的结果；独占的 seam 会拒绝第二次挂载；sandbox 改写过的 argv 会通过 shell Provider 一路写进 log；不认识的 policy 和没挂 Provider，两种情况都回正常的错误结果；llm 的 adapter 可以用名字并存，而且能在 agent 活着的时候换掉。
- [`demo.py`](src/demo.py)：在线示例通过 llm runtime 用真正的 model，在两个 turn 之间换掉 fs 的后端，再让 model 说出 sandbox 替身围出来的那串 argv。

```bash
python sections/10-capability-seams/src/test.py    # offline check, no key
```

在线示例需要根目录的 `requirements.txt` 和一把 key；没有设置 key 时会自动跳过：

```bash
pip install -r requirements.txt         # anthropic + python-dotenv
cp .env.example .env                    # then set ANTHROPIC_API_KEY
python sections/10-capability-seams/src/demo.py
```

---

## 参考资料

- [`docs/glossary.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/glossary.md)：dsh 自己对 Service Definition、Service Provider、Service Consumer 的定义。
- [`.agents/notes/implemented/architecture/2026-06-13-capability-seams.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/.agents/notes/implemented/architecture/2026-06-13-capability-seams.md)：决定了三份拆分和「不预先拆」这条规则的那份架构笔记。
