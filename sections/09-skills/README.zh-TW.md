<!-- source: README.md @ 3705bd7 -->

# 09 · Skills

[English](README.md) | 繁體中文 | [简体中文](README.zh-CN.md)

> skill 內容往往很長，不適合每個 step 都重複傳送；但如果完全不提供，模型也不知道它們存在。因此，請求只帶上 skill 名稱與摘要，完整內容等到真正需要時再載入。

第 08 章的請求已經包含穩定的 system prompt 和可變的 runtime context，但其中每個字仍會在每個 step 重複傳送。隨著 harness 累積越來越多專用操作指南，將所有內容都放進請求會快速消耗 context 預算，而一個 turn 通常只會用到其中少數幾項。

全部寫入 system prompt，會讓每次請求都支付所有 skill 的 token 成本，不論是否使用。如果什麼都不提供，模型又無法主動選用它不知道的 skill。

此外，skill 並非固定不變。內建功能、工作區和 plugin 都可以提供 skill，並在 session 執行期間動態掛載、卸載或覆寫同名項目，各來源之間不應直接修改彼此的內容。

因此，本章要回答的問題是：為什麼 skill 清單以 context 形式注入，完整內容卻要透過工具呼叫才載入？

模型需要以低成本隨時知道「有哪些 skill」，但只在使用時才載入「skill 的完整內容」。registry 因此需要：

1. registry 儲存的是 provider，而不是 skill 本身。每個 provider 透過 `list()` 提供摘要，透過 `get(name)` 提供完整內容。
2. provider 採分層設計：後註冊的同名項目會覆寫先前版本，每次註冊都會回傳撤銷函式。
3. 清單會隨 runtime-context 快照一起注入，內容包含名稱與一行說明，只有清單變更時才重發。
4. 完整內容透過 `skill` 工具按需載入，並以一般 `tool/result` 寫入歷史。
5. 名稱不存在時，回傳一般錯誤結果，不讓例外穿過工具邊界。
6. 清單是空的時候，什麼都不送。

---

## 核心機制

本章只新增 `skills.py`，其他檔案維持不變：

- **`SkillRegistry`**：一層一層的 provider，照註冊順序疊。`catalog()` 把每個 provider 的 `list()` 摘要合起來，看得到的名字每個一行，同名的話後面那層的那行贏。`get(name)` 反過來從最上層往回走，回傳找到的第一份內容。`register()` 照 kernel 的做法回傳撤銷函式。
- **`MemorySkillProvider`**：最簡單的 provider，就是一個 `name -> {"description", "body"}` 的 dict。任何物件只要有 `list()` 和 `get(name)` 就算 provider；`list()` 絕不會主動把內容端出來。
- **`skills_plugin`**：把這個分工接起來。一個第 08 章的 context provider 把 `catalog_text()` 算進快照，一個 `skill` tool 負責載入內容，registry 本身則以 `skills` 這個名字提供出去。

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

清單不需要新的投遞機制，只要作為第 08 章 registry 中的一個 context provider。快照去重會決定何時再次寫入 log：provider 改變時重發，清單不變時不增加額外 token。

```python
ctx.effect(
    ctx.get("system_prompt").context(
        "skills", lambda ac: skills.catalog_text(), order=100
    ),
    "skill catalog",
)
```

內容走的是另一條路：第 05 章蓋好的那條 tool pipeline。名字不認得的時候，tool 的實作裡會丟出例外，pipeline 再把它變成一則正常的 `is_error` 結果，所以對話紀錄的形狀不會被弄壞：

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

以下是實際執行時的 log。清單中有兩個 skill；模型按需載入其中一份內容並照著執行，第二個 step 則確認清單沒有變化：

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

seq 7 的內容現在成為推導歷史的一部分，也就是一則普通的 `tool` 訊息。後續 request 會持續攜帶它，但這是模型主動要求載入的結果。`greet` 從未被要求，因此完整內容不會產生任何 token 成本。

### 改了什麼

與第 08 章相比：

- 所有既有檔案都完整沿用：`agent_loop.py`、`inbox.py`、`kernel.py`、 `message.py`、`scheduler.py`、`session_log.py`、`standin.py`、 `system_prompt.py`、`tools.py`。`skills.py` 是唯一的新原始碼檔案，因此與第 08 章相比，diff 只包含本章新增的機制，不包含其他改動。
- loop 完全沒改，因為這項機制純粹是 plugin：清單從第 08 章的 context provider 進來，內容從第 05 章的 tool 進來。這是第一個完全透過 plugin 組合完成的章節，不需要修改任何既有檔案。
- log 沒有多出新的事件型別。快照那一筆現在可能夾著清單那一段，`tool/result` 那一筆可能夾著一份 skill 內容；推導歷史的時候，兩者就是普通的紀錄，照普通的方式處理。
- `demo.py`：實機示範給實際模型一份清單，讓它自己開口載一份內容，再趁兩個 turn 之間註冊第二個 provider，所以重發這件事會發生在一次實際模型呼叫上。

---

## 對照真正的 dsh

以下連結皆指向研究版本 [`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca)。registry 位於 skill 這個套件家族裡： [`packages/skill`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/skill)。

| Mini-dsh | 真正的 dsh | 說明 |
| --- | --- | --- |
| `skills.py` 裡的 `SkillRegistry` | [`packages/skill/skill/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/skill/skill/src/index.ts)：`SkillRegistry` | 真正的 registry 繼承 `Service`，掛在 `ctx.skills` 底下，跟 mini 一樣是個複數形的 seam。它的層知道 scope（`SkillLayer implements ScopeLayer`）；mini 就只照註冊順序疊。 |
| provider 的 duck type（`list()` / `get(name)`） | [`index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/skill/skill/src/index.ts)：`SkillProvider` | 一個把名字換成指示文字的介面（第 248 行），不是 Service。註冊時收的是一個工廠函式，它會拿到一個 `SkillProviderControl`（第 391 行），也就是 mini 那個撤銷函式在真實世界裡的樣子。 |
| `MemorySkillProvider` | [`packages/skill/skill-filesystem/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/skill/skill-filesystem/src/index.ts)：`FileSystemSkillProvider` | 出貨的那個 provider 是去磁碟上解 skill 目錄的（第 146 行）；mini 用 dict 撐起來的 provider，讓 離線測試完全不碰檔案系統。 |
| 清單的 context provider | [`packages/skill/tool-skill/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/skill/tool-skill/src/index.ts) | 真正的使用端是從 `agent/pre-step` 的 listener 把清單發出去的（第 177、213 行），也就是第 08 章指過的那條 pre-step 通道。mini 沒有 pre-step hook，所以它的清單改搭快照那條 context 通道。 |
| `skill` 這個 tool | [`tool-skill/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/skill/tool-skill/src/index.ts) | 內容一樣是按需透過 tool 載入的（第 82 行）：清單和內容一樣分成兩邊，也是靠同樣那兩條通道送出去。 |
| 以快照去重偵測變更 | [`index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/skill/skill/src/index.ts)：`skills/change` | 真正的 registry 會透過 bus 事件公告 provider 變更（第 297 行），讓使用端清除快取；Mini-dsh 則在每次組裝時重算，再由快照去重避免重複寫入。 |

真正的 skills 這一層還提供以下功能：

- **層知道 scope。**`SkillLayer implements ScopeLayer`，用的跟 tool registry 是同一套機制，所以 subagent 的 scope 可以看到跟父層不一樣的清單。mini 的層是全域的；它那條覆蓋規則是同一個想法，只是少了一個維度。
- **provider 手上有一個可以控制的 handle。**註冊收的是一個工廠函式，它會拿到一個 `SkillProviderControl`，所以 provider 可以主動推變更通知，`skills/change` 事件再把通知擴散給有做快取的使用端。mini 每次組裝都重算一次清單，根本沒有快取需要作廢。
- **有一個檔案系統的 provider。**`FileSystemSkillProvider` 會走過 skill 目錄，只讀摘要、不載內容，所以省 token 這件事，在 I/O 這一層也一樣守得住。
- **pre-step 那條投遞通道。**真正的清單，是由 `agent/pre-step` 的 listener 追加成 `user/message` 的，`packages/context` 底下大部分東西走的都是這一條。mini 是透過第 08 章的 context registry，走到同樣那幾筆 log 紀錄。

---

## 常見失敗模式

- **內容直接放進清單，等於永遠為全部付錢。**把每一份指示都內嵌進去，每一次 request 就要扛著全部，可是一個 turn 最多用到一份。`list()` 只給名字和一行說明；`get(name)` 是內容唯一的出口。
- **把清單放進 system 文字會破壞穩定前綴。** session 中途掛載 provider 時，system prompt 會跟著改變，導致前綴快取失效。改走 context 後，清單每次變更只增加一筆 `user/message`，system 文字仍保持不變。
- **未知名稱的例外若穿過工具邊界，會留下不完整的對話。** 模型可能拼錯 skill 名稱，因此 `skill` 工具的例外必須由第 05 章的 pipeline 轉成一般 `is_error` 結果，讓 turn 可以繼續執行。
- **分層結果若依賴執行時序，清單就不穩定。** 若結果取決於 dict 順序或執行緒完成時間，同一組註冊可能產生不同清單，並觸發不必要的快照。改用註冊順序決定覆寫關係，結果就能保持一致。
- **清單一做快取，就會跟 provider 對不上。**把算好的那一段快取起來，某個註冊已經被撤銷的 provider 還會繼續宣傳一批根本解不出來的 skill。mini 每次組裝都重算一次；讓安靜的 step 不花錢的是快照去重，不是快取。

---

## 動手驗證

[`src/`](src/) 延續第 08 章，並加入：

- [`skills.py`](src/skills.py)（新的）：`SkillRegistry`，provider 分層、同名互相覆蓋的解法；`MemorySkillProvider`；還有那個 plugin，把清單的 context、 `skill` tool 和 `skills` 這個 service 接起來。
- [`test.py`](src/test.py)：確認快照只包含 skill 清單，不包含完整內容；內容只有在呼叫 `skill` 後才以 `tool/result` 出現；provider 變更時清單會重發，未變時不會新增事件；後註冊層會覆寫同名項目，撤銷後恢復下層版本；未知名稱會回傳一般錯誤結果；空清單不會送出任何內容。
- [`demo.py`](src/demo.py)：實機示範讓實際模型讀清單、自己開口載一份內容，最後用一個 skill 收尾，而那個 skill 的 provider 是在兩個 turn 之間才註冊上去的。

```bash
python sections/09-skills/src/test.py    # offline check, no key
```

實機示範需要根目錄的 `requirements.txt` 和一把 key；沒有設定 key 時會自動跳過：

```bash
pip install -r requirements.txt         # anthropic + python-dotenv
cp .env.example .env                    # then set ANTHROPIC_API_KEY
python sections/09-skills/src/demo.py
```

---

## 參考資料

- [`docs/subsystems/skills.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/subsystems/skills.md)： dsh 自己帶你走一遍 skill registry、它的 provider，還有清單和內容分家這件事。
