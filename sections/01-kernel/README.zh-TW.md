<!-- source: README.md @ 1371e92 -->

# 01 · Kernel

[English](README.md) | 繁體中文 | [简体中文](README.zh-CN.md)

> 每一次註冊，都等於把一個撤銷動作交給框架收著。卸載一個 plugin，就是把它的撤銷動作倒著跑一遍。

dsh 的口號是「一切都是 plugin」：tool、session 儲存、prompt 的段落，甚至整個
子系統，都會在 runtime 掛上去、再卸下來，像是切換 profile、熱重載、測試收尾、
關掉 subagent 的時候。

要讓這件事安全，最直覺的做法是訂一條約定：每個 plugin 都自己寫一個 `cleanup()`，
把當初註冊過的東西一個一個取消掉。

約定會走樣。某個 plugin 在新的程式碼路徑上加了一個監聽器，卻忘了補上對應的取消，
於是卸載它之後，就漏了一個回呼函式，永遠對著一個已經死掉的 plugin 觸發。

kernel 把責任歸屬翻了過來：註冊一樣東西，本身 *就是* 把撤銷動作交給框架。要做到
這件事，kernel 得先：

1. 讓每一次註冊（監聽器、service，什麼都算）都產出一個撤銷動作，也就是一個
   **disposer**。
2. 把 disposer 都收在擁有這個 plugin 的 **fiber** 上，這樣卸載就只是框架的一個
   動作：倒著跑一遍。
3. 每個 disposer 最多只跑一次，不管是誰先呼叫它。
4. 已經 dispose 掉的 fiber 不接受新的註冊：直接報錯，而不是默默漏掉。

---

## Mechanism

三個零件：

- **Fiber**：每掛上一個 plugin 就有一個 fiber，管的是這個 plugin 的生命週期。
  它就是一串照順序排好的撤銷動作，加上一個狀態（`loading → active → disposed`）。
- **`effect()`**：唯一的基本操作。你給它一個撤銷動作，fiber 就收下來，然後回給
  你一個只會生效一次的 disposer。
- **Context**：一個 plugin 眼中的整個應用程式。每一個註冊用的 API（`on`、
  `provide`）都建在 `effect()` 上，所以透過某個 plugin 的 context 做的註冊，都會
  落在那個 plugin 的 fiber 上。

一個 plugin 說穿了就是一個函式，收下自己的 context：

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

倒著跑很重要：一個 plugin 會先註冊地基，再註冊那些依賴地基的東西，所以拆的時候
必須先拆依賴的一方，才輪到地基，這跟解構子和 `defer` 堆疊要反過來收尾是同一個
道理。

### 改了什麼

跟 Section 00 比起來：

- `message.py` 和 `standin.py` 原封不動搬過來；跟 00 的 diff 就是這個 Section 的
  Mechanism，多的沒有。
- 新增 `kernel.py`：`Fiber`、`effect()`，還有一個 `Context`，它所有註冊用的 API
  都繞經 `effect()`。
- 目前還沒有東西在用這個 kernel。Section 02 的 session log 會當成一個 service 掛在
  上面。

---

## In real dsh

所有指過去的連結都固定在 Studied version
[`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca)
上。kernel 就是 Cordis，整份原始碼內嵌在 `vendor/` 底下，而且在本地打過 patch
（[`vendor/README.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/vendor/README.md)）。

| Mini-dsh | 真正的 dsh | 說明 |
| --- | --- | --- |
| `Fiber` | [`vendor/cordis/src/fiber.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/vendor/cordis/src/fiber.ts)：`Fiber`、`effect()` | 六個狀態（`PENDING, LOADING, ACTIVE, FAILED, DISPOSED, UNLOADING`），我們只有三個；那邊的 effect 還收 `Promise` 和 `(Async)Iterable` 這些形狀。 |
| `InactiveEffectError` | `fiber.ts` 裡的 `CordisError('INACTIVE_EFFECT')` | 在 `UNLOADING` 狀態下建立 effect 就會拋這個錯。 |
| `Context` | [`vendor/cordis/src/context.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/vendor/cordis/src/context.ts) | 它是一個包住自己的 `Proxy`，另外還有 `extend` / `isolate` / `intercept`，我們完全跳過。 |
| `on` / `emit` | [`vendor/cordis/src/events.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/vendor/cordis/src/events.ts) | 五種派送模式（`emit / parallel / serial / bail / waterfall`）；我們只做 `emit`，後面的 Section 會看 loop 需要什麼再補上其他模式。 |
| `provide` / `get` | [`vendor/cordis/src/reflect.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/vendor/cordis/src/reflect.ts)、[`service.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/vendor/cordis/src/service.ts) | `Service` 這個基底類別會在建構子裡透過 `ctx.reflect.provide` 自己註冊自己。 |
| `plugin(apply)` | [`vendor/cordis/src/registry.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/vendor/cordis/src/registry.ts) | plugin 有 Function / Constructor / Object 三種寫法，還能宣告 `inject`；我們只做 Function 那一種。 |

真正的 kernel 在這個 Section 的 Mechanism 之上，還多做了這些：

- **靠 `inject` 觸發的重載**：一個 fiber 會宣告自己需要哪些 service；當它注入的某個
  service 換了 provider，這個 fiber 就自動重載一次（`fiber.ts` 裡用 provider-uid 的
  世代編號做的）。這一整串連鎖反應之所以安全，靠的就是可以反向撤銷：重載說穿了
  就是先 dispose、再掛一次。
- **HMR 走的是同一條路**：熱模組替換（`vendor/hmr/`）就是把改動過的那個 plugin
  的 fiber dispose 掉，再重新掛一次。mini-dsh 不做（Ceiling）：它只是在這個
  Section 已經做好的 Mechanism 上面，再包一層盯著檔案變動的機制。
- 由 config 驅動的掛載：[`vendor/loader/src/config/entry.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/vendor/loader/src/config/entry.ts)
  把 config 裡的一筆設定變成一次掛載或卸載；Section 13 的 composition 層就站在它上面。

---

## Failure modes

- **沒走 `effect()` 的收尾，框架看不見。** 一個 plugin 如果直接去改全域狀態
  （開檔案、開執行緒），卻沒把撤銷動作包進 `ctx.effect()`，卸載的時候就會漏，
  而且框架連漏了什麼都看不到。這條紀律要嘛全做，要嘛等於沒做： *每一個* 副作用
  都得走 `effect()`。
- **有 disposer 拋錯，整段回收就停在那裡。** 一個壞掉的撤銷動作，會讓這個 fiber
  剩下的收尾全都做不完。mini-dsh 為了保持精簡就這樣接受了；真正的 Cordis 會把
  disposer 的錯誤隔開，這樣一個 plugin 的 bug 才卡不死另一個 plugin 的收尾。
- **撤銷動作依賴的東西，已經先被撤銷了。** 倒著跑只保護得了同一個 fiber 裡的
  依賴方，這裡沒有任何機制去排 *跨* fiber 的順序。真正的 dsh 在上面疊了 `inject`，
  讓依賴別人的 fiber 先卸載，之後才輪到provider 那一邊。
- **在收尾途中還在註冊。** 一個回呼函式如果在 dispose 做到一半時被觸發，又註冊了
  新的 effect，那個 effect 就會默默漏掉；所以已經 dispose 的 fiber 會直接拋
  `InactiveEffectError`，而不是把註冊收下來。
- **卸載之後還抓著那個 service 不放。** `ctx.get("echo")` 回來的是一個活的物件；
  呼叫端要是把它快取起來，就算提供它的 plugin 已經不在了，手上這個還是照用。
  真正的 dsh 用 proxy 和 `inject` 的把關，把這個時間窗口縮到很小；mini-dsh 只告訴你
  規則：要用的時候再去拿，永遠不要快取。

---

## 跑跑看

[`src/`](src/) 把 00 搬過來，再加上：

- [`kernel.py`](src/kernel.py)：`Fiber`、`effect()`，還有一個帶著 `plugin` /
  `on` / `emit` / `provide` / `get` 的 `Context`，每一次註冊都建在 `effect()` 上。
- [`test.py`](src/test.py)：掛上去再卸下來確實可以還原、收尾確實倒著跑、disposer
  只生效一次、對已經 dispose 的 fiber 註冊會報錯，還有鄰居之間互不干擾。

```bash
python sections/01-kernel/src/test.py   # offline checks, no key
```

這個 Section 完全不會呼叫 model，所以沒有 `demo.py`。

---

## 出處

- [`docs/cordis-primer.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/cordis-primer.md)：
  dsh 自己寫的 kernel 入門文。
- [`vendor/README.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/vendor/README.md)：
  內嵌了哪些東西的清單，還有 dsh 在本地對上游 Cordis 改動的 18 個地方。
- [cordiverse/cordis](https://github.com/cordiverse/cordis)：上游框架
  （dsh 固定在 `56b3d4f`）。
