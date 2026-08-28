<!-- source: README.md @ 3705bd7 -->

# 01 · Kernel

[English](README.md) | 繁體中文 | [简体中文](README.zh-CN.md)

> 工具、prompt 和整個子系統，都可能在 harness 執行期間掛載或卸載。如果每個 plugin 都要自己記得如何清理，只要漏掉一個步驟，就會留下失效的註冊。比較安全的做法，是在註冊時就把撤銷方式一起交給框架管理。

dsh 的核心概念是「一切都是 plugin」。工具、session 儲存、prompt 段落，甚至完整子系統，都會在執行期間動態掛載與卸載。切換 profile、熱重載、結束測試或關閉 subagent，都會走同一套生命週期。

一個直覺的方法，是要求每個 plugin 實作 `cleanup()`，並在卸載時逐一取消原本的註冊。

但這種約定很容易失效。例如，plugin 新增了一個 listener，卻忘了在 `cleanup()` 中移除。plugin 卸載後，callback 仍留在系統裡，下次事件發生時就可能存取已經失效的狀態。

kernel 改變了責任分工：每次註冊都必須同時提供對應的撤銷動作，再由框架統一管理。具體規則如下：

1. 每次註冊，無論是 listener 還是 service，都會產生一個撤銷函式，也就是 **disposer**。
2. 所有 disposer 都由該 plugin 的 **fiber** 管理。卸載時，框架只要以反向順序執行它們。
3. 每個 disposer 最多生效一次，因此提前撤銷也不會造成重複清理。
4. 已經 disposed 的 fiber 不得再接受新註冊；系統會直接報錯，避免資源無聲地洩漏。

---

## 核心機制

核心由三個元件組成：

- **Fiber**：每個掛載的 plugin 都對應一個 fiber，負責管理它的生命週期。fiber 包含一組有順序的撤銷動作，以及 `loading → active → disposed` 狀態。
- **`effect()`**：基礎註冊操作。它會把撤銷動作加入 fiber，並回傳只會生效一次的 disposer。
- **Context**：plugin 可見的應用程式上下文。`on` 和 `provide` 等註冊 API 都建立在 `effect()` 上，因此通過某個 plugin context 建立的註冊，都會自動歸屬到它的 fiber。

在 Mini-dsh 中，plugin 就是一個接收專屬 context 的函式：

```python
def echo_plugin(ctx):
    ctx.on("say", heard.append)                       # registration #1
    ctx.provide("echo", lambda t: f"echo: {t}")       # registration #2
    # no cleanup code: the undos are already on this plugin's fiber

fiber = ctx.plugin(echo_plugin)   # mount
fiber.dispose()                   # unmount: listener and service both gone
```

fiber 就是一份 disposer 清單，外加一道狀態關卡：

```python
def collect(self, dispose, label=""):
    if self.state == "disposed":
        raise InactiveEffectError(...)
    entry = {"dispose": dispose, "done": False}
    def run():
        if not entry["done"]:
            entry["done"] = True
            entry["dispose"]()
    self._disposers.append(run)
    return run          # single-shot: safe to call early, fiber won't re-run it

def dispose(self):
    self.state = "disposed"
    for run in reversed(self._disposers):
        run()
```

而每一個註冊用的 API 都只有三行：先把事情做掉，再把撤銷動作交給 `effect()`：

```python
def on(self, event, callback):
    listeners = self._root._listeners.setdefault(event, [])
    listeners.append(callback)
    return self.effect(lambda: listeners.remove(callback), f"on({event})")
```

這樣一來，掛載和卸載天生就是對稱的：

```text
mount:    plugin(apply) ──► new Fiber ──► apply(child ctx)
                                          each ctx.on / ctx.provide / ctx.effect
                                          pushes one undo onto the fiber
unmount:  fiber.dispose() ──► undos run in reverse ──► registrations gone
```

反向執行很重要：plugin 通常先註冊基礎資源，再註冊依賴它們的項目；卸載時必須先移除依賴者，最後才清理基礎資源。這與解構子或 `defer` 堆疊採用後進先出的原理相同。

### 改了什麼

與第 00 章相比：

- `message.py` 和 `standin.py` 完整沿用，因此 diff 只會顯示本章新增的 kernel 機制。
- 新增 `kernel.py`：`Fiber`、`effect()`，還有一個 `Context`，它所有註冊用的 API 都繞經 `effect()`。
- 目前還沒有東西在用這個 kernel。第 02 章的 session log 會當成一個 service 掛在上面。

---

## 對照真正的 dsh

以下連結皆指向研究版本 [`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca)。kernel 就是 Cordis，整份原始碼內嵌在 `vendor/` 底下，而且在本地打過 patch （[`vendor/README.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/vendor/README.md)）。

| Mini-dsh | 真正的 dsh | 說明 |
| --- | --- | --- |
| `Fiber` | [`vendor/cordis/src/fiber.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/vendor/cordis/src/fiber.ts)：`Fiber`、`effect()` | 六個狀態（`PENDING, LOADING, ACTIVE, FAILED, DISPOSED, UNLOADING`），我們只有三個；那邊的 effect 還收 `Promise` 和 `(Async)Iterable` 這些形狀。 |
| `InactiveEffectError` | `fiber.ts` 裡的 `CordisError('INACTIVE_EFFECT')` | 在 `UNLOADING` 狀態下建立 effect 就會拋這個錯。 |
| `Context` | [`vendor/cordis/src/context.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/vendor/cordis/src/context.ts) | 它是一個包住自己的 `Proxy`，另外還有 `extend` / `isolate` / `intercept`，我們完全跳過。 |
| `on` / `emit` | [`vendor/cordis/src/events.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/vendor/cordis/src/events.ts) | 五種派送模式（`emit / parallel / serial / bail / waterfall`）；我們只做 `emit`，後續章節會看 loop 需要什麼再補上其他模式。 |
| `provide` / `get` | [`vendor/cordis/src/reflect.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/vendor/cordis/src/reflect.ts), [`service.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/vendor/cordis/src/service.ts) | `Service` 這個基底類別會在建構子裡透過 `ctx.reflect.provide` 自己註冊自己。 |
| `plugin(apply)` | [`vendor/cordis/src/registry.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/vendor/cordis/src/registry.ts) | plugin 有 Function / Constructor / Object 三種寫法，還能宣告 `inject`；我們只做 Function 那一種。 |

真正的 kernel 還提供以下功能：

- **由 `inject` 觸發重載**：fiber 會宣告自己需要哪些 service；當其中一個 service 更換 provider 時，fiber 便自動重載（`fiber.ts` 透過 provider-uid 的世代編號判斷）。重載的本質是先 dispose 再重新掛載，因此可撤銷的註冊讓整個過程保持安全。
- **HMR 使用相同生命週期**：熱模組替換（`vendor/hmr/`）會 dispose 已變更 plugin 的 fiber，再重新掛載。Mini-dsh 不實作檔案監看與 HMR，但本章的生命週期已提供所需基礎。
- 由 config 驅動的掛載：[`vendor/loader/src/config/entry.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/vendor/loader/src/config/entry.ts) 把 config 裡的一筆設定變成一次掛載或卸載；第 13 章的 composition 層就站在它上面。

---

## 常見失敗模式

- **沒走 `effect()` 的收尾，框架看不見。** 一個 plugin 如果直接去改全域狀態（開檔案、開執行緒），卻沒把撤銷動作包進 `ctx.effect()`，卸載的時候就會漏，而且框架連漏了什麼都看不到。這條紀律要嘛全做，要嘛等於沒做： *每一個* 副作用都得走 `effect()`。
- **disposer 拋錯會中斷後續清理。** 單一撤銷動作失敗，就會讓同一個 fiber 剩餘的清理無法執行。Mini-dsh 為了保持精簡而接受這項限制；真正的 Cordis 會隔離 disposer 錯誤，避免一個 plugin 的問題影響其他清理工作。
- **跨 fiber 的依賴可能以錯誤順序撤銷。** 反向執行只能保護同一個 fiber 內的註冊順序，無法安排不同 fiber 的卸載先後。真正的 dsh 透過 `inject` 追蹤依賴，先卸載使用 service 的 fiber，再卸載提供 service 的一方。
- **在收尾途中還在註冊。** 一個回呼函式如果在 dispose 做到一半時被觸發，又註冊了新的 effect，那個 effect 就會默默漏掉；所以已經 dispose 的 fiber 會直接拋 `InactiveEffectError`，而不是把註冊收下來。
- **卸載後仍持有舊 service。** `ctx.get("echo")` 會直接回傳目前的物件；若呼叫端長期快取，即使提供它的 plugin 已卸載，舊參考仍然可被使用。真正的 dsh 透過 proxy 和 `inject` 縮短這個風險窗口；Mini-dsh 的規則則是需要時再取得，不要自行快取。

---

## 動手驗證

[`src/`](src/) 延續第 00 章，並加入：

- [`kernel.py`](src/kernel.py)：`Fiber`、`effect()`，還有一個帶著 `plugin` / `on` / `emit` / `provide` / `get` 的 `Context`，每一次註冊都建在 `effect()` 上。
- [`test.py`](src/test.py)：掛上去再卸下來確實可以還原、收尾確實倒著跑、disposer 只生效一次、對已經 dispose 的 fiber 註冊會報錯，還有鄰居之間互不干擾。

```bash
python sections/01-kernel/src/test.py   # offline checks, no key
```

本章不會呼叫模型，因此沒有 `demo.py`。

---

## 參考資料

- [`docs/cordis-primer.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/cordis-primer.md)： dsh 自己寫的 kernel 入門文。
- [`vendor/README.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/vendor/README.md)：內嵌了哪些東西的清單，還有 dsh 在本地對上游 Cordis 改動的 18 個地方。
- [cordiverse/cordis](https://github.com/cordiverse/cordis)：上游框架（dsh 固定在 `56b3d4f`）。
