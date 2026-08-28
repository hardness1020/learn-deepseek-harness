<!-- source: README.md @ 3705bd7 -->

# 13 · Composition

[English](README.md) | [繁體中文](README.zh-TW.md) | 简体中文

> 前十二章已经创建完整 harness，但实际挂载哪些 plugin，仍然由一个需要手动修改的 Python 函数决定。不同产品模式其实只是同一批 plugin 的不同组合，所以组合方式应该表示成数据，而不是写死在代码中。

到目前为止，每章的测试都会手动组装 harness：挂载 session log、工具、loop，创建 agent，再接上拥有者专属的工具。虽然前十二章的机制都已经完成，但产品究竟激活哪些机制，还是必须直接修改 Python 函数。

这种做法不适合产品化。web 与 headless 模式只是将同一批 plugin 排成不同组合，用户也应该能只覆写其中一个 entry，不需 fork 整份设置。因此，harness 应以一份扁平 entry 列表描述，再由 bundle、profile 和用户设置依序叠加。

对 patch 来说，直觉选择是深层合并：共用键放在 base，每个 mode 只覆写需要改动的字段。真正的 dsh 不这么做。每个 patch 以 id 指定 entry，并替换完整 config，不做深层合并。

因此，本章要回答的问题是：为什么 patch 会替换完整 config，而不是深层合并？

深层合并会让 entry 的最终设置分散在多个层中。要理解某个键的来源，必须重放所有曾修改它的层，base 默认值也可能无意间流入不需要它的 mode。完整替换则让最后一个 patch 拥有该 entry 的全部设置。代价是，各 mode 必须重复列出完整 config，但设置来源因此更容易理解与调试。本章依下列规则实现：

1. 一份扁平 entry 列表就是产品的完整描述：每笔数据包含 `{id, name, config}`，不放任何 callable。
2. 列表从空集合开始，依序套用 patch 层：先 bundle，再 profile 与用户设置，后套用的层优先。
3. 三种 patch 动作都以 entry id 为键：新 id 代表插入，既有 id 会整份替换 config，`disabled` 则移除 entry。
4. 统一的名称对照表会将 entry name 解析成 plugin factory，让设置数据只在单一入口连接代码。
5. plugin 的挂载时机取决于所需 service 是否就绪，而不是 entry 在列表中的位置。
6. 加载器会持续挂载可启动项目，直到无法再前进；若仍有 entry 等待，就拒绝启动，并列出每个 entry 缺少的 service。

---

## 核心机制

只添加一个文件 `composition.py`，前面沿用的文件都没有修改：

- **`apply_layers(layers)`**：依序套用 patch 层，产生扁平 entry 列表；三种操作都以 id 为键。
- **`mount_entries(ctx, entries, plugins)`**：加载器。entry 所需 service 就绪后才挂载 plugin；若依赖始终无法满足，会明确拒绝启动。
- **`PLUGINS`**：名称对照表，将 entry 的 `name` 对应到 `config -> plugin` factory；每个 factory 都会声明挂载所需的 service。
- **`MINI_BASE`**：基础 bundle：第 00 章到 12 一路用手组出来的整套 harness，这次是十六个 entry 的数据。

套用逻辑很精简：先检查 id 是否已存在，再决定插入、替换或移除；完整替换只需要一行指派：

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

这行指派直接表达完整替换的规则。patch 不会读取旧 config，也不需要逐键合并；无论前面几层曾设置什么，最后套用的 patch 都拥有完整结果。

加载器是另外一半。entry 是数据，所以一个 entry 没办法说「把我排在 sessions 那个后面加载」；改成每个工厂点名自己要的 service，加载器就一轮一轮把当下挂得上去的都挂上去，直到列表不再变动：

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

因此，entry 顺序不代表加载顺序。即使将基础 bundle 反向排列，只要 service 依赖相同，最后仍会得到同一个产品。若某个 entry 需要的 service 始终未出现，加载器会拒绝启动并列出缺少项目，避免系统在功能不完整的状态下运作。

基础 bundle 也示范了完整替换。每种 mode 都需要 model，因此 base 先提供 stand-in；不同 profile 再以完整 config 替换它：

```python
{"id": "model", "name": "scripted-llm",
 "config": {"name": "scripted", "responses": []}},
{"id": "agent", "name": "agent",
 "config": {"agent": "a1", "session": "s1", "model": "scripted"}},
```

需要实际模型的 profile 不会只为 agent entry 合并 `{"model": "live"}`，而是提供完整 agent config，并另外插入 adapter entry。base 的旧字段不会意外保留，因此只看最后一层就能理解实际设置。

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

下面是组出来的 harness 在跑，log 就是这样记的。profile 把 model 那个 entry 的 config 换成一段会调用 shell tool 的脚本；这个 tool 的答案穿过另外两个 entry，也就是把 echo shell 包起来的 sandbox 围篱：

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

这段记录涉及的 loop、pipeline、sandbox 与 prompt 都由 entry 挂载。若停用其中一个 entry，例如 skills，相同 harness 运行相同脚本时就会由第 05 章的工具 pipeline 返回 `unknown tool 'skill'`。整个子系统可以通过数据移除，不需修改代码。

### 改了什么

与第 12 章相比：

- 所有既有文件都完整沿用：`agent_loop.py`、`capabilities.py`、`inbox.py`、`jobs.py`、`kernel.py`、`message.py`、`scheduler.py`、`session_log.py`、`skills.py`、`standin.py`、`subagent.py`、`system_prompt.py`、`tools.py`。`composition.py` 是唯一添加的源代码文件，因此与第 12 章相比，diff 只包含本章添加的机制，不包含其他改动。
- 既有机制都不需要为了能被组合而改过。这些 entry 挂的，就是前面每次检查手动挂上的同一批 plugin，走的也是第 01 章那道 `ctx.plugin()`；model 那个 entry 是通过第 10 章的 llm seam 接到 loop 的，adapter 用名字注册，每次调用才解一次。
- log 没有多出任何新的事件类型。组合这件事发生在第一个 turn 打开之前；组出来的产品，它的记录跟用手搭的那份分不出差别，而这正是重点。
- `demo.py`：在线示例在一层 live 的 profile 底下启动基础 bundle：插入一个 adapter entry、插入一个 worker entry、把 scripted 的 model entry 停掉，再把 agent 那个 entry 的 config 整份换掉，让它的 model 变成真的那个。
- 这是最后一个章节。第 00 章到 12 一片一片教出来的 harness，现在是空列表上的十六个 entry，而「一切都是 plugin，而且每一次注册都可以反向撤销」这句话，最后收在数据上：一个产品，就是对着空无一物做出来的一份 diff。

---

## 对照真正的 dsh

以下链接皆指向研究版本 [`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca)。这一层是启动平面：[`apps/cli`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/apps/cli)、[`packages/boot/app-boot`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/boot/app-boot)，以及 [`packages/bundle`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/bundle) 底下那些 bundle。

| Mini-dsh | 真正的 dsh | 说明 |
| --- | --- | --- |
| 在空列表上跑的 `apply_layers` | [`apps/cli/src/profile-boot.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/apps/cli/src/profile-boot.ts)（第 142 到 171 行） | 层的顺序是锁死的：先是照 `dsh.profile.bundles` 排的那些 bundle，接着是 profile 的 `cordis.patch.yml`，再来是 `$DSH_HOME/cordis.patch.yml`，最后是 `--patch` 叠上去的那几层。 |
| 三个动作，换就换整份 | [`packages/bundle/base/cordis.patch.yml`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/bundle/base/cordis.patch.yml)（第 6 到 10 行），由 [`vendor/include`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/vendor/include) 里的 `applyEntryPatches` 套用 | 一个 patch 用 id 指定一个 entry，然后把它整份 `config` 换掉，从不合并；再不然就是插入新的 entry。 |
| `MINI_BASE`，十六个 entry | [`packages/bundle/base/cordis.patch.yml`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/bundle/base/cordis.patch.yml) | `@deepseek-ai/dsh-base` 有 78 个 entry；headless 模式在 [`packages/bundle/headless/cordis.patch.yml`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/bundle/headless/cordis.patch.yml) 里再加 6 个。完整，但不小。 |
| `PLUGINS` 这张名字对照表 | [`packages/boot/app-boot/src/profile.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/boot/app-boot/src/profile.ts)：`resolveBundleDir`（第 344 行）、`PROFILE_TEMPLATES`（第 114 到 117 行） | 名字会解到磁盘上真正的软件包；出货的样板是 `web = [dsh-base, dsh-web-app]` 和 `headless = [dsh-base, dsh-headless]`。 |
| 在一个全新的 `Context()` 上跑 `mount_entries` | [`packages/boot/app-boot/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/boot/app-boot/src/index.ts)：`boot()`（第 757 行），entry 是通过 [`vendor/loader/src/config/entry.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/vendor/loader/src/config/entry.ts) 挂上去的 | `boot()` 就是先 `new Context()`，再 `ctx.plugin(Loader)`；每一个 entry 变成一次 plugin 挂载，每一次移除变成一次卸载，就是第 01 章那份约定放大到整个产品的规模。 |
| 持续挂载后再清查 | [`packages/boot/app-boot/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/boot/app-boot/src/index.ts)：`assertEntriesActivated`（第 700 到 725 行） | entry 顺序不代表加载顺序；是否启动取决于所需 service 是否就绪，清查则会列出所有仍在等待缺少 service 的 entry。 |

真正的 dsh 组合层还提供以下功能：

- **可动态更新的 entry 列表。** Loader 本身也是 plugin；修改 entry 时，运行中的 process 只会挂载或卸载对应差异，HMR 也沿用相同机制。Mini-dsh 不实现动态更新与 HMR。
- **profile 定义产品组合。** `dsh --profile web` 和 `dsh --profile headless`（[`apps/cli/src/args.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/apps/cli/src/args.ts)）会从 `PROFILE_TEMPLATES` 选择不同 bundle 组合；同一个运行档可因此形成两种产品。
- **每个 agent 各自的组合：preset。** 这不是 profile 那种层。[`@deepseek-ai/dsh-agent-presets`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/preset/agent-presets/src/mount.ts) 在每个 process 里把一棵 `agent.cordis.yml` 的子树挂一次，每个 session 再把自己的 agent 作用域接到它底下来加入；profile 的组合是整个 process 共用一份，preset 则是每个 agent 一份。
- **YAML entry 与完整模块解析。** entry 保存在用户可修改、可比较 diff 的 `cordis.patch.yml`，名称再通过 `resolveBundleDir` 解析成 npm 软件包。Mini-dsh 的 `PLUGINS` 字典扮演相同角色，但省略文件系统解析。

---

## 常见失败模式

- **深层合并会让最终 config 分散在多层。** 要追查一个字段，就必须重放所有修改过它的 patch。完整替换把答案集中在最后一个 entry，来源更清楚。
- **共用默认值可能渗入不需要它的 mode。** base 的 `{"name": "scripted", "responses": []}` 若与 profile 深层合并，旧字段可能意外保留。完整替换可确保未明确列出的键不会沿用。
- **将列表位置视为加载顺序，会让 patch 改变启动时序。** 新 entry 通常加入列表尾端，若位置具有语意，就可能重排其他项目。改由 service 依赖决定挂载后，patch 可以插入任意位置。
- **未清查等待中的 entry，可能启动出功能不完整的产品。** 某个 entry 若一直缺少 service，harness 可能表面启动成功，实际上 agent 根本不存在。加载完成后必须检查剩余 entry，并明确指出缺少的依赖。
- **config 若包含 callable，就无法作为可 patch 的数据。** callable 不适合写入文件、比较 diff 或由后续层替换。实际模型应通过名称与 adapter entry 解析，config 本身只保存纯数据。

---

## 动手验证

[`src/`](src/) 延续第 12 章，并加入：

- [`composition.py`](src/composition.py)（添加）：patch 的套用器、由 service 到齐与否决定挂载并带着先跑到停再清查的加载器、`PLUGINS` 名字对照表，还有十六个 entry 的 `MINI_BASE` bundle。
- [`test.py`](src/test.py)：离线测试证明几件事：三个动作能在空列表上一层层叠起来；一次换掉会整份收下 patch 的 config，base 一点都不会漏过来；那十六个 entry 能把 harness 启动起来，而且一个 turn 会穿过组出来的 sandbox 和 shell 两个 entry；base 整份倒着写，启动结果一模一样；一个 disable 的 entry 就能把 skills 这个子系统拿掉；少一个 service 的启动会被拒绝，而且每个还在等的 entry 都被点名。
- [`demo.py`](src/demo.py)：在线示例在基础 bundle 上叠一层 live 的 profile，把组出来的 entry 印出来，再证明它们是活的：一个穿过 sandbox 那几个 entry 的 shell turn，加上一次前景委派给 worker entry 里那个 Provider。

```bash
python sections/13-composition/src/test.py    # offline check, no key
```

在线示例需要根目录的 `requirements.txt` 和一把 key；没有设置 key 时会自动跳过：

```bash
pip install -r requirements.txt         # anthropic + python-dotenv
cp .env.example .env                    # then set ANTHROPIC_API_KEY
python sections/13-composition/src/demo.py
```

---

## 参考资料

- [`docs/architecture.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/architecture.md)：讲 Profiles 和 bundle 的那一节：entry 列表、patch 层，还有出货的那几叠 bundle。
- [`apps/cli/src/profile-boot.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/apps/cli/src/profile-boot.ts)：层是怎么叠起来的：bundle、profile 的 patch、home 的 patch，最后是 `--patch` 叠上去的，就这个顺序。
