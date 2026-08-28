<!-- source: README.md @ 3705bd7 -->

# 13 · Composition

[English](README.md) | 繁體中文 | [简体中文](README.zh-CN.md)

> 前十二章已經建立完整 harness，但實際掛載哪些 plugin，仍然由一個需要手動修改的 Python 函式決定。不同產品模式其實只是同一批 plugin 的不同組合，所以組合方式應該表示成資料，而不是寫死在程式碼中。

到目前為止，每章的測試都會手動組裝 harness：掛載 session log、工具、loop，建立 agent，再接上擁有者專屬的工具。雖然前十二章的機制都已經完成，但產品究竟啟用哪些機制，還是必須直接修改 Python 函式。

這種做法不適合產品化。web 與 headless 模式只是將同一批 plugin 排成不同組合，使用者也應該能只覆寫其中一個 entry，不需 fork 整份設定。因此，harness 應以一份扁平 entry 清單描述，再由 bundle、profile 和使用者設定依序疊加。

對 patch 來說，直覺選擇是深層合併：共用鍵放在 base，每個 mode 只覆寫需要改動的欄位。真正的 dsh 不這麼做。每個 patch 以 id 指定 entry，並替換完整 config，不做深層合併。

因此，本章要回答的問題是：為什麼 patch 會替換完整 config，而不是深層合併？

深層合併會讓 entry 的最終設定分散在多個層中。要理解某個鍵的來源，必須重放所有曾修改它的層，base 預設值也可能無意間流入不需要它的 mode。完整替換則讓最後一個 patch 擁有該 entry 的全部設定。代價是，各 mode 必須重複列出完整 config，但設定來源因此更容易理解與除錯。本章依下列規則實作：

1. 一份扁平 entry 清單就是產品的完整描述：每筆資料包含 `{id, name, config}`，不放任何 callable。
2. 清單從空集合開始，依序套用 patch 層：先 bundle，再 profile 與使用者設定，後套用的層優先。
3. 三種 patch 動作都以 entry id 為鍵：新 id 代表插入，既有 id 會整份替換 config，`disabled` 則移除 entry。
4. 統一的名稱對照表會將 entry name 解析成 plugin factory，讓設定資料只在單一入口連接程式碼。
5. plugin 的掛載時機取決於所需 service 是否就緒，而不是 entry 在清單中的位置。
6. 載入器會持續掛載可啟動項目，直到無法再前進；若仍有 entry 等待，就拒絕啟動，並列出每個 entry 缺少的 service。

---

## 核心機制

只新增一個檔案 `composition.py`，前面沿用的檔案都沒有修改：

- **`apply_layers(layers)`**：依序套用 patch 層，產生扁平 entry 清單；三種操作都以 id 為鍵。
- **`mount_entries(ctx, entries, plugins)`**：載入器。entry 所需 service 就緒後才掛載 plugin；若依賴始終無法滿足，會明確拒絕啟動。
- **`PLUGINS`**：名稱對照表，將 entry 的 `name` 對應到 `config -> plugin` factory；每個 factory 都會宣告掛載所需的 service。
- **`MINI_BASE`**：基礎 bundle：第 00 章到 12 一路用手組出來的整套 harness，這次是十六個 entry 的資料。

套用邏輯很精簡：先檢查 id 是否已存在，再決定插入、替換或移除；完整替換只需要一行指派：

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

這行指派直接表達完整替換的規則。patch 不會讀取舊 config，也不需要逐鍵合併；無論前面幾層曾設定什麼，最後套用的 patch 都擁有完整結果。

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

因此，entry 順序不代表載入順序。即使將基礎 bundle 反向排列，只要 service 依賴相同，最後仍會得到同一個產品。若某個 entry 需要的 service 始終未出現，載入器會拒絕啟動並列出缺少項目，避免系統在功能不完整的狀態下運作。

基礎 bundle 也示範了完整替換。每種 mode 都需要 model，因此 base 先提供 stand-in；不同 profile 再以完整 config 替換它：

```python
{"id": "model", "name": "scripted-llm",
 "config": {"name": "scripted", "responses": []}},
{"id": "agent", "name": "agent",
 "config": {"agent": "a1", "session": "s1", "model": "scripted"}},
```

需要實際模型的 profile 不會只為 agent entry 合併 `{"model": "live"}`，而是提供完整 agent config，並另外插入 adapter entry。base 的舊欄位不會意外保留，因此只看最後一層就能理解實際設定。

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

這段紀錄涉及的 loop、pipeline、sandbox 與 prompt 都由 entry 掛載。若停用其中一個 entry，例如 skills，相同 harness 執行相同腳本時就會由第 05 章的工具 pipeline 回傳 `unknown tool 'skill'`。整個子系統可以透過資料移除，不需修改程式碼。

### 改了什麼

與第 12 章相比：

- 所有既有檔案都完整沿用：`agent_loop.py`、`capabilities.py`、`inbox.py`、`jobs.py`、`kernel.py`、`message.py`、`scheduler.py`、`session_log.py`、`skills.py`、`standin.py`、`subagent.py`、`system_prompt.py`、`tools.py`。`composition.py` 是唯一新增的原始碼檔案，因此與第 12 章相比，diff 只包含本章新增的機制，不包含其他改動。
- 既有機制都不需要為了能被組合而改過。這些 entry 掛的，就是前面每次檢查手動掛上的同一批 plugin，走的也是第 01 章那道 `ctx.plugin()`；model 那個 entry 是透過第 10 章的 llm seam 接到 loop 的，adapter 用名字註冊，每次呼叫才解一次。
- log 沒有多出任何新的事件型別。組合這件事發生在第一個 turn 打開之前；組出來的產品，它的紀錄跟用手搭的那份分不出差別，而這正是重點。
- `demo.py`：實機示範在一層 live 的 profile 底下啟動基礎 bundle：插入一個 adapter entry、插入一個 worker entry、把 scripted 的 model entry 停掉，再把 agent 那個 entry 的 config 整份換掉，讓它的 model 變成真的那個。
- 這是最後一個章節。第 00 章到 12 一片一片教出來的 harness，現在是空清單上的十六個 entry，而「一切都是 plugin，而且每一次註冊都可以反向撤銷」這句話，最後收在資料上：一個產品，就是對著空無一物做出來的一份 diff。

---

## 對照真正的 dsh

以下連結皆指向研究版本 [`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca)。這一層是啟動平面：[`apps/cli`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/apps/cli)、[`packages/boot/app-boot`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/boot/app-boot)，以及 [`packages/bundle`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/bundle) 底下那些 bundle。

| Mini-dsh | 真正的 dsh | 說明 |
| --- | --- | --- |
| 在空清單上跑的 `apply_layers` | [`apps/cli/src/profile-boot.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/apps/cli/src/profile-boot.ts)（第 142 到 171 行） | 層的順序是鎖死的：先是照 `dsh.profile.bundles` 排的那些 bundle，接著是 profile 的 `cordis.patch.yml`，再來是 `$DSH_HOME/cordis.patch.yml`，最後是 `--patch` 疊上去的那幾層。 |
| 三個動作，換就換整份 | [`packages/bundle/base/cordis.patch.yml`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/bundle/base/cordis.patch.yml)（第 6 到 10 行），由 [`vendor/include`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/vendor/include) 裡的 `applyEntryPatches` 套用 | 一個 patch 用 id 指定一個 entry，然後把它整份 `config` 換掉，從不合併；再不然就是插入新的 entry。 |
| `MINI_BASE`，十六個 entry | [`packages/bundle/base/cordis.patch.yml`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/bundle/base/cordis.patch.yml) | `@deepseek-ai/dsh-base` 有 78 個 entry；headless 模式在 [`packages/bundle/headless/cordis.patch.yml`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/bundle/headless/cordis.patch.yml) 裡再加 6 個。完整，但不小。 |
| `PLUGINS` 這張名字對照表 | [`packages/boot/app-boot/src/profile.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/boot/app-boot/src/profile.ts)：`resolveBundleDir`（第 344 行）、`PROFILE_TEMPLATES`（第 114 到 117 行） | 名字會解到磁碟上真正的套件；出貨的樣板是 `web = [dsh-base, dsh-web-app]` 和 `headless = [dsh-base, dsh-headless]`。 |
| 在一個全新的 `Context()` 上跑 `mount_entries` | [`packages/boot/app-boot/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/boot/app-boot/src/index.ts)：`boot()`（第 757 行），entry 是透過 [`vendor/loader/src/config/entry.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/vendor/loader/src/config/entry.ts) 掛上去的 | `boot()` 就是先 `new Context()`，再 `ctx.plugin(Loader)`；每一個 entry 變成一次 plugin 掛載，每一次移除變成一次卸載，就是第 01 章那份約定放大到整個產品的規模。 |
| 持續掛載後再清查 | [`packages/boot/app-boot/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/boot/app-boot/src/index.ts)：`assertEntriesActivated`（第 700 到 725 行） | entry 順序不代表載入順序；是否啟動取決於所需 service 是否就緒，清查則會列出所有仍在等待缺少 service 的 entry。 |

真正的 dsh 組合層還提供以下功能：

- **可動態更新的 entry 清單。** Loader 本身也是 plugin；修改 entry 時，執行中的 process 只會掛載或卸載對應差異，HMR 也沿用相同機制。Mini-dsh 不實作動態更新與 HMR。
- **profile 定義產品組合。** `dsh --profile web` 和 `dsh --profile headless`（[`apps/cli/src/args.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/apps/cli/src/args.ts)）會從 `PROFILE_TEMPLATES` 選擇不同 bundle 組合；同一個執行檔可因此形成兩種產品。
- **每個 agent 各自的組合：preset。** 這不是 profile 那種層。[`@deepseek-ai/dsh-agent-presets`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/preset/agent-presets/src/mount.ts) 在每個 process 裡把一棵 `agent.cordis.yml` 的子樹掛一次，每個 session 再把自己的 agent 作用域接到它底下來加入；profile 的組合是整個 process 共用一份，preset 則是每個 agent 一份。
- **YAML entry 與完整模組解析。** entry 儲存在使用者可修改、可比較 diff 的 `cordis.patch.yml`，名稱再透過 `resolveBundleDir` 解析成 npm 套件。Mini-dsh 的 `PLUGINS` 字典扮演相同角色，但省略檔案系統解析。

---

## 常見失敗模式

- **深層合併會讓最終 config 分散在多層。** 要追查一個欄位，就必須重放所有修改過它的 patch。完整替換把答案集中在最後一個 entry，來源更清楚。
- **共用預設值可能滲入不需要它的 mode。** base 的 `{"name": "scripted", "responses": []}` 若與 profile 深層合併，舊欄位可能意外保留。完整替換可確保未明確列出的鍵不會沿用。
- **將清單位置視為載入順序，會讓 patch 改變啟動時序。** 新 entry 通常加入清單尾端，若位置具有語意，就可能重排其他項目。改由 service 依賴決定掛載後，patch 可以插入任意位置。
- **未清查等待中的 entry，可能啟動出功能不完整的產品。** 某個 entry 若一直缺少 service，harness 可能表面啟動成功，實際上 agent 根本不存在。載入完成後必須檢查剩餘 entry，並明確指出缺少的依賴。
- **config 若包含 callable，就無法作為可 patch 的資料。** callable 不適合寫入檔案、比較 diff 或由後續層替換。實際模型應透過名稱與 adapter entry 解析，config 本身只保存純資料。

---

## 動手驗證

[`src/`](src/) 延續第 12 章，並加入：

- [`composition.py`](src/composition.py)（新增）：patch 的套用器、由 service 到齊與否決定掛載並帶著先跑到停再清查的載入器、`PLUGINS` 名字對照表，還有十六個 entry 的 `MINI_BASE` bundle。
- [`test.py`](src/test.py)：離線測試證明幾件事：三個動作能在空清單上一層層疊起來；一次換掉會整份收下 patch 的 config，base 一點都不會漏過來；那十六個 entry 能把 harness 啟動起來，而且一個 turn 會穿過組出來的 sandbox 和 shell 兩個 entry；base 整份倒著寫，啟動結果一模一樣；一個 disable 的 entry 就能把 skills 這個子系統拿掉；少一個 service 的啟動會被拒絕，而且每個還在等的 entry 都被點名。
- [`demo.py`](src/demo.py)：實機示範在基礎 bundle 上疊一層 live 的 profile，把組出來的 entry 印出來，再證明它們是活的：一個穿過 sandbox 那幾個 entry 的 shell turn，加上一次前景委派給 worker entry 裡那個 Provider。

```bash
python sections/13-composition/src/test.py    # offline check, no key
```

實機示範需要根目錄的 `requirements.txt` 和一把 key；沒有設定 key 時會自動跳過：

```bash
pip install -r requirements.txt         # anthropic + python-dotenv
cp .env.example .env                    # then set ANTHROPIC_API_KEY
python sections/13-composition/src/demo.py
```

---

## 參考資料

- [`docs/architecture.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/architecture.md)：講 Profiles 和 bundle 的那一節：entry 清單、patch 層，還有出貨的那幾疊 bundle。
- [`apps/cli/src/profile-boot.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/apps/cli/src/profile-boot.ts)：層是怎麼疊起來的：bundle、profile 的 patch、home 的 patch，最後是 `--patch` 疊上去的，就這個順序。
