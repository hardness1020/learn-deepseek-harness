<!-- source: README.md @ 55e829b -->

# 09 · Skills

[English](README.md) | [繁體中文](README.zh-TW.md) | 简体中文

> 说明文本太大，不能每个 step 都送；又太有用，不能干脆不放。所以 request 只带名字，全文等真的有东西要用时再取。

Section 08 的 request 带着稳定的 system 文本和一份会变的快照，但它带的每一个字，每个 step 都还是要送一次。指示文本塞不进这个预算：一套 harness 会慢慢积累各种专门工作的操作说明，而任何一个 turn 用得到的，几乎都只有其中一小块。

两个最直觉的放法都不对。全部写死进 system 文本，每一次 request 就得为所有指示付钱，用不用得到都一样。整包都不放，model 连听都没听过的东西，当然也用不上。

而且这组东西不是固定的。skill 文本的主人很多：内建的一批、一个工作区、一个 plugin。session 还在跑的时候，每一方都可能挂上、卸下，或者盖掉某个名字，而且谁都不准为了这件事去改别人的文本。

所以：为什么 skill 列表是当成 context 注入，内容却要靠一次 tool 调用才加载进来？

因为“有哪些东西”必须便宜、而且随时看得到，“东西说了什么”则是用到才付钱。要做到这件事，registry 必须：

1. 收的是 provider，不是 skill：每个 provider 用两个动作把名字换成指示文本， `list()` 给摘要，`get(name)` 给一份完整内容。
2. provider 要分层：后注册的会盖掉先注册的同名项，而且每一次注册都会返回它的撤销函数。
3. 列表当成 context 注入：名字和一行说明搭 runtime-context 快照的便车，只有列表变了才重发。
4. 内容用一个 `skill` tool 按需加载，所以那段文本是以一条普通的 `tool/result` 落地。
5. 碰到不认得的名字，就回一条正常的错误结果，绝不往外抛异常。
6. 列表是空的时候，什么都不送。

---

## Mechanism

一个新文件 `skills.py`，搬过来的文件一个都没动：

- **`SkillRegistry`**：一层一层的 provider，照注册顺序叠。`catalog()` 把每个 provider 的 `list()` 摘要合起来，看得到的名字每个一行，同名的话后面那层的那行赢。`get(name)` 反过来从最上层往回走，返回找到的第一份内容。`register()` 照 kernel 的做法返回撤销函数。
- **`MemorySkillProvider`**：最简单的 provider，就是一个 `name -> {"description", "body"}` 的 dict。任何对象只要有 `list()` 和 `get(name)` 就算 provider；`list()` 绝不会主动把内容端出来。
- **`skills_plugin`**：把这个分工接起来。一个 Section 08 的 context provider 把 `catalog_text()` 算进快照，一个 `skill` tool 负责加载内容，registry 本身则以 `skills` 这个名字提供出去。

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

这份列表不需要任何新的投递机制。它只是 Section 08 那个 registry 上多出来的一个 context provider，所以它什么时候会再进 log 一次，早就由快照去重决定好了： provider 一变就重发，列表安安静静的时候一毛钱都不花。

```python
ctx.effect(
    ctx.get("system_prompt").context(
        "skills", lambda ac: skills.catalog_text(), order=100
    ),
    "skill catalog",
)
```

内容走的是另一条路：Section 05 盖好的那条 tool pipeline。名字不认得的时候，tool 的实现里会抛出异常，pipeline 再把它变成一条正常的 `is_error` 结果，所以对话记录的形状不会被弄坏：

```text
registered, layered              every step (the section 08 plane)

built-in   greet, haiku ─┐ catalog() ─► skills, load with the      ─► same as the last
workspace  greet         ┘             skill tool before use:         snapshot row?
  (shadows the built-in)               - greet: <workspace's line>    ├─ yes: nothing
                                       - haiku: answer as a haiku     └─ no: user/message

on demand, mid-turn (the model asks)

tool/call    skill {"name": "haiku"}
               │  get("haiku"): reverse layer walk, first body wins
tool/result  the full instruction text, an ordinary row
```

下面是一次真的运行，照 log 记下来的样子。列表上有两个 skill 的名字；model 加载了其中一份内容、照着做，第二个 step 则发现列表没变：

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

seq 7 那份内容，现在是推导历史的一部分，一条普通的 `tool` 消息：这个 session 后面每一次 request 都要为它付钱，但那是因为 model 自己开口要的。`greet` 的内容从头到尾没人要过，所以一个 token 都没花。

### 改了什么

跟 Section 08 比起来：

- 搬过来的文件全都原封不动：`agent_loop.py`、`inbox.py`、`kernel.py`、 `message.py`、`scheduler.py`、`session_log.py`、`standin.py`、 `system_prompt.py`、`tools.py`。`skills.py` 是唯一的新源代码文件，所以跟 08 的 diff 刚好就是这个 Section 的 Mechanism，没有别的。
- loop 完全没改，因为这个 Mechanism 纯粹是 plugin：列表从 Section 08 的 context provider 进来，内容从 Section 05 的 tool 进来。这是第一个 Section，它的 Mechanism 不用动到任何搬过来的文件，就放得进去。
- log 没有多出新的事件类型。快照那一条现在可能夹着列表那一段，`tool/result` 那一条可能夹着一份 skill 内容；推导历史的时候，两者就是普通的记录，照普通的方式处理。
- `demo.py`：Live demo 给真的 model 一份列表，让它自己开口加载一份内容，再趁两个 turn 之间注册第二个 provider，所以重发这件事会发生在一次真的 model 调用上。

---

## In real dsh

所有指过去的链接都固定在 Studied version [`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca) 上。registry 住在 skill 这个包家族里： [`packages/skill`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/skill)。

| Mini-dsh | 真正的 dsh | 说明 |
| --- | --- | --- |
| `skills.py` 里的 `SkillRegistry` | [`packages/skill/skill/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/skill/skill/src/index.ts)：`SkillRegistry` | 真正的 registry 继承 `Service`，挂在 `ctx.skills` 底下，跟 mini 一样是个复数形的 seam。它的层知道 scope（`SkillLayer implements ScopeLayer`）；mini 就只照注册顺序叠。 |
| provider 的 duck type（`list()` / `get(name)`） | [`index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/skill/skill/src/index.ts)：`SkillProvider` | 一个把名字换成指示文本的接口（第 248 行），不是 Service。注册时收的是一个工厂函数，它会拿到一个 `SkillProviderControl`（第 391 行），也就是 mini 那个撤销函数在真实世界里的样子。 |
| `MemorySkillProvider` | [`packages/skill/skill-filesystem/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/skill/skill-filesystem/src/index.ts)：`FileSystemSkillProvider` | 出货的那个 provider 是去磁盘上解 skill 目录的（第 146 行）；mini 用 dict 撑起来的 provider，让 Offline check 完全不碰文件系统。 |
| 列表的 context provider | [`packages/skill/tool-skill/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/skill/tool-skill/src/index.ts) | 真正的使用端是从 `agent/pre-step` 的 listener 把列表发出去的（第 177、213 行），也就是 Section 08 指过的那条 pre-step 通道。mini 没有 pre-step hook，所以它的列表改搭快照那条 context 通道。 |
| `skill` 这个 tool | [`tool-skill/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/skill/tool-skill/src/index.ts) | 内容一样是按需通过 tool 加载的（第 82 行）：列表和内容一样分成两边，也是靠同样那两条通道送出去。 |
| 拿快照去重当变更信号 | [`index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/skill/skill/src/index.ts)：`skills/change` | 真正的 registry 会用一个 bus 事件公告 provider 有变（第 297 行），使用端收到就把缓存作废；mini 则是每次组装都重算一次，安静的那些 step 就交给快照去重吸收掉。 |

真正的 skills 这一层，在这个 Section 的 Mechanism 之上，还多做了这些：

- **层知道 scope。**`SkillLayer implements ScopeLayer`，用的跟 tool registry 是同一套机制，所以 subagent 的 scope 可以看到跟父层不一样的列表。mini 的层是全局的；它那条覆盖规则是同一个想法，只是少了一个维度。
- **provider 手上有一个可以控制的 handle。**注册收的是一个工厂函数，它会拿到一个 `SkillProviderControl`，所以 provider 可以主动推变更通知，`skills/change` 事件再把通知扩散给有做缓存的使用端。mini 每次组装都重算一次列表，根本没有缓存需要作废。
- **有一个文件系统的 provider。**`FileSystemSkillProvider` 会走过 skill 目录，只读摘要、不加载内容，所以省 token 这件事，在 I/O 这一层也一样守得住。
- **pre-step 那条投递通道。**真正的列表，是由 `agent/pre-step` 的 listener 追加成 `user/message` 的，`packages/context` 底下大部分东西走的都是这一条。mini 是通过 Section 08 的 context registry，走到同样那几条 log 记录。

---

## Failure modes

- **内容直接放进列表，等于永远为全部付钱。**把每一份指示都内嵌进去，每一次 request 就要扛着全部，可是一个 turn 最多用到一份。`list()` 只给名字和一行说明；`get(name)` 是内容唯一的出口。
- **列表塞进 system 文本，前缀就被推走了。**Section 08 承诺的是一个字节都不差的 system 文本；session 中途挂上一个 provider 就会把它改掉，prompt 前缀缓存跟着报销。改成走 context，列表变一次只花一条 `user/message`，前缀稳稳不动。
- **不认得的名字直接往外抛异常，会把对话记录撕破。**model 迟早会把某个 skill 的名字拼错。`skill` 这个 tool 的实现抛出异常，Section 05 的 pipeline 用一条正常的 `is_error` 结果回应，turn 就继续跑下去，而不是把 loop 弄垮。
- **照时间先后分层，列表就会乱跳。**如果解出来的结果取决于 dict 顺序或线程的快慢，同样的注册就会算出不一样的列表，而每不一样一次，就白白多发一条快照。分层照的是注册顺序，后面的赢：同一组 provider 永远算出同样的文本。
- **列表一做缓存，就会跟 provider 对不上。**把算好的那一段缓存起来，某个注册已经被撤销的 provider 还会继续宣传一批根本解不出来的 skill。mini 每次组装都重算一次；让安静的 step 不花钱的是快照去重，不是缓存。

---

## 跑跑看

[`src/`](src/) 把 08 搬过来，然后加上：

- [`skills.py`](src/skills.py)（新的）：`SkillRegistry`，provider 分层、同名互相覆盖的解法；`MemorySkillProvider`；还有那个 plugin，把列表的 context、 `skill` tool 和 `skills` 这个 service 接起来。
- [`test.py`](src/test.py)：Offline check 证明列表是搭快照那一条进来的，里面一份内容都没有；内容只有在一次 `skill` 调用之后，才以 `tool/result` 的身份出现；provider 变了列表就重发，没变就安安静静；后面那层会盖住一个名字，直到它的撤销函数被调用，下面那层才露出来；不认得的名字就是一条正常的错误结果；列表空的时候什么都不送。
- [`demo.py`](src/demo.py)：Live demo 让真的 model 读列表、自己开口加载一份内容，最后用一个 skill 收尾，而那个 skill 的 provider 是在两个 turn 之间才注册上去的。

```bash
python sections/09-skills/src/test.py    # offline check, no key
```

Live demo 需要根目录的 `requirements.txt` 和一把 key；没有 key 的话，它会安静地跳过：

```bash
pip install -r requirements.txt         # anthropic + python-dotenv
cp .env.example .env                    # then set ANTHROPIC_API_KEY
python sections/09-skills/src/demo.py
```

---

## 出处

- [`docs/subsystems/skills.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/subsystems/skills.md)： dsh 自己带你走一遍 skill registry、它的 provider，还有列表和内容分家这件事。
