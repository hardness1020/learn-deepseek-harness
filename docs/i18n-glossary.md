# i18n glossary

How this repo's Chinese translations are written. Read this before starting or
reviewing a translation batch.

## Files and switcher

Translations are sibling files next to each English `README.md`:
`README.zh-TW.md` (繁體中文) and `README.zh-CN.md` (简体中文).

Every README that has been translated carries a language switcher line, the
English one included. The current language is plain text, the other two are
links:

```text
English | [繁體中文](README.zh-TW.md) | [简体中文](README.zh-CN.md)
```

It sits at the top of the file, inside the centered header block when the file
has one. A section README has no such block, so its line goes immediately after
the `# NN · Title` heading, one blank line on each side, before the
epigraph. A README gets its line in the same batch that translates it, never
before, because the links would point at files that do not exist yet. The root
README and all 14 sections now carry it.

Every zh file opens with an HTML comment naming the English commit it tracks:

```text
<!-- source: README.md @ 75b51d8 -->
```

The sha names the commit whose English prose the translation covers. Adding or
fixing the switcher line in that same commit does not bump it; a prose change
does.

`zh-CN` is derived from `zh-TW`: character conversion plus the regional term
swaps below. Never a bare character conversion.

Chinese prose is never hard-wrapped, however the English file is wrapped. One
paragraph is one line. Markdown turns a soft line break into a space, and
between two Chinese characters that space is visible in the rendered page, so a
wrapped translation reads with stray gaps GitHub puts there. Tables, code, and
everything else copied verbatim keep the English line structure.

## Voice

- Oral and conversational, matching the register of the English.
- 你, never 您.
- No internet slang, no memes.
- No em-dashes, in any language, including the Chinese 破折號.
- Epigraphs are original prose, not quotations. Translate them fully.

## Sentence shape

Translate the meaning, then throw the English sentence away and say that meaning
in Chinese. A line that keeps the English word order or an English metaphor
reads like a machine wrote it, even when every word is correct. Six patterns,
all six caught on the root README batch:

**1. An English metaphor rarely survives the trip.** Check the picture the verb
paints before reusing it.

| English | 不要 | 要 |
| --- | --- | --- |
| pinned at a version | 釘在 | 固定在, 鎖定在 |
| the system is built on it | 照這條規則蓋起來 | 建立在這條規則之上 |
| a real harness | 一套真的 harness, 一套貨真價實的 harness | 一套 harness, 讓後面的描述說明它不是玩具 |
| hit the API | 打 API | 呼叫 API, 调用 API |

**2. A trailing English qualifier moves to the front.** English can hang a
condition off the end of a clause. Chinese states it first.

- 不要: 這個 Section 要回答的那一個設計問題，在任何程式碼之前。
- 要: 還沒碰到任何程式碼之前，先講清楚這個 Section 要回答的那一個設計問題。

**3. A long pre-noun modifier splits into two clauses.** English stacks
modifiers in front of a noun. Past roughly ten characters, Chinese chokes.

- 不要: 一張把你的 Mini-dsh 符號對到真 dsh 符號的表
- 要: 一張對照表，把你寫的 Mini-dsh 符號對到真 dsh 的符號

**4. An English noun often wants to be a Chinese verb.**

- 不要: Offline check 就是這份 tutorial 的證明。
- 要: 這份 tutorial 講的每件事，都由 Offline check 來證明。

**5. Hyphenated compounds get unpacked into a clause.** Chinese has no
equivalent word-formation, so a literal rendering is opaque.

- 不要: 平行安全的呼叫, 以擁有者為界的協定
- 要: 可以平行跑的呼叫, 只有擁有者能動的協定

**6. Put back the subject English dropped.** English leans on the surrounding
paragraph for a noun Chinese needs on the spot.

- 不要: 為什麼清單是當成 context 注入
- 要: 為什麼 skill 清單是當成 context 注入

Two smaller habits. Do not let one word carry two senses within a paragraph
(結果固定的 check colliding with 連結固定在). And prefer the ordinary verb over
the vivid one when the vivid one is regional.

The check: read the finished file aloud with the English closed. Anything you
would not say out loud gets rewritten, no matter how faithful it is.

## Headings

A section README's `# NN · Title` stays English verbatim: the Section titles
also name directories and diagram nodes. The rest of the section headings are
fixed, so their anchors do not drift between batches:

| English | 繁體中文 | 简体中文 |
| --- | --- | --- |
| `## Mechanism` | `## Mechanism` | `## Mechanism` |
| `### What changed` | `### 改了什麼` | `### 改了什么` |
| `## In real dsh` | `## In real dsh` | `## In real dsh` |
| `## Failure modes` | `## Failure modes` | `## Failure modes` |
| `## Runnable` | `## 跑跑看` | `## 跑跑看` |
| `## Sources` | `## 出處` | `## 出处` |

Mechanism, In real dsh, and Failure modes stay English because they are
whitelist terms, which keeps their anchors English too.

The In-real-dsh table header `| Mini-dsh | Real dsh | Notes |` becomes
`| Mini-dsh | 真正的 dsh | 說明 |` (zh-CN: `| Mini-dsh | 真正的 dsh | 说明 |`). Cells
holding only a symbol, path, or link stay byte-identical; the Notes column is
prose.

## Translate prose only

These stay byte-identical to the English file:

- Mermaid diagrams
- Code blocks, shell commands, and expected output
- File trees
- Badges and their URLs
- Identifiers, event names, file paths, and API names from dsh or Mini-dsh

## English terms, no Chinese gloss

Never gloss these in parentheses. A heading made only of these terms stays
English verbatim, so its anchor stays English too.

The first line is the settled core. The rest are the repo's own nouns from
`CONTEXT.md` plus terms the translations already lean on; add to it only when a
batch actually needs the word.

**General**: agent, harness, loop, skill, tool, session, compaction, kernel,
scheduler, inbox, subagent, prompt, token, plugin, Mechanism, Section, Phase,
Offline, then: context, provider, registry, pipeline, runtime, repo, diff, API,
key

**Added by the section batches**, all of them dsh or Cordis nouns bound to an
identifier in the source: model, log, turn, step, seam, service, surface, bus,
request, entry, job, run, child, parent, chunk, seq, fiber, effect, disposer,
adapter, schema, policy, profile, config, patch, handle, process, waterfall,
guard, hook, listener, payload, store, callable, bundle, UI, and the three
capability roles Definition, Provider, Consumer

Three of those were split across the first four batches and are now settled.
`bus` and `service` stay English because they name Cordis APIs, the event bus
behind `on` / `emit` and the `Service` a fiber provides. `thread` goes the other
way, into 執行緒 / 线程, because it is a plain OS word rather than one of the
repo's nouns. The rule: when a term is a dsh noun, keep it English; when it is
ordinary computing vocabulary with a settled Chinese term, translate it. That is
why `replay` is 重放 everywhere and `callable` stays English, the Python type
name the Model seam is written against.

**Repo terms defined in `CONTEXT.md`**: dsh, Mini-dsh, Carry-forward, Lens,
Opening, In-real-dsh, Failure modes, Offline check, Live demo, Studied version,
Re-pin, Layer map, Ceiling, Model seam, Scripted stand-in

**Names**: the four Phase names (Foundation, The Loop, Capabilities,
Composition) and the 14 Section titles, because they also name directories and
diagram nodes.

## 繁體 / 简体 term pairs

Applied when deriving zh-CN from zh-TW. Character conversion alone is not
enough.

| English | 繁體中文 | 简体中文 |
| --- | --- | --- |
| repository, project | 專案 | 项目 |
| code | 程式碼 | 代码 |
| source code | 原始碼 | 源代码 |
| file | 檔案 | 文件 |
| document | 文件 | 文档 |
| library | 函式庫 | 库 |
| package | 套件 | 包 |
| message | 訊息 | 消息 |
| list | 清單 | 列表 |
| link | 連結 | 链接 |
| text | 文字 | 文本 |
| call | 呼叫 | 调用 |
| load | 載入 | 加载 |
| parallel | 平行 | 并行 |
| implement | 實作 | 实现 |
| support | 支援 | 支持 |
| run, execute | 執行 | 运行 |
| default | 預設 | 默认 |
| network | 網路 | 网络 |
| protocol | 協定 | 协议 |
| background | 背景 | 后台 |
| interface | 介面 | 接口 |
| abstract base class | 抽象基底類別 | 抽象基类 |
| thread | 執行緒 | 线程 |
| exception (thrown) | 例外 | 异常 |
| exception (to a rule) | 例外 | 例外 |
| object | 物件 | 对象 |
| type | 型別 | 类型 |
| class | 類別 | 类 |
| module | 模組 | 模块 |
| function | 函式 | 函数 |
| constructor | 建構子 | 构造函数 |
| destructor | 解構子 | 析构函数 |
| data | 資料 | 数据 |
| cache | 快取 | 缓存 |
| queue | 佇列 | 队列 |
| streaming | 串流 | 流式输出 |
| user (the person) | 使用者 | 用户 |
| memory | 記憶體 | 内存 |
| byte | 位元組 | 字节 |
| character | 字元 | 字符 |
| field | 欄位 | 字段 |
| constant | 常數 | 常量 |
| enum | 列舉 | 枚举 |
| dispatch | 派送 | 分发 |
| create | 建立 | 创建 |
| disk | 磁碟 | 磁盘 |
| foreground | 前景 | 前台 |
| measure word for a script or check | 一支 | 一个 |
| measure word for a message or row | 一則 | 一条 |

Two traps in that table. 文件 means *document* in TW and *file* in CN, so a bare
character conversion silently flips its meaning. And quoted speech is 「」 in
TW, “” in CN.
