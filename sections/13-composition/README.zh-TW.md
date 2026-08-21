<!-- source: README.md @ c6e4d6f -->

# 13 · Composition

[English](README.md) | 繁體中文 | [简体中文](README.zh-CN.md)

> 在一張空白的紙上，每一層輪流寫下自己的那幾個 entry。等最後一層安靜下來，留在紙上的就是這個應用程式：不是一段程式碼呼叫另一段程式碼，而是一份機器照著掛的清單。

到目前為止，每個 Section 的結尾都長一樣：一支用手把 harness 組起來的檢查。掛上 session log、掛上 tool、掛上 loop、建出 agent、接好擁有者的 tool；十二個 Section 的 mechanism 攤在那裡，而決定一個產品要掛哪幾個的，還是一支要人動手去改的 Python function。

產品不能這樣出貨。web 版和 headless 版是同一批 plugin 排成不同的清單；使用者想換掉別人那份組合裡的某一個 entry，又不想整份 fork 走。harness 的描述必須變成資料：一份扁平的 entry 清單，由好幾層疊出來，每一層歸負責發言的那一方所有，基礎的廠商排最前面，使用者排最後面。

講到 patch 這個動作，第一個直覺是深層合併：共同的鍵放在 base，每個 mode 只 patch 自己要改的那幾個鍵。真正的 dsh 不接受。一個 patch 用 id 指定一個 entry，然後把那個 entry 的整份 config 換掉，從不合併。

所以：為什麼一個 patch 是整份 config 的替換，而不是深層合併？

因為一旦合併，一個 entry 最後到底是什麼設定，就得靠推的：想知道它的意思，你得把每一層碰過它的都重放一遍，而且 base 的預設值會漏進一個根本沒要它的 mode 裡。換掉則讓每個 entry 的真相只留在一個地方：最後碰過它的那一層，手上就是完整的故事。這個代價是刻意丟給寫 bundle 的人的：一個值會因 mode 而不同的 entry，根本不能待在 base 裡，每個 mode 都得把那個 entry 的完整 config 重講一次。這個 Section 這樣把它做出來：

1. 一份扁平的 entry 清單，就是這個產品的全部描述：照順序排的 `{id, name, config}`，純資料，不放任何 callable。
2. 這份清單是在一份空清單上，照順序疊 patch 層疊出來的：bundle 排最前面，profile 和使用者那幾層排後面，後面的贏。
3. 三個 patch 動作，都用 entry 的 id 當鍵：沒見過的 id 就插入，已經有的 id 就把整份 config 換掉，`disabled` 就移除。
4. 一張名字對照表，把一個 entry 的 name 換成 plugin 工廠，讓資料只在一個地方找到程式碼。
5. 掛載的時機由 service 到齊了沒決定，絕不由 entry 排在第幾個決定。
6. 先跑到停、再清查一遍：等到再也掛不上任何東西、卻還有 entry 剩著，就拒絕啟動，並且把每個剩下的 entry 和它在等什麼都講出來。

---

## Mechanism

只新增一個檔案 `composition.py`，前面搬過來的檔案一個都沒動：

- **`apply_layers(layers)`**：照順序排好的 patch 層進去，一份扁平的 entry 清單出來。三個動作，全都用 id 當鍵。
- **`mount_entries(ctx, entries, plugins)`**：載入器。等一個 entry 的工廠點名的那些 service 都在了，就把它的 plugin 掛上去；碰到永遠活不起來的 entry，就指名道姓地拒絕。
- **`PLUGINS`**：那張名字對照表，從一個 entry 的 `name` 對到一個 `config -> plugin` 的工廠，每個工廠都宣告自己掛上去的時候需要哪些 service。
- **`MINI_BASE`**：基礎 bundle：Section 00 到 12 一路用手組出來的整套 harness，這次是十六個 entry 的資料。

套用的那段很小，因為要做哪個動作，看那個 id 現在代表什麼就決定了，而換掉這件事，一行指派就寫完了：

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

那一行指派，就是用程式碼回答這個設計問題。沒有逐鍵走訪要寫，因為一個 patch 根本不會碰到舊的 config；不管前面幾層對這個 entry 講過什麼，最後碰它的那個 patch 說了算。

載入器是另外一半。entry 是資料，所以一個 entry 沒辦法說「把我排在 sessions 那個後面載入」；改成每個工廠點名自己要的 service，載入器就一輪一輪把當下掛得上去的都掛上去，直到清單不再變動：

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

所以 entry 的先後順序不帶任何載入語意。基礎 bundle 整份倒著寫，啟動起來還是同一個產品，因為「什麼時候掛」是由 ctx 上有哪些 service 回答的，也就是 Section 01 那層底座，不是由它在檔案裡排第幾個回答的。而只要一個 entry 等的 service 永遠不會來，清查就會拒絕整次啟動，並且把它在等什麼講出來，所以半殘的產品沒辦法安安靜靜地跑起來。

基礎 bundle 把這個設計問題變成日常。model 這個 entry 放在 base 裡，因為每種 mode 都有一個 model，但它的值會因 mode 而不同，所以 base 放的是一個沒話可說的 stand-in，每個 profile 再把整份 config 重講一次：

```python
{"id": "model", "name": "scripted-llm",
 "config": {"name": "scripted", "responses": []}},
{"id": "agent", "name": "agent",
 "config": {"agent": "a1", "session": "s1", "model": "scripted"}},
```

一個想要真 model 的 profile，不會往 agent 那個 entry 裡 patch 一句 `{"model": "live"}`；它會把 agent 那個 entry 的完整 config 重講一次，再在旁邊插進自己的 adapter entry。沒有任何東西會從 base 漏上來，所以讀 profile 那一層，讀到的就是真相。

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

下面是組出來的 harness 在跑，log 就是這樣記的。profile 把 model 那個 entry 的 config 換成一段會呼叫 shell tool 的腳本；這個 tool 的答案穿過另外兩個 entry，也就是把 echo shell 包起來的 sandbox 圍籬：

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

那段紀錄裡的每一個 mechanism，loop、pipeline、sandbox、prompt，全都是以一個 entry 的身分到場的。把其中一個 entry 停掉，比如 skills 那個，同一套 harness 面對同一段腳本，就會從 Section 05 那道門回一句 `unknown tool 'skill'`：一個子系統被資料拿掉了，卻沒有任何一行程式碼被改過。

### 改了什麼

跟 Section 12 比起來：

- 每一個搬過來的檔案都原封不動：`agent_loop.py`、`capabilities.py`、`inbox.py`、`jobs.py`、`kernel.py`、`message.py`、`scheduler.py`、`session_log.py`、`skills.py`、`standin.py`、`subagent.py`、`system_prompt.py`、`tools.py`。`composition.py` 是唯一新增的原始碼檔案，所以拿 12 來 diff，跑出來的就是這個 Section 的 Mechanism，沒有別的。
- 底下沒有任何一個 mechanism 為了能被組合而改過。這些 entry 掛的，就是前面每一支檢查用手掛的同一批 plugin，走的也是 Section 01 那道 `ctx.plugin()`；model 那個 entry 是透過 Section 10 的 llm seam 接到 loop 的，adapter 用名字註冊，每次呼叫才解一次。
- log 沒有多出任何新的事件型別。組合這件事發生在第一個 turn 打開之前；組出來的產品，它的紀錄跟用手搭的那份分不出差別，而這正是重點。
- `demo.py`：Live demo 在一層 live 的 profile 底下啟動基礎 bundle：插入一個 adapter entry、插入一個 worker entry、把 scripted 的 model entry 停掉，再把 agent 那個 entry 的 config 整份換掉，讓它的 model 變成真的那個。
- 這是最後一個 Section。Section 00 到 12 一片一片教出來的 harness，現在是空清單上的十六個 entry，而「一切都是 plugin，而且每一次註冊都可以反向撤銷」這句話，最後收在資料上：一個產品，就是對著空無一物做出來的一份 diff。

---

## In real dsh

所有指過去的連結都固定在 Studied version [`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca) 上。這一層是啟動平面：[`apps/cli`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/apps/cli)、[`packages/boot/app-boot`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/boot/app-boot)，以及 [`packages/bundle`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/bundle) 底下那些 bundle。

| Mini-dsh | 真正的 dsh | 說明 |
| --- | --- | --- |
| 在空清單上跑的 `apply_layers` | [`apps/cli/src/profile-boot.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/apps/cli/src/profile-boot.ts)（第 142 到 171 行） | 層的順序是鎖死的：先是照 `dsh.profile.bundles` 排的那些 bundle，接著是 profile 的 `cordis.patch.yml`，再來是 `$DSH_HOME/cordis.patch.yml`，最後是 `--patch` 疊上去的那幾層。 |
| 三個動作，換就換整份 | [`packages/bundle/base/cordis.patch.yml`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/bundle/base/cordis.patch.yml)（第 6 到 10 行），由 [`vendor/include`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/vendor/include) 裡的 `applyEntryPatches` 套用 | 一個 patch 用 id 指定一個 entry，然後把它整份 `config` 換掉，從不合併；再不然就是插入新的 entry。 |
| `MINI_BASE`，十六個 entry | [`packages/bundle/base/cordis.patch.yml`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/bundle/base/cordis.patch.yml) | `@deepseek-ai/dsh-base` 有 78 個 entry；headless 模式在 [`packages/bundle/headless/cordis.patch.yml`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/bundle/headless/cordis.patch.yml) 裡再加 6 個。完整，但不小。 |
| `PLUGINS` 這張名字對照表 | [`packages/boot/app-boot/src/profile.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/boot/app-boot/src/profile.ts)：`resolveBundleDir`（第 344 行）、`PROFILE_TEMPLATES`（第 114 到 117 行） | 名字會解到磁碟上真正的套件；出貨的樣板是 `web = [dsh-base, dsh-web-app]` 和 `headless = [dsh-base, dsh-headless]`。 |
| 在一個全新的 `Context()` 上跑 `mount_entries` | [`packages/boot/app-boot/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/boot/app-boot/src/index.ts)：`boot()`（第 757 行），entry 是透過 [`vendor/loader/src/config/entry.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/vendor/loader/src/config/entry.ts) 掛上去的 | `boot()` 就是先 `new Context()`，再 `ctx.plugin(Loader)`；每一個 entry 變成一次 plugin 掛載，每一次移除變成一次卸載，就是 Section 01 那份約定放大到整個產品的規模。 |
| 先跑到停、再清查的那一輪 | [`packages/boot/app-boot/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/boot/app-boot/src/index.ts)：`assertEntriesActivated`（第 700 到 725 行） | entry 的順序不帶任何載入語意；要不要活起來是由 service 到齊了沒決定的，而清查會把還在等缺席 service 的 entry 一個個點名。 |

真正的組合這一層，在這個 Section 的 Mechanism 之上，還多做了這些：

- **一份活的 entry 清單。** Loader 自己就是一個 plugin，而那份清單一直是活的：改一個 entry，在跑著的 process 裡就會剛好掛上或卸下那一點差異，HMR 也是搭同一套機器。HMR 在這次重建的 Ceiling 之上：只在這裡指給你看，不重建。
- **profile 就是產品。** `dsh --profile web` 和 `dsh --profile headless`（[`apps/cli/src/args.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/apps/cli/src/args.ts)）會從 `PROFILE_TEMPLATES` 挑一疊 bundle：同一支執行檔，兩個產品，差別只在清單。
- **每個 agent 各自的組合：preset。** 這不是 profile 那種層。[`@deepseek-ai/dsh-agent-presets`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/preset/agent-presets/src/mount.ts) 在每個 process 裡把一棵 `agent.cordis.yml` 的子樹掛一次，每個 session 再把自己的 agent 作用域接到它底下來加入；profile 的組合是整個 process 共用一份，preset 則是每個 agent 一份。
- **YAML 寫的 entry，加上一套真的模組系統。** entry 就住在 `cordis.patch.yml` 這種使用者可以改、可以 diff 的檔案裡，名字則透過 `resolveBundleDir` 解到 npm 套件；mini 那個 `PLUGINS` 字典，就是同一件解析的事，只是把檔案系統拿掉了。

---

## Failure modes

- **深層合併會讓每一層都變成嫌疑犯。** 一旦開始合併，一個 entry 真正生效的 config 哪裡都不存在；它是每一層碰過它的結果疊起來的，要抓一個鍵錯在哪，就得把整疊重放一次。換掉則把答案留在一層裡的一個 entry 上：最後那個 patch 就是全部的真相。
- **抽出來共用的預設值，會漏進一個根本沒要它的 mode。** 讓 base 帶著 `{"name": "scripted", "responses": []}`，再讓一個 profile 合併進一個鍵，那這個 profile 的 model 就會默默留著 base 的剩菜。這支檢查證明的是相反的形狀：換掉之後，舊 config 一個鍵都不會活下來。
- **拿排列位置當載入順序，一 patch 就壞。** 每一層插進來的 entry 都是加在清單最後面，所以要是位置代表載入順序，一個 profile 加的 entry 就會把它後面所有東西的啟動順序重洗一次。由 service 到齊與否決定掛載，位置就沒有意義了，也正因為這樣，任何一層想插在哪都行。
- **一個默默卡住的 entry，會啟動出半個產品。** 沒有清查的話，一個在等沒人提供的 service 的 entry，就只是一直掛不上去：harness 起來了，agent 不見了，卻沒有人講一句話。先跑到停、再清查那一輪，把這個安靜的洞變成一次講清楚的拒絕，指名是哪個 entry，等的是哪個 service。
- **裝著活物件的 config 沒辦法 patch。** config 裡放一個 callable，它就沒辦法寫進檔案、沒辦法 diff，也沒辦法被後面某一層整份換掉。Live demo 的 model 是透過對照表裡的一個名字和一個 adapter entry 進來的；config 只帶名字，所以這個 entry 一直是 patch 管得動的資料。

---

## 跑跑看

[`src/`](src/) 把 12 搬過來，再加上：

- [`composition.py`](src/composition.py)（新增）：patch 的套用器、由 service 到齊與否決定掛載並帶著先跑到停再清查的載入器、`PLUGINS` 名字對照表，還有十六個 entry 的 `MINI_BASE` bundle。
- [`test.py`](src/test.py)：Offline check 證明幾件事：三個動作能在空清單上一層層疊起來；一次換掉會整份收下 patch 的 config，base 一點都不會漏過來；那十六個 entry 能把 harness 啟動起來，而且一個 turn 會穿過組出來的 sandbox 和 shell 兩個 entry；base 整份倒著寫，啟動結果一模一樣；一個 disable 的 entry 就能把 skills 這個子系統拿掉；少一個 service 的啟動會被拒絕，而且每個還在等的 entry 都被點名。
- [`demo.py`](src/demo.py)：Live demo 在基礎 bundle 上疊一層 live 的 profile，把組出來的 entry 印出來，再證明它們是活的：一個穿過 sandbox 那幾個 entry 的 shell turn，加上一次前景委派給 worker entry 裡那個 Provider。

```bash
python sections/13-composition/src/test.py    # offline check, no key
```

Live demo 需要根目錄的 `requirements.txt` 和一把 key；沒有 key 的話，它會安靜地跳過：

```bash
pip install -r requirements.txt         # anthropic + python-dotenv
cp .env.example .env                    # then set ANTHROPIC_API_KEY
python sections/13-composition/src/demo.py
```

---

## 出處

- [`docs/architecture.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/architecture.md)：講 Profiles 和 bundle 的那一節：entry 清單、patch 層，還有出貨的那幾疊 bundle。
- [`apps/cli/src/profile-boot.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/apps/cli/src/profile-boot.ts)：層是怎麼疊起來的：bundle、profile 的 patch、home 的 patch，最後是 `--patch` 疊上去的，就這個順序。
