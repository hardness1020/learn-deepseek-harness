<!-- source: README.md @ 3705bd7 -->

# 01 · Kernel

[English](README.md) | [繁體中文](README.zh-TW.md) | 简体中文

> 工具、prompt 和整个子系统，都可能在 harness 运行期间挂载或卸载。如果每个 plugin 都要自己记得如何清理，只要漏掉一个步骤，就会留下失效的注册。比较安全的做法，是在注册时就把撤销方式一起交给框架管理。

dsh 的内核概念是「一切都是 plugin」。工具、session 保存、prompt 段落，甚至完整子系统，都会在运行期间动态挂载与卸载。切换 profile、热重载、结束测试或关闭 subagent，都会走同一套生命周期。

一个直觉的方法，是要求每个 plugin 实现 `cleanup()`，并在卸载时逐一取消原本的注册。

但这种约定很容易失效。例如，plugin 添加了一个 listener，却忘了在 `cleanup()` 中移除。plugin 卸载后，callback 仍留在系统里，下次事件发生时就可能访问已经失效的状态。

kernel 改变了责任分工：每次注册都必须同时提供对应的撤销动作，再由框架统一管理。具体规则如下：

1. 每次注册，无论是 listener 还是 service，都会产生一个撤销函数，也就是 **disposer**。
2. 所有 disposer 都由该 plugin 的 **fiber** 管理。卸载时，框架只要以反向顺序运行它们。
3. 每个 disposer 最多生效一次，因此提前撤销也不会造成重复清理。
4. 已经 disposed 的 fiber 不得再接受新注册；系统会直接报错，避免资源无声地泄漏。

---

## 内核机制

内核由三个组件组成：

- **Fiber**：每个挂载的 plugin 都对应一个 fiber，负责管理它的生命周期。fiber 包含一组有顺序的撤销动作，以及 `loading → active → disposed` 状态。
- **`effect()`**：基础注册操作。它会把撤销动作加入 fiber，并返回只会生效一次的 disposer。
- **Context**：plugin 可见的应用程序上下文。`on` 和 `provide` 等注册 API 都创建在 `effect()` 上，因此通过某个 plugin context 创建的注册，都会自动归属到它的 fiber。

在 Mini-dsh 中，plugin 就是一个接收专属 context 的函数：

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

反向运行很重要：plugin 通常先注册基础资源，再注册依赖它们的项目；卸载时必须先移除依赖者，最后才清理基础资源。这与解构子或 `defer` 堆栈采用后进先出的原理相同。

### 改了什么

与第 00 章相比：

- `message.py` 和 `standin.py` 完整沿用，因此 diff 只会显示本章添加的 kernel 机制。
- 添加 `kernel.py`：`Fiber`、`effect()`，还有一个 `Context`，它所有注册用的 API 都绕经 `effect()`。
- 目前还没有东西在用这个 kernel。第 02 章的 session log 会当成一个 service 挂在上面。

---

## 对照真正的 dsh

以下链接皆指向研究版本 [`99f6f02`](https://github.com/deepseek-ai/deepseek-harness/tree/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca)。kernel 就是 Cordis，整份源代码内嵌在 `vendor/` 底下，而且在本地打过 patch （[`vendor/README.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/vendor/README.md)）。

| Mini-dsh | 真正的 dsh | 说明 |
| --- | --- | --- |
| `Fiber` | [`vendor/cordis/src/fiber.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/vendor/cordis/src/fiber.ts)：`Fiber`、`effect()` | 六个状态（`PENDING, LOADING, ACTIVE, FAILED, DISPOSED, UNLOADING`），我们只有三个；那边的 effect 还收 `Promise` 和 `(Async)Iterable` 这些形状。 |
| `InactiveEffectError` | `fiber.ts` 里的 `CordisError('INACTIVE_EFFECT')` | 在 `UNLOADING` 状态下创建 effect 就会抛这个错。 |
| `Context` | [`vendor/cordis/src/context.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/vendor/cordis/src/context.ts) | 它是一个包住自己的 `Proxy`，另外还有 `extend` / `isolate` / `intercept`，我们完全跳过。 |
| `on` / `emit` | [`vendor/cordis/src/events.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/vendor/cordis/src/events.ts) | 五种派送模式（`emit / parallel / serial / bail / waterfall`）；我们只做 `emit`，后续章节会看 loop 需要什么再补上其他模式。 |
| `provide` / `get` | [`vendor/cordis/src/reflect.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/vendor/cordis/src/reflect.ts), [`service.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/vendor/cordis/src/service.ts) | `Service` 这个基类会在建构子里通过 `ctx.reflect.provide` 自己注册自己。 |
| `plugin(apply)` | [`vendor/cordis/src/registry.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/vendor/cordis/src/registry.ts) | plugin 有 Function / Constructor / Object 三种写法，还能声明 `inject`；我们只做 Function 那一种。 |

真正的 kernel 还提供以下功能：

- **由 `inject` 触发重载**：fiber 会声明自己需要哪些 service；当其中一个 service 更换 provider 时，fiber 便自动重载（`fiber.ts` 通过 provider-uid 的世代编号判断）。重载的本质是先 dispose 再重新挂载，因此可撤销的注册让整个过程保持安全。
- **HMR 使用相同生命周期**：热模块替换（`vendor/hmr/`）会 dispose 已变更 plugin 的 fiber，再重新挂载。Mini-dsh 不实现文件监看与 HMR，但本章的生命周期已提供所需基础。
- 由 config 驱动的挂载：[`vendor/loader/src/config/entry.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/vendor/loader/src/config/entry.ts) 把 config 里的一笔设置变成一次挂载或卸载；第 13 章的 composition 层就站在它上面。

---

## 常见失败模式

- **没走 `effect()` 的收尾，框架看不见。** 一个 plugin 如果直接去改全域状态（开文件、开线程），却没把撤销动作包进 `ctx.effect()`，卸载的时候就会漏，而且框架连漏了什么都看不到。这条纪律要嘛全做，要嘛等于没做： *每一个* 副作用都得走 `effect()`。
- **disposer 抛错会中断后续清理。** 单一撤销动作失败，就会让同一个 fiber 剩余的清理无法运行。Mini-dsh 为了保持精简而接受这项限制；真正的 Cordis 会隔离 disposer 错误，避免一个 plugin 的问题影响其他清理工作。
- **跨 fiber 的依赖可能以错误顺序撤销。** 反向运行只能保护同一个 fiber 内的注册顺序，无法安排不同 fiber 的卸载先后。真正的 dsh 通过 `inject` 追踪依赖，先卸载使用 service 的 fiber，再卸载提供 service 的一方。
- **在收尾途中还在注册。** 一个回呼函数如果在 dispose 做到一半时被触发，又注册了新的 effect，那个 effect 就会默默漏掉；所以已经 dispose 的 fiber 会直接抛 `InactiveEffectError`，而不是把注册收下来。
- **卸载后仍持有旧 service。** `ctx.get("echo")` 会直接返回目前的对象；若调用端长期缓存，即使提供它的 plugin 已卸载，旧参考仍然可被使用。真正的 dsh 通过 proxy 和 `inject` 缩短这个风险窗口；Mini-dsh 的规则则是需要时再取得，不要自行缓存。

---

## 动手验证

[`src/`](src/) 延续第 00 章，并加入：

- [`kernel.py`](src/kernel.py)：`Fiber`、`effect()`，还有一个带着 `plugin` / `on` / `emit` / `provide` / `get` 的 `Context`，每一次注册都建在 `effect()` 上。
- [`test.py`](src/test.py)：挂上去再卸下来确实可以还原、收尾确实倒着跑、disposer 只生效一次、对已经 dispose 的 fiber 注册会报错，还有邻居之间互不干扰。

```bash
python sections/01-kernel/src/test.py   # offline checks, no key
```

本章不会调用模型，因此没有 `demo.py`。

---

## 参考资料

- [`docs/cordis-primer.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/docs/cordis-primer.md)： dsh 自己写的 kernel 入门文。
- [`vendor/README.md`](https://github.com/deepseek-ai/deepseek-harness/blob/99f6f02fecdb7dff40c3fbc9470f5907c29f74ca/vendor/README.md)：内嵌了哪些东西的列表，还有 dsh 在本地对上游 Cordis 改动的 18 个地方。
- [cordiverse/cordis](https://github.com/cordiverse/cordis)：上游框架（dsh 固定在 `56b3d4f`）。
