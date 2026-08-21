<!-- source: README.md @ 55e829b -->

# 13 · Composition

[English](README.md) | [繁體中文](README.zh-TW.md) | 简体中文

> 走完十二个 Section，产品实际挂上的东西，仍然是一个得由人手动改的 Python 函数。两种 build 只是同一批 plugin 放进不同列表，所以 harness 不再是代码，改成一份列表。

到目前为止，每个 Section 的结尾都长一样：由检查自己动手把 harness 组起来。挂上 session log、挂上 tool、挂上 loop、建出 agent、接好拥有者的 tool；十二个 Section 的 mechanism 摊在那里，而决定一个产品要挂哪几个的，还是得靠人手动去改 Python function。

产品不能这样出货。web 版和 headless 版是同一批 plugin 排成不同的列表；用户想换掉别人那份组合里的某一个 entry，又不想整份 fork 走。harness 的描述必须变成数据：一份扁平的 entry 列表，由好几层叠出来，每一层归负责发言的那一方所有，基础的厂商排最前面，用户排最后面。

讲到 patch 这个动作，第一个直觉是深层合并：共同的键放在 base，每个 mode 只 patch 自己要改的那几个键。真正的 dsh 不接受。一个 patch 用 id 指定一个 entry，然后把那个 entry 的整份 config 换掉，从不合并。

所以：为什么一个 patch 是整份 config 的替换，而不是深层合并？

因为一旦合并，一个 entry 最后到底是什么配置，就得靠推的：想知道它的意思，你得把每一层碰过它的都重放一遍，而且 base 的默认值会漏进一个根本没要它的 mode 里。换掉则让每个 entry 的真相只留在一个地方：最后碰过它的那一层，手上就是完整的故事。这个代价是刻意丢给写 bundle 的人的：一个值会因 mode 而不同的 entry，根本不能待在 base 里，每个 mode 都得把那个 entry 的完整 config 重讲一次。这个 Section 这样把它做出来：

1. 一份扁平的 entry 列表，就是这个产品的全部描述：照顺序排的 `{id, name, config}`，纯数据，不放任何 callable。
2. 这份列表是在一份空列表上，照顺序叠 patch 层叠出来的：bundle 排最前面，profile 和用户那几层排后面，后面的赢。
3. 三个 patch 动作，都用 entry 的 id 当键：没见过的 id 就插入，已经有的 id 就把整份 config 换掉，`disabled` 就移除。
4. 一张名字对照表，把一个 entry 的 name 换成 plugin 工厂，让数据只在一个地方找到代码。
5. 挂载的时机由 service 到齐了没决定，绝不由 entry 排在第几个决定。
6. 先跑到停、再清查一遍：等到再也挂不上任何东西、却还有 entry 剩着，就拒绝启动，并且把每个剩下的 entry 和它在等什么都讲出来。

---

## Mechanism

只新增一个文件 `composition.py`，前面搬过来的文件一个都没动：

- **`apply_layers(layers)`**：照顺序排好的 patch 层进去，一份扁平的 entry 列表出来。三个动作，全都用 id 当键。
- **`mount_entries(ctx, entries, plugins)`**：加载器。等一个 entry 的工厂点名的那些 service 都在了，就把它的 plugin 挂上去；碰到永远活不起来的 entry，就指名道姓地拒绝。
- **`PLUGINS`**：那张名字对照表，从一个 entry 的 `name` 对到一个 `config -> plugin` 的工厂，每个工厂都声明自己挂上去的时候需要哪些 service。
- **`MINI_BASE`**：基础 bundle：Section 00 到 12 一路用手组出来的整套 harness，这次是十六个 entry 的数据。

套用的那段很小，因为要做哪个动作，看那个 id 现在代表什么就决定了，而换掉这件事，一行指派就写完了：

```python
row = next((r for r in entries if r["id"] == patch["id"]), None)
if patch.get("disabled"):
    if row is not None:
        entries.remove(row)
elif row is None:
    entries.append({"id": patch["id"], "name": patch["name"],
                    "config": dict(patch.get("config", {}))})
else:
    row["config"] = dict(patch.get("config", {}))  # whole, never a merge
```

那一行指派，就是用代码回答这个设计问题。没有逐键走访要写，因为一个 patch 根本不会碰到旧的 config；不管前面几层对这个 entry 讲过什么，最后碰它的那个 patch 说了算。

加载器是另外一半。entry 是数据，所以一个 entry 没办法说“把我排在 sessions 那个后面加载”；改成每个工厂点名自己要的 service，加载器就一轮一轮把当下挂得上去的都挂上去，直到列表不再变动：

```python
while pending:
    ready = [row for row in pending if not _missing(ctx, table, row)]
    if not ready:
        waits = "; ".join(
            f"'{row['id']}' waits on {', '.join(_missing(ctx, table, row))}"
            for row in pending
        )
        raise RuntimeError(f"rows never activated: {waits}")
    for row in ready:
        fibers[row["id"]] = ctx.plugin(table[row["name"]](row.get("config", {})))
        pending.remove(row)
```

所以 entry 的先后顺序不带任何加载语义。基础 bundle 整份倒着写，启动起来还是同一个产品，因为“什么时候挂”是由 ctx 上有哪些 service 回答的，也就是 Section 01 那层底座，不是由它在文件里排第几个回答的。而只要一个 entry 等的 service 永远不会来，清查就会拒绝整次启动，并且把它在等什么讲出来，所以半残的产品没办法安安静静地跑起来。

基础 bundle 把这个设计问题变成日常。model 这个 entry 放在 base 里，因为每种 mode 都有一个 model，但它的值会因 mode 而不同，所以 base 放的是一个没话可说的 stand-in，每个 profile 再把整份 config 重讲一次：

```python
{"id": "model", "name": "scripted-llm",
 "config": {"name": "scripted", "responses": []}},
{"id": "agent", "name": "agent",
 "config": {"agent": "a1", "session": "s1", "model": "scripted"}},
```

一个想要真 model 的 profile，不会往 agent 那个 entry 里 patch 一句 `{"model": "live"}`；它会把 agent 那个 entry 的完整 config 重讲一次，再在旁边插进自己的 adapter entry。没有任何东西会从 base 漏上来，所以读 profile 那一层，读到的就是真相。

```text
composition, top to bottom

MINI_BASE          sixteen rows over the empty list
profile layer      inserts its rows, replaces whole configs,
                   disables what the mode does without
user layer         the same three verbs, last word wins
  │
  ▼  apply_layers
one flat entry list: {id, name, config} rows, data only
  │
  ▼  mount_entries, via PLUGINS
plugins mounted as services become available; the audit
names any row still waiting when the list can settle no further
```

下面是组出来的 harness 在跑，log 就是这样记的。profile 把 model 那个 entry 的 config 换成一段会调用 shell tool 的脚本；这个 tool 的答案穿过另外两个 entry，也就是把 echo shell 包起来的 sandbox 围栏：

```text
send("run a command")                       the composed product
  │   0  turn/start
  │   2  user/message   "run a command"
  │   5  tool/call      shell {"command": "echo composed from rows"}
  │   6  tool/result    "mini-sandbox --policy echo-only --
  │                      echo composed from rows"
  │  13  assistant/message "the rows are alive"
  │  15  turn/end
```

那段记录里的每一个 mechanism，loop、pipeline、sandbox、prompt，全都是以一个 entry 的身份到场的。把其中一个 entry 停掉，比如 skills 那个，同一套 harness 面对同一段脚本，就会从 Section 05 那道门回一句 `unknown tool 'skill'`：一个子系统被数据拿掉了，却没有任何一行代码被改过。

### 改了什么

跟 Section 12 比起来：

- 每一个搬过来的文件都原封不动：`agent_loop.py`、`capabilities.py`、`inbox.py`、`jobs.py`、`kernel.py`、`message.py`、`scheduler.py`、`session_log.py`、`skills.py`、`standin.py`、`subagent.py`、`system_prompt.py`、`tools.py`。`composition.py` 是唯一新增的源代码文件，所以拿 12 来 diff，跑出来的就是这个 Section 的 Mechanism，没有别的。
- 底下没有任何一个 mechanism 为了能被组合而改过。这些 entry 挂的，就是前面每次检查手动挂上的同一批 plugin，走的也是 Section 01 那道 `ctx.plugin()`；model 那个 entry 是通过 Section 10 的 llm seam 接到 loop 的，adapter 用名字注册，每次调用才解一次。
- log 没有多出任何新的事件类型。组合这件事发生在第一个 turn 打开之前；组出来的产品，它的记录跟用手搭的那份分不出差别，而这正是重点。
- `demo.py`：Live demo 在一层 live 的 profile 底下启动基础 bundle：插入一个 adapter entry、插入一个 worker entry、把 scripted 的 model entry 停掉，再把 agent 那个 entry 的 config 整份换掉，让它的 model 变成真的那个。
- 这是最后一个 Section。Section 00 到 12 一片一片教出来的 harness，现在是空列表上的十六个 entry，而“一切都是 plugin，而且每一次注册都可以反向撤销”这句话，最后收在数据上：一个产品，就是对着空无一物做出来的一份 diff。

---

## In real dsh

所有指过去的链接都固定在 Studied version [`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca) 上。这一层是启动平面：[`apps/cli`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/apps/cli)、[`packages/boot/app-boot`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/boot/app-boot)，以及 [`packages/bundle`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/bundle) 底下那些 bundle。

| Mini-dsh | 真正的 dsh | 说明 |
| --- | --- | --- |
| 在空列表上跑的 `apply_layers` | [`apps/cli/src/profile-boot.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/apps/cli/src/profile-boot.ts)（第 142 到 171 行） | 层的顺序是锁死的：先是照 `dsh.profile.bundles` 排的那些 bundle，接着是 profile 的 `cordis.patch.yml`，再来是 `$DSH_HOME/cordis.patch.yml`，最后是 `--patch` 叠上去的那几层。 |
| 三个动作，换就换整份 | [`packages/bundle/base/cordis.patch.yml`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/bundle/base/cordis.patch.yml)（第 6 到 10 行），由 [`vendor/include`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/vendor/include) 里的 `applyEntryPatches` 套用 | 一个 patch 用 id 指定一个 entry，然后把它整份 `config` 换掉，从不合并；再不然就是插入新的 entry。 |
| `MINI_BASE`，十六个 entry | [`packages/bundle/base/cordis.patch.yml`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/bundle/base/cordis.patch.yml) | `@deepseek-ai/dsh-base` 有 78 个 entry；headless 模式在 [`packages/bundle/headless/cordis.patch.yml`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/bundle/headless/cordis.patch.yml) 里再加 6 个。完整，但不小。 |
| `PLUGINS` 这张名字对照表 | [`packages/boot/app-boot/src/profile.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/boot/app-boot/src/profile.ts)：`resolveBundleDir`（第 344 行）、`PROFILE_TEMPLATES`（第 114 到 117 行） | 名字会解到磁盘上真正的包；出货的模板是 `web = [dsh-base, dsh-web-app]` 和 `headless = [dsh-base, dsh-headless]`。 |
| 在一个全新的 `Context()` 上跑 `mount_entries` | [`packages/boot/app-boot/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/boot/app-boot/src/index.ts)：`boot()`（第 757 行），entry 是通过 [`vendor/loader/src/config/entry.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/vendor/loader/src/config/entry.ts) 挂上去的 | `boot()` 就是先 `new Context()`，再 `ctx.plugin(Loader)`；每一个 entry 变成一次 plugin 挂载，每一次移除变成一次卸载，就是 Section 01 那份约定放大到整个产品的规模。 |
| 先跑到停、再清查的那一轮 | [`packages/boot/app-boot/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/boot/app-boot/src/index.ts)：`assertEntriesActivated`（第 700 到 725 行） | entry 的顺序不带任何加载语义；要不要活起来是由 service 到齐了没决定的，而清查会把还在等缺席 service 的 entry 一个个点名。 |

真正的组合这一层，在这个 Section 的 Mechanism 之上，还多做了这些：

- **一份活的 entry 列表。** Loader 自己就是一个 plugin，而那份列表一直是活的：改一个 entry，在跑着的 process 里就会刚好挂上或卸下那一点差异，HMR 也是搭同一套机器。HMR 在这次重建的 Ceiling 之上：只在这里指给你看，不重建。
- **profile 就是产品。** `dsh --profile web` 和 `dsh --profile headless`（[`apps/cli/src/args.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/apps/cli/src/args.ts)）会从 `PROFILE_TEMPLATES` 挑一叠 bundle：同一个可执行文件，两个产品，差别只在列表。
- **每个 agent 各自的组合：preset。** 这不是 profile 那种层。[`@deepseek-ai/dsh-agent-presets`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/preset/agent-presets/src/mount.ts) 在每个 process 里把一棵 `agent.cordis.yml` 的子树挂一次，每个 session 再把自己的 agent 作用域接到它底下来加入；profile 的组合是整个 process 共用一份，preset 则是每个 agent 一份。
- **YAML 写的 entry，加上一套真的模块系统。** entry 就住在 `cordis.patch.yml` 这种用户可以改、可以 diff 的文件里，名字则通过 `resolveBundleDir` 解到 npm 包；mini 那个 `PLUGINS` 字典，就是同一件解析的事，只是把文件系统拿掉了。

---

## Failure modes

- **深层合并会让每一层都变成嫌疑犯。** 一旦开始合并，一个 entry 真正生效的 config 哪里都不存在；它是每一层碰过它的结果叠起来的，要抓一个键错在哪，就得把整叠重放一次。换掉则把答案留在一层里的一个 entry 上：最后那个 patch 就是全部的真相。
- **抽出来共用的默认值，会漏进一个根本没要它的 mode。** 让 base 带着 `{"name": "scripted", "responses": []}`，再让一个 profile 合并进一个键，那这个 profile 的 model 就会默默留着 base 的剩菜。这次检查证明的是相反的形状：换掉之后，旧 config 一个键都不会活下来。
- **拿排列位置当加载顺序，一 patch 就坏。** 每一层插进来的 entry 都是加在列表最后面，所以要是位置代表加载顺序，一个 profile 加的 entry 就会把它后面所有东西的启动顺序重洗一次。由 service 到齐与否决定挂载，位置就没有意义了，也正因为这样，任何一层想插在哪都行。
- **一个默默卡住的 entry，会启动出半个产品。** 没有清查的话，一个在等没人提供的 service 的 entry，就只是一直挂不上去：harness 起来了，agent 不见了，却没有人讲一句话。先跑到停、再清查那一轮，把这个安静的洞变成一次讲清楚的拒绝，指名是哪个 entry，等的是哪个 service。
- **装着活对象的 config 没办法 patch。** config 里放一个 callable，它就没办法写进文件、没办法 diff，也没办法被后面某一层整份换掉。Live demo 的 model 是通过对照表里的一个名字和一个 adapter entry 进来的；config 只带名字，所以这个 entry 一直是 patch 管得动的数据。

---

## 跑跑看

[`src/`](src/) 把 12 搬过来，再加上：

- [`composition.py`](src/composition.py)（新增）：patch 的套用器、由 service 到齐与否决定挂载并带着先跑到停再清查的加载器、`PLUGINS` 名字对照表，还有十六个 entry 的 `MINI_BASE` bundle。
- [`test.py`](src/test.py)：Offline check 证明几件事：三个动作能在空列表上一层层叠起来；一次换掉会整份收下 patch 的 config，base 一点都不会漏过来；那十六个 entry 能把 harness 启动起来，而且一个 turn 会穿过组出来的 sandbox 和 shell 两个 entry；base 整份倒着写，启动结果一模一样；一个 disable 的 entry 就能把 skills 这个子系统拿掉；少一个 service 的启动会被拒绝，而且每个还在等的 entry 都被点名。
- [`demo.py`](src/demo.py)：Live demo 在基础 bundle 上叠一层 live 的 profile，把组出来的 entry 打印出来，再证明它们是活的：一个穿过 sandbox 那几个 entry 的 shell turn，加上一次前台委派给 worker entry 里那个 Provider。

```bash
python sections/13-composition/src/test.py    # offline check, no key
```

Live demo 需要根目录的 `requirements.txt` 和一把 key；没有 key 的话，它会安静地跳过：

```bash
pip install -r requirements.txt         # anthropic + python-dotenv
cp .env.example .env                    # then set ANTHROPIC_API_KEY
python sections/13-composition/src/demo.py
```

---

## 出处

- [`docs/architecture.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/architecture.md)：讲 Profiles 和 bundle 的那一节：entry 列表、patch 层，还有出货的那几叠 bundle。
- [`apps/cli/src/profile-boot.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/apps/cli/src/profile-boot.ts)：层是怎么叠起来的：bundle、profile 的 patch、home 的 patch，最后是 `--patch` 叠上去的，就这个顺序。
