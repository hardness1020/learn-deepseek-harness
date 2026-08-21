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
has one. A section README gets its line in the same batch that translates it,
never before, because the links would point at files that do not exist yet.
Right now only the root README qualifies.

Every zh file opens with an HTML comment naming the English commit it tracks:

```text
<!-- source: README.md @ 75b51d8 -->
```

The sha names the commit whose English prose the translation covers. Adding or
fixing the switcher line in that same commit does not bump it; a prose change
does.

`zh-CN` is derived from `zh-TW`: character conversion plus the regional term
swaps below. Never a bare character conversion.

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
| a real harness | 一套真的 harness | 一套貨真價實的 harness |
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

Two traps in that table. 文件 means *document* in TW and *file* in CN, so a bare
character conversion silently flips its meaning. And quoted speech is 「」 in
TW, “” in CN.
