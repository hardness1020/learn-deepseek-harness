<!-- source: README.md @ d01aaee -->

# 10 · Capability seams

[English](README.md) | [繁體中文](README.zh-TW.md) | 简体中文

> model 看到的是一个 tool，tool 看到的是一份契约，只有 provider 看得到底下那台机器。把机器换掉，另外两个房间完全不会知道。

走到第十个 Section，mini-dsh 还是碰不到自己那份 log 以外的任何东西。第一个真正的能力，不管是读一个文件还是跑一条命令，总得住在某个地方，而最顺手的地方就是 tool 的本体。

但这个位置会把三件事焊死在同一个 function 里：model 看到什么、契约长什么样、由哪一台机器来兑现。Offline check 要的是内存，你自己那台机器要的是磁盘，锁得很紧的主机要的是一道围栏；差一种环境就要重写一次 tool，连 model 拿来规划的 schema 都跟着晃。

反过来把场面做足也一样会坏。第一天就给每个能力配一套接口、一个后端包、一个 tool 包，harness 就会被一堆只有一种实现、从来没人换过的抽象淹掉。

所以：一个能力要到什么时候，才值得拆成三份？

答案是：当有一段代码不能知道回答它的是哪一台机器的时候，比如一个面对 model 的 Consumer，或是一个排在后面等着上场的第二个后端。只要这个拆分值得做，seam 就必须做到这几件事：

1. 每个 seam 只定义一次：一个抽象基类、一个 ctx key，加上这个 seam 的词汇；它只拥有契约，别的什么都不管。
2. Provider 一律当 plugin 挂上去：一个 key 底下挂一份实现，撤销的动作挂在 fiber 上；独占的 key（fs、shell、sandbox）被挂第二次的时候要当场报错。
3. 让 Consumer 看不到 Provider：tool 的本体要到运行的当下才去解那个 key，而且只讲抽象基类定义的那几个动词，所以换掉后端不会改变 model 看到的东西。
4. 把 sandbox 做成一道围栏，不是一个 tool：只有一个动词 `confine(argv, policy)`，由别的 seam 的 Provider 来用，遇到不认识的 policy 就拒绝。
5. llm 要折在一起：Definition 和 Consumer 放在同一个 service 里，adapter 就是符合 Model seam 形状的普通 callable，用名字分成很多个，每次调用才解一次名字。
6. 出事要在 tool 这道门口就降级：没有 Provider、或是 policy 被拒绝，都变成一条正常的 `is_error` 结果，让这个 turn 自己好好收尾。

---

## Mechanism

只新增一个文件 `capabilities.py`，前面搬过来的文件一个都没动：

- **Definition**：`FileSystem`（read、write）、`ShellExecutor`（run）、`SandboxProvider`（confine）三个抽象基类，各自指名一个 ctx key。抽象基类、key、词汇，这三样就是这个角色的全部；Definition 不带任何真的会做事的代码。
- **`provider()`**：把 Provider 这个角色做成一个 plugin 工厂。记账的事 kernel 早就做好了：`provide()` 会交回一个撤销用的函数，而且拒绝重复的 key，所以独占的 seam 不用多写一行，挂上去的当下就会报错。
- **`capability_tools_plugin`**：这里放的是 Consumer。`read`、`write`、`shell` 三个 tool 要到运行的当下才用 `ctx.get()` 去解自己的 seam，而且只讲抽象基类的动词；没有任何一个 tool 去 import Provider。这条 import 的纪律就是 seam 本身。
- **两个转折**：sandbox 这个 seam 有 Provider 却没有 tool，llm 这个 seam 有 service 却没有抽象基类。每一个转折，都是同一个设计问题换另一种方式回答。

先看 sandbox 这个转折。它唯一的动词会照指定的 policy 改写一组 argv，遇到不认识的 policy 就直接拒绝，而不是让 argv 没被围住就过去：

```python
def confine(self, argv, policy):
    if policy not in self._policies:  # fail closed: never run unfenced
        raise ValueError(f"unknown sandbox policy '{policy}'")
    return [SANDBOX_ARGV_MARKER, "--policy", policy, "--", *argv]
```

没有人会把 `confine` 端到 model 面前。sandbox 的 Consumer 是别的 seam 的 Provider，所以这道围栏包住的，是 model 早就通过别的 schema 要求过的工作：

```python
class SandboxedShellExecutor(ShellExecutor):
    """Provider built on another seam: run everything through the fence."""

    def run(self, argv):
        return self._inner.run(self._sandbox.confine(argv, self._policy))
```

llm 这个转折折的是另一个方向。它的 Consumer 就是 agent loop 自己，也就是从 Section 04 开始每个 Agent 都收的那个 `model` 参数，所以另外帮 Consumer 开一个家，只会画出一条永远没人跨过去的界线。而且 Model seam 本身就已经是契约了：一个先串出好几个 chunk、最后给一条 Message 的普通 callable，根本不需要抽象基类。剩下的只有数量这件事：一份用名字记住 adapter 的 registry，加上 `model(name)` 晚一点才解名字，这样连正在跑的 agent 都换得掉：

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

下面是一次真的运行，log 就是这样记的。两个 turn 读同一个路径；中间第一个 fs Provider 的撤销跑掉了，换另一份实现接手同一个 key。agent 完全没被动过：

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

seam 的证据就在这个对比上：换前换后，log 里每一行 `request/header` 都一模一样，只有 `tool/result` 那几行看得出后端换了。

### 改了什么

跟 Section 09 比起来：

- 每一个搬过来的文件都原封不动：`agent_loop.py`、`inbox.py`、`kernel.py`、`message.py`、`scheduler.py`、`session_log.py`、`skills.py`、`standin.py`、`system_prompt.py`、`tools.py`。`capabilities.py` 是唯一新增的源代码文件，所以拿 09 来 diff，跑出来的就是这个 Section 的 Mechanism，没有别的。
- 这个 Mechanism 一样是纯粹的 plugin：Consumer 从 Section 05 的 registry 进来，Provider 从 kernel 的 `provide()` 进来，折起来的 llm 则走 loop 从 Section 04 就一直在收的那个 model 参数。要做这个拆分不用加任何框架，只要守住谁可以 import 谁。
- Model seam 多了一个 service 当家，形状却没变：`llm.model(name)` 还是那个先串 chunk、最后给一条 Message 的普通 callable，所以 `ScriptedModel` 和 `live_model` 一行都不用改就能注册成 adapter。
- log 没有多出任何新的事件类型。换后端这件事，只会表现成同样的 `request/header` 底下，`tool/result` 那几行不一样。
- `demo.py`：Live demo 通过 llm runtime 挂上真正的 Anthropic adapter，在两个 turn 之间换掉 fs 的后端，再让 model 自己说出 sandbox 替身围出来的 argv 长什么样。

---

## In real dsh

所有指过去的链接都固定在 Studied version [`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca) 上。每个 seam 都是一组包家族：[`packages/fs`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/fs)、[`packages/shell`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/shell)、[`packages/sandbox`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/sandbox)、[`packages/llm`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/llm)。

| Mini-dsh | 真正的 dsh | 说明 |
| --- | --- | --- |
| `FileSystem` 抽象基类，一个 `"fs"` key | [`packages/fs/fs/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/fs/fs/src/index.ts)：`FileSystem` | 真正的 Definition 是 `abstract class FileSystem extends Service`，它拥有 `ctx.fs`（第 86 行）：继承 `Service` 会把 key 和契约一起带进来，不会只留下一个光秃秃的接口。 |
| `provider("fs", MemoryFileSystem(...))` | [`packages/fs/fs-local/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/fs/fs-local/src/index.ts)：`LocalFileSystem`、[`packages/fs/fs-sandbox/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/fs/fs-sandbox/src/index.ts)：`SandboxedFileSystem` | 出货的 Provider。有 sandbox 的那个 fs 会通过 `ctx.sandboxPolicy`（第 127 行）把路径围起来，那是 sandbox 的第二个对外接口，这次重建把它折进 `confine` 的 policy 名字里。 |
| `read`/`write` 这两个 tool | [`packages/fs/tool-fs/src/read.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/fs/tool-fs/src/read.ts) 和它的邻居 | 这里是 Consumer：`read`、`write`、`edit`、`read_image`，另外 `glob` 和 `grep` 放在 `packages/fs` 的别处。没有任何一份 tool schema 提到后端的名字。 |
| `ShellExecutor`，独占挂载 | [`packages/shell/shell/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/shell/shell/src/index.ts)：`ShellExecutor` | `ctx.shell`（第 65 行）在一个 context 里只准一份实现；注册第二次就抛异常（第 48 到 50 行）。mini 这边是 kernel 的 `provide()` 给出同样的拒绝。 |
| `SandboxedShellExecutor` | [`packages/shell/bash-sandbox/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/shell/bash-sandbox/src/index.ts)：`SandboxBashExecutor` | 它会调用 `ctx.sandbox.confine(['bash', '-c', command], policy)`（第 178 行）：一个用到 sandbox seam 的 shell Provider，也就是 mini 那个外面再包一层的做法，只是后面接的是真机器。 |
| `ArgvRewriteSandbox.confine` | [`packages/sandbox/sandbox/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/sandbox/sandbox/src/index.ts)：`SandboxProvider` | `confine(argv, policy)` 是这个 Definition 唯一的抽象方法（第 158 行）；这个 seam 不拥有任何 tool，也不拥有任何事件。 |
| `LlmRuntime` | [`packages/llm/llm/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/llm/llm/src/index.ts)：`LlmRuntime`、`LlmAdapter` | Definition 和 Consumer 折在同一个包里：`ctx.llm`（第 284 行）是给 loop 用的，adapter 则继承 `LlmAdapter`（第 180 行）。像 [`llm-deepseek`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/llm/llm-deepseek/src/index.ts) 这样的 Provider 通过 `ctx.llm.registerAdapter` 注册进来。 |

真正的 seam 在这个 Section 的 Mechanism 之上，还多做了这些：

- **真的关得住。** `sandbox-local` 会串起各平台的运行器：linux 上是 `bwrap` 和 `landlock`，darwin 上是 `seatbelt`（第 160 行），还有一个 Windows ACL 的 Provider。那一整套机器就是这次重建的 Ceiling：只会改写 argv 的替身留住了 seam 的形状，也留住了出事就关死的规则，但它其实什么都挡不住；真正的隔离只在这里指给你看，不重建。
- **事件由 seam 自己拥有。** fs 的 Definition 自己拥有 `fs/write-intent` 和 `fs/edit-intent` 两个 waterfall，再加一个 `fs/observed` 的 emit，所以在任何 Provider 看到这次写入之前，plugin 就可以否决它或改写它；llm 拥有一个给中间件用的 `llm/stream` waterfall。shell 和 sandbox 一个事件都没有：一个 Definition 对外的样子，就是它那几个动词，加上它自己声明的那些事件。
- **adapter 的分流。** `registerAdapter(providers, adapter)` 绑的是 model 名字的前缀，runtime 再照每个请求的 model id 去分流。mini 是在建 agent 的时候绑一个名字，每次调用才去解；晚绑这件事两边一样，只是拿来分流的键小很多。
- **这个拆分是白纸黑字的规定。** dsh 的架构笔记把这个 Section 在问的规则写死了：能力不预先拆，一个 Provider 配一个 Consumer 就先待在同一个包里，等第二个出现再说；`dsh-llm` 是那个长期的例外，因为它的 Consumer 就是 loop。

---

## Failure modes

- **一个去 import Provider 的 tool，等于把 seam 焊死。** 如果 `read` 的本体自己生一个后端出来，或是自己去开磁盘，那换机器就等于改 tool，每一种环境都会分岔出一份自己的 schema。本体每次调用都去解 `"fs"`，而且只讲抽象基类的动词；那条 import 的纪律就是 seam。
- **安静挂上去的第二个 Provider，等于出货一个配置 bug。** 让两个 shell 都安静地挂上去，那到底是哪一台机器在跑这条命令，就取决于一个没人在读的挂载顺序。独占的 key 在挂载的当下就拒绝第二个，比任何一次调用挑错都还早。
- **一个出事就放行的 sandbox，比没有还糟。** 遇到不认识的 policy 就把 argv 原封不动还回去，那每一次配置错误都会在没有围栏的情况下跑起来，而且看不见。`confine()` 直接抛异常，Section 05 的 pipeline 回一条正常的 `is_error` 结果，什么都不会跑。
- **把 sandbox 做成给 model 用的 tool，等于守错了门。** 把 `confine` 写进 schema，要不要围就变成 model 自己决定。sandbox 的 Consumer 是别的 seam 的 Provider：这道围栏包住的是已经批准过的工作，位置在 schema 底下，没有人开得了口叫它别围。
- **提前拆分只是多余的重量。** 帮 llm 开一个抽象基类，可是它的 Consumer 只有一个，而且永远不会变，那只是多画一条没人会跨的界线；adapter 早就以普通 callable 的身份躲在 `model(name)` 后面换来换去了。三份拆分是靠一个不能知道自己 Provider 是谁的 Consumer 换来的，不是靠对称好看。

---

## 跑跑看

[`src/`](src/) 把 09 搬过来，再加上：

- [`capabilities.py`](src/capabilities.py)（新增）：三个 seam 的抽象基类和它们的 Provider（`MemoryFileSystem`、`EchoShellExecutor`、`ArgvRewriteSandbox`、`SandboxedShellExecutor`）、`provider()` 这个 plugin 工厂、折起来的 `LlmRuntime`，还有那几个 Consumer tool。
- [`test.py`](src/test.py)：Offline check 证明几件事：在 schema 一模一样的前提下，换后端会换出不同的结果；独占的 seam 会拒绝第二次挂载；sandbox 改写过的 argv 会通过 shell Provider 一路写进 log；不认识的 policy 和没挂 Provider，两种情况都回正常的错误结果；llm 的 adapter 可以用名字并存，而且能在 agent 活着的时候换掉。
- [`demo.py`](src/demo.py)：Live demo 通过 llm runtime 用真正的 model，在两个 turn 之间换掉 fs 的后端，再让 model 说出 sandbox 替身围出来的那串 argv。

```bash
python sections/10-capability-seams/src/test.py    # offline check, no key
```

Live demo 需要根目录的 `requirements.txt` 和一把 key；没有 key 的话，它会安静地跳过：

```bash
pip install -r requirements.txt         # anthropic + python-dotenv
cp .env.example .env                    # then set ANTHROPIC_API_KEY
python sections/10-capability-seams/src/demo.py
```

---

## 出处

- [`docs/glossary.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/glossary.md)：dsh 自己对 Service Definition、Service Provider、Service Consumer 的定义。
- [`.agents/notes/implemented/architecture/2026-06-13-capability-seams.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/.agents/notes/implemented/architecture/2026-06-13-capability-seams.md)：决定了三份拆分和“不预先拆”这条规则的那份架构笔记。
