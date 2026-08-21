<!-- source: README.md @ 1371e92 -->

# 01 · Kernel

[English](README.md) | [繁體中文](README.zh-TW.md) | 简体中文

> 每一次注册，都等于把一个撤销动作交给框架收着。卸载一个 plugin，就是把它的撤销动作倒着跑一遍。

dsh 的口号是“一切都是 plugin”：tool、session 存储、prompt 的段落，甚至整个
子系统，都会在 runtime 挂上去、再卸下来，像是切换 profile、热重载、测试收尾、
关掉 subagent 的时候。

要让这件事安全，最直觉的做法是订一条约定：每个 plugin 都自己写一个 `cleanup()`，
把当初注册过的东西一个一个取消掉。

约定会走样。某个 plugin 在新的代码路径上加了一个监听器，却忘了补上对应的取消，
于是卸载它之后，就漏了一个回调函数，永远对着一个已经死掉的 plugin 触发。

kernel 把责任归属翻了过来：注册一样东西，本身 *就是* 把撤销动作交给框架。要做到
这件事，kernel 得先：

1. 让每一次注册（监听器、service，什么都算）都产出一个撤销动作，也就是一个
   **disposer**。
2. 把 disposer 都收在拥有这个 plugin 的 **fiber** 上，这样卸载就只是框架的一个
   动作：倒着跑一遍。
3. 每个 disposer 最多只跑一次，不管是谁先调用它。
4. 已经 dispose 掉的 fiber 不接受新的注册：直接报错，而不是默默漏掉。

---

## Mechanism

三个零件：

- **Fiber**：每挂上一个 plugin 就有一个 fiber，管的是这个 plugin 的生命周期。
  它就是一串照顺序排好的撤销动作，加上一个状态（`loading → active → disposed`）。
- **`effect()`**：唯一的基本操作。你给它一个撤销动作，fiber 就收下来，然后回给
  你一个只会生效一次的 disposer。
- **Context**：一个 plugin 眼中的整个应用程序。每一个注册用的 API（`on`、
  `provide`）都建在 `effect()` 上，所以通过某个 plugin 的 context 做的注册，都会
  落在那个 plugin 的 fiber 上。

一个 plugin 说穿了就是一个函数，收下自己的 context：

```python
def echo_plugin(ctx):
    ctx.on("say", heard.append)                       # registration #1
    ctx.provide("echo", lambda t: f"echo: {t}")       # registration #2
    # no cleanup code: the undos are already on this plugin's fiber

fiber = ctx.plugin(echo_plugin)   # mount
fiber.dispose()                   # unmount: listener and service both gone
```

fiber 就是一份 disposer 列表，外加一道状态关卡：

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

而每一个注册用的 API 都只有三行：先把事情做掉，再把撤销动作交给 `effect()`：

```python
def on(self, event, callback):
    listeners = self._root._listeners.setdefault(event, [])
    listeners.append(callback)
    return self.effect(lambda: listeners.remove(callback), f"on({event})")
```

这样一来，挂载和卸载天生就是对称的：

```text
mount:    plugin(apply) ──► new Fiber ──► apply(child ctx)
                                          each ctx.on / ctx.provide / ctx.effect
                                          pushes one undo onto the fiber
unmount:  fiber.dispose() ──► undos run in reverse ──► registrations gone
```

倒着跑很重要：一个 plugin 会先注册地基，再注册那些依赖地基的东西，所以拆的时候
必须先拆依赖的一方，才轮到地基，这跟析构函数和 `defer` 栈要反过来收尾是同一个
道理。

### 改了什么

跟 Section 00 比起来：

- `message.py` 和 `standin.py` 原封不动搬过来；跟 00 的 diff 就是这个 Section 的
  Mechanism，多的没有。
- 新增 `kernel.py`：`Fiber`、`effect()`，还有一个 `Context`，它所有注册用的 API
  都绕经 `effect()`。
- 目前还没有东西在用这个 kernel。Section 02 的 session log 会当成一个 service 挂在
  上面。

---

## In real dsh

所有指过去的链接都固定在 Studied version
[`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca)
上。kernel 就是 Cordis，整份源代码内嵌在 `vendor/` 底下，而且在本地打过 patch
（[`vendor/README.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/vendor/README.md)）。

| Mini-dsh | 真正的 dsh | 说明 |
| --- | --- | --- |
| `Fiber` | [`vendor/cordis/src/fiber.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/vendor/cordis/src/fiber.ts)：`Fiber`、`effect()` | 六个状态（`PENDING, LOADING, ACTIVE, FAILED, DISPOSED, UNLOADING`），我们只有三个；那边的 effect 还收 `Promise` 和 `(Async)Iterable` 这些形状。 |
| `InactiveEffectError` | `fiber.ts` 里的 `CordisError('INACTIVE_EFFECT')` | 在 `UNLOADING` 状态下创建 effect 就会抛这个错。 |
| `Context` | [`vendor/cordis/src/context.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/vendor/cordis/src/context.ts) | 它是一个包住自己的 `Proxy`，另外还有 `extend` / `isolate` / `intercept`，我们完全跳过。 |
| `on` / `emit` | [`vendor/cordis/src/events.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/vendor/cordis/src/events.ts) | 五种分发模式（`emit / parallel / serial / bail / waterfall`）；我们只做 `emit`，后面的 Section 会看 loop 需要什么再补上其他模式。 |
| `provide` / `get` | [`vendor/cordis/src/reflect.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/vendor/cordis/src/reflect.ts)、[`service.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/vendor/cordis/src/service.ts) | `Service` 这个基类会在构造函数里通过 `ctx.reflect.provide` 自己注册自己。 |
| `plugin(apply)` | [`vendor/cordis/src/registry.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/vendor/cordis/src/registry.ts) | plugin 有 Function / Constructor / Object 三种写法，还能声明 `inject`；我们只做 Function 那一种。 |

真正的 kernel 在这个 Section 的 Mechanism 之上，还多做了这些：

- **靠 `inject` 触发的重载**：一个 fiber 会声明自己需要哪些 service；当它注入的某个
  service 换了 provider，这个 fiber 就自动重载一次（`fiber.ts` 里用 provider-uid 的
  世代编号做的）。这一整串连锁反应之所以安全，靠的就是可以反向撤销：重载说穿了
  就是先 dispose、再挂一次。
- **HMR 走的是同一条路**：热模块替换（`vendor/hmr/`）就是把改动过的那个 plugin
  的 fiber dispose 掉，再重新挂一次。mini-dsh 不做（Ceiling）：它只是在这个
  Section 已经做好的 Mechanism 上面，再包一层盯着文件变动的机制。
- 由 config 驱动的挂载：[`vendor/loader/src/config/entry.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/vendor/loader/src/config/entry.ts)
  把 config 里的一条设置变成一次挂载或卸载；Section 13 的 composition 层就站在它上面。

---

## Failure modes

- **没走 `effect()` 的收尾，框架看不见。** 一个 plugin 如果直接去改全局状态
  （开文件、开线程），却没把撤销动作包进 `ctx.effect()`，卸载的时候就会漏，
  而且框架连漏了什么都看不到。这条纪律要么全做，要么等于没做： *每一个* 副作用
  都得走 `effect()`。
- **有 disposer 抛错，整段回收就停在那里。** 一个坏掉的撤销动作，会让这个 fiber
  剩下的收尾全都做不完。mini-dsh 为了保持精简就这样接受了；真正的 Cordis 会把
  disposer 的错误隔开，这样一个 plugin 的 bug 才卡不死另一个 plugin 的收尾。
- **撤销动作依赖的东西，已经先被撤销了。** 倒着跑只保护得了同一个 fiber 里的
  依赖方，这里没有任何机制去排 *跨* fiber 的顺序。真正的 dsh 在上面叠了 `inject`，
  让依赖别人的 fiber 先卸载，之后才轮到provider 那一边。
- **在收尾途中还在注册。** 一个回调函数如果在 dispose 做到一半时被触发，又注册了
  新的 effect，那个 effect 就会默默漏掉；所以已经 dispose 的 fiber 会直接抛
  `InactiveEffectError`，而不是把注册收下来。
- **卸载之后还抓着那个 service 不放。** `ctx.get("echo")` 回来的是一个活的对象；
  调用端要是把它缓存起来，就算提供它的 plugin 已经不在了，手上这个还是照用。
  真正的 dsh 用 proxy 和 `inject` 的把关，把这个时间窗口缩到很小；mini-dsh 只告诉你
  规则：要用的时候再去拿，永远不要缓存。

---

## 跑跑看

[`src/`](src/) 把 00 搬过来，再加上：

- [`kernel.py`](src/kernel.py)：`Fiber`、`effect()`，还有一个带着 `plugin` /
  `on` / `emit` / `provide` / `get` 的 `Context`，每一次注册都建在 `effect()` 上。
- [`test.py`](src/test.py)：挂上去再卸下来确实可以还原、收尾确实倒着跑、disposer
  只生效一次、对已经 dispose 的 fiber 注册会报错，还有邻居之间互不干扰。

```bash
python sections/01-kernel/src/test.py   # offline checks, no key
```

这个 Section 完全不会调用 model，所以没有 `demo.py`。

---

## 出处

- [`docs/cordis-primer.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/cordis-primer.md)：
  dsh 自己写的 kernel 入门文。
- [`vendor/README.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/vendor/README.md)：
  内嵌了哪些东西的列表，还有 dsh 在本地对上游 Cordis 改动的 18 个地方。
- [cordiverse/cordis](https://github.com/cordiverse/cordis)：上游框架
  （dsh 固定在 `56b3d4f`）。
