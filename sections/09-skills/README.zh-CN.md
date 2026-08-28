<!-- source: README.md @ 3705bd7 -->

# 09 · Skills

[English](README.md) | [繁體中文](README.zh-TW.md) | 简体中文

> skill 内容往往很长，不适合每个 step 都重复发送；但如果完全不提供，模型也不知道它们存在。因此，请求只带上 skill 名称与摘要，完整内容等到真正需要时再加载。

第 08 章的请求已经包含稳定的 system prompt 和可变的 runtime context，但其中每个字仍会在每个 step 重复发送。随着 harness 累积越来越多专用操作指南，将所有内容都放进请求会快速消耗 context 预算，而一个 turn 通常只会用到其中少数几项。

全部写入 system prompt，会让每次请求都支付所有 skill 的 token 成本，不论是否使用。如果什么都不提供，模型又无法主动选用它不知道的 skill。

此外，skill 并非固定不变。内置功能、工作区和 plugin 都可以提供 skill，并在 session 运行期间动态挂载、卸载或覆写同名项目，各来源之间不应直接修改彼此的内容。

因此，本章要回答的问题是：为什么 skill 列表以 context 形式注入，完整内容却要通过工具调用才加载？

模型需要以低成本随时知道「有哪些 skill」，但只在使用时才加载「skill 的完整内容」。registry 因此需要：

1. registry 保存的是 provider，而不是 skill 本身。每个 provider 通过 `list()` 提供摘要，通过 `get(name)` 提供完整内容。
2. provider 采分层设计：后注册的同名项目会覆写先前版本，每次注册都会返回撤销函数。
3. 列表会随 runtime-context 快照一起注入，内容包含名称与一行说明，只有列表变更时才重发。
4. 完整内容通过 `skill` 工具按需加载，并以一般 `tool/result` 写入历史。
5. 名称不存在时，返回一般错误结果，不让例外穿过工具边界。
6. 列表是空的时候，什么都不送。

---

## 核心机制

本章只添加 `skills.py`，其他文件维持不变：

- **`SkillRegistry`**：一层一层的 provider，照注册顺序叠。`catalog()` 把每个 provider 的 `list()` 摘要合起来，看得到的名字每个一行，同名的话后面那层的那行赢。`get(name)` 反过来从最上层往回走，返回找到的第一份内容。`register()` 照 kernel 的做法返回撤销函数。
- **`MemorySkillProvider`**：最简单的 provider，就是一个 `name -> {"description", "body"}` 的 dict。任何对象只要有 `list()` 和 `get(name)` 就算 provider；`list()` 绝不会主动把内容端出来。
- **`skills_plugin`**：把这个分工接起来。一个第 08 章的 context provider 把 `catalog_text()` 算进快照，一个 `skill` tool 负责加载内容，registry 本身则以 `skills` 这个名字提供出去。

```python
def catalog(self):
    """One summary per visible name; a later layer's line wins."""
    merged = {}
    for provider in self._providers:
        for summary in provider.list():
            merged[summary["name"]] = summary
    return list(merged.values())

def get(self, name):
    """One full body, nearest layer first; None if no layer knows the name."""
    for provider in reversed(self._providers):
        body = provider.get(name)
        if body is not None:
            return body
    return None
```

列表不需要新的投递机制，只要作为第 08 章 registry 中的一个 context provider。快照去重会决定何时再次写入 log：provider 改变时重发，列表不变时不增加额外 token。

```python
ctx.effect(
    ctx.get("system_prompt").context(
        "skills", lambda ac: skills.catalog_text(), order=100
    ),
    "skill catalog",
)
```

内容走的是另一条路：第 05 章盖好的那条 tool pipeline。名字不认得的时候，tool 的实现里会丢出例外，pipeline 再把它变成一则正常的 `is_error` 结果，所以对话记录的形状不会被弄坏：

```text
registered, layered              every step (the Section 08 plane)

built-in   greet, haiku ─┐ catalog() ─► skills, load with the      ─► same as the last
workspace  greet         ┘             skill tool before use:         snapshot row?
  (shadows the built-in)               - greet: <workspace's line>    ├─ yes: nothing
                                       - haiku: answer as a haiku     └─ no: user/message

on demand, mid-turn (the model asks)

tool/call    skill {"name": "haiku"}
               │  get("haiku"): reverse layer walk, first body wins
tool/result  the full instruction text, an ordinary row
```

以下是实际运行时的 log。列表中有两个 skill；模型按需加载其中一份内容并照着运行，第二个 step 则确认列表没有变化：

```text
send("hi")
  │   0  turn/start
  │   1  step/start
  │   2  user/message   "hi"                       ◄ claimed at the boundary
  │   3  user/message   "skills, load with ..."    ◄ the catalog: names and
  │   4  request/header tools ["skill"]              one line each, no bodies
  │   5  assistant/message {"tool_calls": [skill "haiku"]}
  │   6  tool/call     skill {"name": "haiku"}
  │   7  tool/result   "Answer with one haiku: ..." ◄ the body, on demand
  │   8  step/end      {"reason": null}
  │   9  step/start                                ◄ catalog unchanged:
  │  10  request/header                              no new snapshot row
  │  11  assistant/chunk "do"
  │  12  assistant/chunk "ne"
  │  13  assistant/message "done"
  │  14  step/end      {"reason": "completed"}
  │  15  turn/end
```

seq 7 的内容现在成为推导历史的一部分，也就是一则普通的 `tool` 消息。后续 request 会持续携带它，但这是模型主动要求加载的结果。`greet` 从未被要求，因此完整内容不会产生任何 token 成本。

### 改了什么

与第 08 章相比：

- 所有既有文件都完整沿用：`agent_loop.py`、`inbox.py`、`kernel.py`、 `message.py`、`scheduler.py`、`session_log.py`、`standin.py`、 `system_prompt.py`、`tools.py`。`skills.py` 是唯一的新源代码文件，因此与第 08 章相比，diff 只包含本章添加的机制，不包含其他改动。
- loop 完全没改，因为这项机制纯粹是 plugin：列表从第 08 章的 context provider 进来，内容从第 05 章的 tool 进来。这是第一个完全通过 plugin 组合完成的章节，不需要修改任何既有文件。
- log 没有多出新的事件类型。快照那一笔现在可能夹着列表那一段，`tool/result` 那一笔可能夹着一份 skill 内容；推导历史的时候，两者就是普通的记录，照普通的方式处理。
- `demo.py`：在线示例给实际模型一份列表，让它自己开口载一份内容，再趁两个 turn 之间注册第二个 provider，所以重发这件事会发生在一次实际模型调用上。

---

## 对照真正的 dsh

以下链接皆指向研究版本 [`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca)。registry 位于 skill 这个软件包系列里： [`packages/skill`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/skill)。

| Mini-dsh | 真正的 dsh | 说明 |
| --- | --- | --- |
| `skills.py` 里的 `SkillRegistry` | [`packages/skill/skill/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/skill/skill/src/index.ts)：`SkillRegistry` | 真正的 registry 继承 `Service`，挂在 `ctx.skills` 底下，跟 mini 一样是个复数形的 seam。它的层知道 scope（`SkillLayer implements ScopeLayer`）；mini 就只照注册顺序叠。 |
| provider 的 duck type（`list()` / `get(name)`） | [`index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/skill/skill/src/index.ts)：`SkillProvider` | 一个把名字换成指示文本的接口（第 248 行），不是 Service。注册时收的是一个工厂函数，它会拿到一个 `SkillProviderControl`（第 391 行），也就是 mini 那个撤销函数在真实世界里的样子。 |
| `MemorySkillProvider` | [`packages/skill/skill-filesystem/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/skill/skill-filesystem/src/index.ts)：`FileSystemSkillProvider` | 出货的那个 provider 是去磁盘上解 skill 目录的（第 146 行）；mini 用 dict 撑起来的 provider，让 离线测试完全不碰文件系统。 |
| 列表的 context provider | [`packages/skill/tool-skill/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/skill/tool-skill/src/index.ts) | 真正的使用端是从 `agent/pre-step` 的 listener 把列表发出去的（第 177、213 行），也就是第 08 章指过的那条 pre-step 通道。mini 没有 pre-step hook，所以它的列表改搭快照那条 context 通道。 |
| `skill` 这个 tool | [`tool-skill/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/skill/tool-skill/src/index.ts) | 内容一样是按需通过 tool 加载的（第 82 行）：列表和内容一样分成两边，也是靠同样那两条通道送出去。 |
| 以快照去重侦测变更 | [`index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/skill/skill/src/index.ts)：`skills/change` | 真正的 registry 会通过 bus 事件公告 provider 变更（第 297 行），让使用端清除缓存；Mini-dsh 则在每次组装时重算，再由快照去重避免重复写入。 |

真正的 skills 这一层还提供以下功能：

- **层知道 scope。**`SkillLayer implements ScopeLayer`，用的跟 tool registry 是同一套机制，所以 subagent 的 scope 可以看到跟父层不一样的列表。mini 的层是全域的；它那条覆盖规则是同一个想法，只是少了一个维度。
- **provider 手上有一个可以控制的 handle。**注册收的是一个工厂函数，它会拿到一个 `SkillProviderControl`，所以 provider 可以主动推变更通知，`skills/change` 事件再把通知扩散给有做缓存的使用端。mini 每次组装都重算一次列表，根本没有缓存需要作废。
- **有一个文件系统的 provider。**`FileSystemSkillProvider` 会走过 skill 目录，只读摘要、不载内容，所以省 token 这件事，在 I/O 这一层也一样守得住。
- **pre-step 那条投递通道。**真正的列表，是由 `agent/pre-step` 的 listener 追加成 `user/message` 的，`packages/context` 底下大部分东西走的都是这一条。mini 是通过第 08 章的 context registry，走到同样那几笔 log 记录。

---

## 常见失败模式

- **内容直接放进列表，等于永远为全部付钱。**把每一份指示都内嵌进去，每一次 request 就要扛着全部，可是一个 turn 最多用到一份。`list()` 只给名字和一行说明；`get(name)` 是内容唯一的出口。
- **把列表放进 system 文本会破坏稳定前缀。** session 中途挂载 provider 时，system prompt 会跟着改变，导致前缀缓存失效。改走 context 后，列表每次变更只增加一笔 `user/message`，system 文本仍保持不变。
- **未知名称的例外若穿过工具边界，会留下不完整的对话。** 模型可能拼错 skill 名称，因此 `skill` 工具的例外必须由第 05 章的 pipeline 转成一般 `is_error` 结果，让 turn 可以继续运行。
- **分层结果若依赖运行时序，列表就不稳定。** 若结果取决于 dict 顺序或线程完成时间，同一组注册可能产生不同列表，并触发不必要的快照。改用注册顺序决定覆写关系，结果就能保持一致。
- **列表一做缓存，就会跟 provider 对不上。**把算好的那一段缓存起来，某个注册已经被撤销的 provider 还会继续宣传一批根本解不出来的 skill。mini 每次组装都重算一次；让安静的 step 不花钱的是快照去重，不是缓存。

---

## 动手验证

[`src/`](src/) 延续第 08 章，并加入：

- [`skills.py`](src/skills.py)（新的）：`SkillRegistry`，provider 分层、同名互相覆盖的解法；`MemorySkillProvider`；还有那个 plugin，把列表的 context、 `skill` tool 和 `skills` 这个 service 接起来。
- [`test.py`](src/test.py)：确认快照只包含 skill 列表，不包含完整内容；内容只有在调用 `skill` 后才以 `tool/result` 出现；provider 变更时列表会重发，未变时不会添加事件；后注册层会覆写同名项目，撤销后恢复下层版本；未知名称会返回一般错误结果；空列表不会送出任何内容。
- [`demo.py`](src/demo.py)：在线示例让实际模型读列表、自己开口载一份内容，最后用一个 skill 收尾，而那个 skill 的 provider 是在两个 turn 之间才注册上去的。

```bash
python sections/09-skills/src/test.py    # offline check, no key
```

在线示例需要根目录的 `requirements.txt` 和一把 key；没有设置 key 时会自动跳过：

```bash
pip install -r requirements.txt         # anthropic + python-dotenv
cp .env.example .env                    # then set ANTHROPIC_API_KEY
python sections/09-skills/src/demo.py
```

---

## 参考资料

- [`docs/subsystems/skills.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/subsystems/skills.md)： dsh 自己带你走一遍 skill registry、它的 provider，还有列表和内容分家这件事。
