# i18n glossary

How this repo's Chinese translations are written. Read this before starting or
reviewing a translation batch.

## Files and switcher

Translations are sibling files next to each English `README.md`:
`README.zh-TW.md` (繁體中文) and `README.zh-CN.md` (简体中文).

Every README carries a language switcher line, English ones included. The
current language is plain text, the other two are links:

```text
English | [繁體中文](README.zh-TW.md) | [简体中文](README.zh-CN.md)
```

It sits at the top of the file, inside the centered header block when the file
has one.

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

**General**: agent, harness, loop, skill, tool, session, compaction, kernel,
scheduler, inbox, subagent, prompt, token, plugin, context, provider, registry,
pipeline, runtime, repo, diff, commit, API, key

**Repo terms defined in [`CONTEXT.md`](../CONTEXT.md)**: dsh, Mini-dsh, Section,
Mechanism, Phase, Carry-forward, Lens, Opening, In-real-dsh, Failure modes,
Offline check, Live demo, Studied version, Re-pin, Layer map, Ceiling, Model
seam, Scripted stand-in

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
