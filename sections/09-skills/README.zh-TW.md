<!-- source: README.md @ 75fe15e -->

# 09 · Skills

[English](README.md) | 繁體中文 | [简体中文](README.zh-CN.md)

> 一份清單是菜單，不是那頓飯。每一次 request 都要為那些名字付錢，一個名字一
> 行；全文要等 model 開口點了才會上桌。

Section 08 的 request 帶著穩定的 system 文字和一份會變的快照，但它帶的每一個
字，每個 step 都還是要送一次。指示文字塞不進這個預算：一套 harness 會慢慢累積各
種專門工作的操作說明，而任何一個 turn 用得到的，幾乎都只有其中一小塊。

兩個最直覺的放法都不對。全部寫死進 system 文字，每一次 request 就得為所有指示付
錢，用不用得到都一樣。整包都不放，model 連聽都沒聽過的東西，當然也用不上。

而且這組東西不是固定的。skill 文字的主人很多：內建的一批、一個工作區、一個
plugin。session 還在跑的時候，每一方都可能掛上、卸下，或者蓋掉某個名字，而且誰
都不准為了這件事去改別人的文字。

所以：為什麼 skill 清單是當成 context 注入，內容卻要靠一次 tool 呼叫才載進來？

因為「有哪些東西」必須便宜、而且隨時看得到，「東西說了什麼」則是用到才付錢。要
做到這件事，registry 必須：

1. 收的是 provider，不是 skill：每個 provider 用兩個動作把名字換成指示文字，
   `list()` 給摘要，`get(name)` 給一份完整內容。
2. provider 要分層：後註冊的會蓋掉先註冊的同名項目，而且每一次註冊都會回傳它的
   撤銷函式。
3. 清單當成 context 注入：名字和一行說明搭 runtime-context 快照的便車，只有清單
   變了才重發。
4. 內容用一個 `skill` tool 按需載入，所以那段文字是以一筆普通的 `tool/result`
   落地。
5. 碰到不認得的名字，就回一則正常的錯誤結果，絕不往外丟例外。
6. 清單是空的時候，什麼都不送。

---

## Mechanism

一個新檔案 `skills.py`，搬過來的檔案一個都沒動：

- **`SkillRegistry`**：一層一層的 provider，照註冊順序疊。`catalog()` 把每個
  provider 的 `list()` 摘要合起來，看得到的名字每個一行，同名的話後面那層的那行
  贏。`get(name)` 反過來從最上層往回走，回傳找到的第一份內容。`register()` 照
  kernel 的做法回傳撤銷函式。
- **`MemorySkillProvider`**：最簡單的 provider，就是一個
  `name -> {"description", "body"}` 的 dict。任何物件只要有 `list()` 和
  `get(name)` 就算 provider；`list()` 絕不會主動把內容端出來。
- **`skills_plugin`**：把這個分工接起來。一個 Section 08 的 context provider 把
  `catalog_text()` 算進快照，一個 `skill` tool 負責載入內容，registry 本身則以
  `skills` 這個名字提供出去。

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

這份清單不需要任何新的投遞機制。它只是 Section 08 那個 registry 上多出來的一個
context provider，所以它什麼時候會再進 log 一次，早就由快照去重決定好了：
provider 一變就重發，清單安安靜靜的時候一毛錢都不花。

```python
ctx.effect(
    ctx.get("system_prompt").context(
        "skills", lambda ac: skills.catalog_text(), order=100
    ),
    "skill catalog",
)
```

內容走的是另一條路：Section 05 蓋好的那條 tool pipeline。名字不認得的時候，tool
的實作裡會丟出例外，pipeline 再把它變成一則正常的 `is_error` 結果，所以對話紀錄
的形狀不會被弄壞：

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

下面是一次真的執行，照 log 記下來的樣子。清單上有兩個 skill 的名字；model 載了
其中一份內容、照著做，第二個 step 則發現清單沒變：

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

seq 7 那份內容，現在是推導歷史的一部分，一則普通的 `tool` 訊息：這個 session 後
面每一次 request 都要為它付錢，但那是因為 model 自己開口要的。`greet` 的內容從
頭到尾沒人要過，所以一個 token 都沒花。

### 改了什麼

跟 Section 08 比起來：

- 搬過來的檔案全都原封不動：`agent_loop.py`、`inbox.py`、`kernel.py`、
  `message.py`、`scheduler.py`、`session_log.py`、`standin.py`、
  `system_prompt.py`、`tools.py`。`skills.py` 是唯一的新原始碼檔案，所以跟 08
  的 diff 剛好就是這個 Section 的 Mechanism，沒有別的。
- loop 完全沒改，因為這個 Mechanism 純粹是 plugin：清單從 Section 08 的 context
  provider 進來，內容從 Section 05 的 tool 進來。這是第一個 Section，它的
  Mechanism 不用動到任何搬過來的檔案，就放得進去。
- log 沒有多出新的事件型別。快照那一筆現在可能夾著清單那一段，`tool/result` 那
  一筆可能夾著一份 skill 內容；推導歷史的時候，兩者就是普通的紀錄，照普通的方式
  處理。
- `demo.py`：Live demo 給真的 model 一份清單，讓它自己開口載一份內容，再趁兩個
  turn 之間註冊第二個 provider，所以重發這件事會發生在一次真的 model 呼叫上。

---

## In real dsh

所有指過去的連結都固定在鎖定的 Studied version
[`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca)
上。registry 住在 skill 這個套件家族裡：
[`packages/skill`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/skill)。

| Mini-dsh | 真正的 dsh | 說明 |
| --- | --- | --- |
| `skills.py` 裡的 `SkillRegistry` | [`packages/skill/skill/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/skill/skill/src/index.ts)：`SkillRegistry` | 真正的 registry 繼承 `Service`，掛在 `ctx.skills` 底下，跟 mini 一樣是個複數形的 seam。它的層知道 scope（`SkillLayer implements ScopeLayer`）；mini 就只照註冊順序疊。 |
| provider 的 duck type（`list()` / `get(name)`） | [`index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/skill/skill/src/index.ts)：`SkillProvider` | 一個把名字換成指示文字的介面（第 248 行），不是 Service。註冊時收的是一個工廠函式，它會拿到一個 `SkillProviderControl`（第 391 行），也就是 mini 那個撤銷函式在真實世界裡的樣子。 |
| `MemorySkillProvider` | [`packages/skill/skill-filesystem/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/skill/skill-filesystem/src/index.ts)：`FileSystemSkillProvider` | 出貨的那個 provider 是去磁碟上解 skill 目錄的（第 146 行）；mini 用 dict 撐起來的 provider，讓 Offline check 完全不碰檔案系統。 |
| 清單的 context provider | [`packages/skill/tool-skill/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/skill/tool-skill/src/index.ts) | 真正的使用端是從 `agent/pre-step` 的 listener 把清單發出去的（第 177、213 行），也就是 Section 08 指過的那條 pre-step 通道。mini 沒有 pre-step hook，所以它的清單改搭快照那條 context 通道。 |
| `skill` 這個 tool | [`tool-skill/src/index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/skill/tool-skill/src/index.ts) | 內容一樣是按需透過 tool 載入的（第 82 行）：清單和內容一樣分成兩邊，也是靠同樣那兩條通道送出去。 |
| 拿快照去重當變更訊號 | [`index.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/packages/skill/skill/src/index.ts)：`skills/change` | 真正的 registry 會用一個 bus 事件公告 provider 有變（第 297 行），使用端收到就把快取作廢；mini 則是每次組裝都重算一次，安靜的那些 step 就交給快照去重吸收掉。 |

真正的 skills 這一層，在這個 Section 的 Mechanism 之上還多做了這些：

- **層知道 scope。**`SkillLayer implements ScopeLayer`，用的跟 tool registry 是
  同一套機制，所以 subagent 的 scope 可以看到跟父層不一樣的清單。mini 的層是全
  域的；它那條覆蓋規則是同一個想法，只是少了一個維度。
- **provider 手上有一個可以控制的 handle。**註冊收的是一個工廠函式，它會拿到一個
  `SkillProviderControl`，所以 provider 可以主動推變更通知，`skills/change` 事
  件再把通知擴散給有做快取的使用端。mini 每次組裝都重算一次清單，根本沒有快取需
  要作廢。
- **有一個檔案系統的 provider。**`FileSystemSkillProvider` 會走過 skill 目錄，
  只讀摘要、不載內容，所以省 token 這件事，在 I/O 這一層也一樣守得住。
- **pre-step 那條投遞通道。**真正的清單，是由 `agent/pre-step` 的 listener 追加
  成 `user/message` 的，`packages/context` 底下大部分東西走的都是這一條。mini
  是透過 Section 08 的 context registry，走到同樣那幾筆 log 紀錄。

---

## Failure modes

- **內容直接放進清單，等於永遠為全部付錢。**把每一份指示都內嵌進去，每一次
  request 就要扛著全部，可是一個 turn 最多用到一份。`list()` 只給名字和一行說
  明；`get(name)` 是內容唯一的出口。
- **清單塞進 system 文字，前綴就被推走了。**Section 08 承諾的是一個位元組都不差
  的 system 文字；session 中途掛上一個 provider 就會把它改掉，prompt 前綴快取跟
  著報銷。改成走 context，清單變一次只花一筆 `user/message`，前綴穩穩不動。
- **不認得的名字直接往外丟例外，會把對話紀錄撕破。**model 遲早會把某個 skill 的
  名字拼錯。`skill` 這個 tool 的實作丟出例外，Section 05 的 pipeline 用一則正常
  的 `is_error` 結果回應，turn 就繼續跑下去，而不是把 loop 弄垮。
- **照時間先後分層，清單就會亂跳。**如果解出來的結果取決於 dict 順序或執行緒的
  快慢，同樣的註冊就會算出不一樣的清單，而每不一樣一次，就白白多發一筆快照。分
  層照的是註冊順序，後面的贏：同一組 provider 永遠算出同樣的文字。
- **清單一做快取，就會跟 provider 對不上。**把算好的那一段快取起來，某個註冊已
  經被撤銷的 provider 還會繼續宣傳一批根本解不出來的 skill。mini 每次組裝都重算
  一次；讓安靜的 step 不花錢的是快照去重，不是快取。

---

## 跑跑看

[`src/`](src/) 把 08 搬過來，然後加上：

- [`skills.py`](src/skills.py)（新的）：`SkillRegistry`，provider 分層、同名互
  相覆蓋的解法；`MemorySkillProvider`；還有那個 plugin，把清單的 context、
  `skill` tool 和 `skills` 這個 service 接起來。
- [`test.py`](src/test.py)：Offline check 證明清單是搭快照那一筆進來的，裡面一
  份內容都沒有；內容只有在一次 `skill` 呼叫之後，才以 `tool/result` 的身分出
  現；provider 變了清單就重發，沒變就安安靜靜；後面那層會蓋住一個名字，直到它的
  撤銷函式被呼叫，下面那層才露出來；不認得的名字就是一則正常的錯誤結果；清單空
  的時候什麼都不送。
- [`demo.py`](src/demo.py)：Live demo 讓真的 model 讀清單、自己開口載一份內容，
  最後用一個 skill 收尾，而那個 skill 的 provider 是在兩個 turn 之間才註冊上去
  的。

```bash
python sections/09-skills/src/test.py    # offline check, no key
```

Live demo 需要根目錄的 `requirements.txt` 和一把 key；沒有 key 的話，它會安靜地
跳過：

```bash
pip install -r requirements.txt         # anthropic + python-dotenv
cp .env.example .env                    # then set ANTHROPIC_API_KEY
python sections/09-skills/src/demo.py
```

---

## 出處

- [`docs/subsystems/skills.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/subsystems/skills.md)：
  dsh 自己帶你走一遍 skill registry、它的 provider，還有清單和內容分家這件事。
