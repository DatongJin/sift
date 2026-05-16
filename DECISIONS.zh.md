# Sift — 开发决策记录（中英对照）

本文档记录 Sift 开发过程中每个关键决策点：当时摆在桌面上的**选项 (options)**、
最终的**选择 (decision)**、以及**原因 (rationale)**。

> 说明：Sift 是一个**纯本地桌面应用 (local-only desktop application)**，
> **故意不使用云服务器 (cloud server)**。"为什么不做云应用"本身就是下面的
> 决策 #1。

---

## 1. 产品形态 (Product form factor)

**问题：** Sift 应该是个什么形态的程序？

**考虑过的选项：**

| 选项 | 优点 | 缺点 |
| --- | --- | --- |
| 云端网页应用 (cloud web app)，用户上传文件 | 任何浏览器能用、无需安装 | 用户得把几十 GB 私人照片上传到陌生服务器——隐私灾难、带宽成本高、违背产品本意 |
| 纯浏览器应用 (browser app)，用 File System Access API（文件系统访问接口） | 免安装 | 只能在 Chromium 桌面浏览器用；每次访问都要重新授权文件夹；得用 JS 重写 Pillow/SQLite 或打包 30 MB 的 Pyodide |
| 纯命令行 (CLI, 命令行界面) | 最容易做 | 非技术用户用不了 |
| Tkinter 桌面 GUI（图形界面） | 纯标准库、无依赖 | 界面老土难看，难做现代化 |
| Electron 应用 | 界面现代、跨平台 | 二进制 100+ MB、工具链笨重、杀鸡用牛刀 |
| **本地 HTTP 服务器 (local HTTP server) + 浏览器 UI** | HTML/CSS 现代界面、浏览器当渲染引擎、零前端依赖、文件不出本机 | 架构略特殊、需处理生命周期 |

**决策：** 用 Python 标准库的 `http.server` 起一个绑定 `127.0.0.1`（本机回环地址）
的本地服务器，用浏览器窗口当界面。

**原因：** 不用前端框架就能有现代界面、所有文件处理都在本地（隐私
privacy）、复用浏览器这个免费又强大的渲染引擎 (rendering engine)。云端方案和
纯浏览器方案被否决，因为 Sift 的核心工作——递归遍历用户硬盘 (recursively walk
the disk)——正是浏览器故意禁止、用户也绝不会上传的。

---

## 2. 重复文件检测策略 (Duplicate detection strategy)

**问题：** 怎么判定两个文件是重复的？

**考虑过的选项：**

- 文件名匹配 (filename match)——快但错（同名 ≠ 同内容；不同名可能完全相同）
- 全文件哈希 (full hash)——正确但每个文件每个字节都要读
- 先按大小分组再哈希 (size grouping then hash)——更好
- **三层过滤 (three-tier filter)：大小 → 前 8 KB 部分哈希 (partial hash) → 完整 SHA-256**

**决策：** 三层过滤。

**原因：** 大小不同的文件不可能重复（用 `stat` 过滤，几乎免费）。同大小的
文件里，8 KB 部分哈希能极便宜地排除掉绝大多数。只有两层都过的才做完整哈希。
真实数据上这跳过了朴素全哈希 (naive full-hash) 95%+ 的 I/O（磁盘读写）。

---

## 3. 哈希函数 (Hash function)

**考虑过的选项：** MD5、SHA-1、SHA-256、BLAKE3、xxHash。

**决策：** SHA-256。

**原因：** Python 标准库自带（无依赖），抗碰撞 (collision-resistant，MD5/SHA-1
已不安全），而且它不是瓶颈——整个过程是 I/O 受限 (I/O-bound) 的，换更快的
BLAKE3/xxHash 不会改变实际耗时，反而引入依赖。

---

## 4. 相似照片检测算法 (Similar-photo detection algorithm)

**问题：** 怎么找到"看起来一样但不是逐字节相同"的照片（连拍 burst shots、
重压缩副本 recompressed copies、轻微裁剪 slight crops）？

**考虑过的选项：**

| 选项 | 说明 |
| --- | --- |
| aHash（平均哈希 average hash） | 对亮度变化太敏感 |
| pHash（基于 DCT 离散余弦变换的哈希） | 准但慢，要做一次 DCT |
| **dHash（差分哈希 difference hash）** | 快、抗缩放/重压缩、实现简单、无重依赖 |
| 深度学习嵌入 (deep-learning embeddings) | 最准但要模型（数百 MB）+ 最好有 GPU |
| SSIM（结构相似性 structural similarity） | 全图两两比较，O(n²)，太慢 |

**决策：** dHash——缩到 9×8 灰度 (grayscale)，比较相邻像素生成 64 位指纹
(fingerprint)；两图的指纹**汉明距离 (Hamming distance)** ≤ 阈值（默认 5）就算
相似。

**原因：** 速度、鲁棒性 (robustness)、简洁三者的最佳平衡。不下载模型、不做
重数学，就能覆盖真实场景（同一张照片、不同压缩/尺寸/裁剪）。

---

## 5. 图像库 (Image library)

**考虑过的选项：** Pillow、OpenCV、Wand (ImageMagick)、纯标准库。

**决策：** Pillow，作为**可选依赖 (optional dependency)**。

**原因：** Pillow 是 Python 事实标准图像库，Anaconda 自带。OpenCV/Wand 更重。
设成可选：重复/空文件夹/最大文件三个模式零依赖工作，只有相似照片模式需要
Pillow，缺了 UI 也会优雅降级 (degrade gracefully)。

---

## 6. 照片扫描性能 (Photo-scan performance)

**问题：** 1700 张单反 JPEG（约 85 GB）的文件夹要扫 ~15 分钟，太慢。

**考虑过的选项：** 朴素完整解码、多进程 (multiprocessing)、GPU、libjpeg draft
模式（草图模式）、线程池 (thread pool)、持久缓存 (persistent cache)。

**决策：** 三个优化叠加：

1. `Image.draft()`——让 libjpeg 在 DCT 域用 1/4 或 1/8 倍率解码大 JPEG
   （快 ~10×，dHash 精度零损失——实测 0 bit 差异）。
2. 单次解码 (single decode)——dHash、尺寸、缩略图都从一次 `Image.open()` 派生，
   而不是开三次。
3. `ThreadPoolExecutor`（线程池执行器）8 个 worker——Pillow 解码时释放 GIL
   （全局解释器锁 Global Interpreter Lock），所以多线程能近线性扩展。

**原因：** 合计 ~30× 提速（同一文件夹 15 分钟 → ~30 秒），无新依赖、无精度
损失。GPU 和多进程被否决，比需要的重。

---

## 7. 删除安全模型 (Deletion safety model)

**问题：** 用户删文件时，实际发生什么？

**考虑过的选项：**

- 硬删除 (hard delete, `os.unlink`)——快、不可逆、吓人
- 操作系统回收站 (OS recycle bin)——可恢复但要平台特定代码、有容量上限
- **隔离区文件夹 (quarantine folder) + JSON 清单 (manifest) + 恢复 (restore)**

**决策：** 把文件移到带时间戳的 `.dupfinder_quarantine/` 文件夹，写一份 JSON
清单，提供一键恢复。永久删除 (permanent delete) 是选择性开启 (opt-in) 的。

**原因：** 完全可逆、透明（清单是人类可读的 human-readable）、跨平台（无需
OS 特定的回收站 API）、保留原始目录结构所以恢复是精确的。

---

## 8. Web 安全 / CSRF（跨站请求伪造 Cross-Site Request Forgery）

**问题：** 服务器监听 `127.0.0.1`；任何浏览器标签页的任何网页都能向它发
POST。恶意页面可能触发文件删除。

**考虑过的选项：** 什么都不做、基于令牌的认证 (token-based auth)、
Origin/Host/Content-Type 头检查。

**决策：** 拒绝 POST，除非：`Host` 头是回环地址（挡 DNS 重绑定 DNS-rebinding）、
`Origin` 头（如果有）匹配自己的 URL、`Content-Type` 是 `application/json`
（强制触发我们从不响应的 CORS 预检 preflight，从而阻断跨域 fetch）。

**原因：** 三个头检查就消除了 CSRF 攻击面 (attack surface)，对用户零摩擦
（无需登录）。令牌认证被否决——对本地单用户工具是不必要的摩擦。

---

## 9. 取消正在进行的扫描 (Cancelling a running scan)

**考虑过的选项：** 不支持取消、杀进程、用 `threading.Event`（线程事件）信号
穿过扫描函数。

**决策：** 一个 `threading.Event` 传入 `iter_files`、`hash_file`、
`find_duplicates`、`find_empty_folders`、`compute_top_largest`，在每个文件
之前和分块哈希读取循环 (chunked hash-read loop) 内部检查。

**原因：** 优雅且响应快（即使大文件读到一半也能 ~50 ms 中止），不杀进程、
不留下不一致状态。

---

## 10. HTTP 上处理长时间扫描 (Long scans over HTTP)

**问题：** 15 分钟的扫描占着一个 HTTP 请求不放；操作系统断掉空闲 socket 后
浏览器报 "Failed to fetch"（获取失败）。

**考虑过的选项：** 同步请求 (synchronous request)、WebSocket、后台任务 +
状态轮询 (background job + status polling)。

**决策：** `/api/scan_*_start` 创建任务并立即返回；工作在后台线程跑；UI 每
~450 ms 轮询 `/api/scan_status` 拿进度和最终结果。

**原因：** 能扛任意长的扫描、有实时进度条、复用已有的取消机制。WebSocket
被否决——对这个需求来说，比轮询循环 (poll loop) 多了不必要的机制。

---

## 11. 哈希缓存 (Hash cache)

**考虑过的选项：** 不缓存、仅内存、pickle 文件、SQLite。

**决策：** SQLite（`.dupfinder_cache.sqlite3`），用 `路径 + 大小 + 修改时间
mtime + 哈希模式` 作为复合键 (composite key)。

**原因：** 跨运行持久化，重扫同一文件夹近乎瞬时；复合键在文件变化时自动
失效 (auto-invalidate)；SQLite 是标准库自带。

---

## 12. 打包与分发 (Packaging and distribution)

**考虑过的选项：** 只发源码、pip/PyPI 包、PyInstaller `--onefile`（单文件）、
PyInstaller `--onedir`（单目录）、Nuitka、cx_Freeze。

**决策：** PyInstaller `--onefile` → 单个 `sift.exe`，再用
`sift-win64.zip` 包一层，附 README。

**原因：** 一个文件、目标机器无需 Python、双击即跑。zip 外壳符合用户对
"便携 Windows 工具 (portable Windows tool)"的预期。pip 被否决——要求用户
已经装了 Python。

---

## 13. 缩小打包体积 (Shrinking the bundle)

**问题：** 第一次 PyInstaller 构建 **235 MB**，因为 Anaconda 把
pandas/numpy/scipy/matplotlib 等全拖进来了。

**考虑过的选项：** 发 235 MB、用 `--exclude-module`（排除模块）列表、从干净
虚拟环境 (virtualenv) 构建。

**决策：** 一个明确的 `--exclude-module` 列表（numpy、pandas、scipy、
matplotlib、IPython、jupyter、sklearn……）。

**原因：** 改一处构建脚本就把体积从 235 MB 降到 **16 MB**（小了 93%），
不用搞干净环境。

---

## 14. 应用生命周期 / 关窗行为 (Application lifecycle)

**问题：** exe 起一个服务器、开一个分离的 (detached) 浏览器窗口；关窗后
后台残留一个幽灵服务器 (phantom server)。

**考虑过的选项：** 分离服务器、系统托盘 (system-tray) 应用、窗口绑定生命周期
(window-tied lifecycle)。

**决策：** 用专属的 `--user-data-dir`（用户数据目录，放在 temp 临时目录）
启动 Edge/Chrome `--app`，追踪那个进程，用户关窗时退出（关服务器 + 清理
临时配置目录）。

**原因：** 符合桌面应用预期（"关窗 = 退出"）。专属 `--user-data-dir` 是关键
——没有它，机器上已经开着 Edge 时 `--app=URL` 只会把 URL 丢给现有 Edge
进程，我们的进程句柄 (process handle) 立即返回，会误以为窗口已经关了。

---

## 15. 浏览器启动器 (Browser launcher)

**考虑过的选项：** `webbrowser.open`（默认浏览器）、Edge `--app`、Chrome
`--app`、pywebview（内嵌 WebView2）、Electron。

**决策：** Edge 或 Chrome 的 `--app` 模式，找不到就退回默认浏览器。

**原因：** `--app` 模式给一个无边框 (chromeless)、像原生应用的窗口，无额外
依赖，且 Windows 10/11 必带 Edge。pywebview/Electron 被否决——在这里是
无谓的额外重量。

---

## 16. 原生文件夹选择框 (Native folder picker)

**问题：** UI 需要真正的"选择文件夹"对话框。浏览器不暴露绝对路径（隐私），
所以要原生的。

**考虑过的选项：** HTML file input、浏览器 File System Access API、
`subprocess`（子进程）+ tkinter、线程里直接调 tkinter、Win32 ctypes COM 对话框。

**决策：** 在守护线程 (daemon thread) 里直接调 tkinter 的 `filedialog`，
用一个锁 (lock) 保护防止两次点击竞争 (race)。

**原因：** 原设计是开子进程（`python -c "...tkinter..."`）来避开线程问题。
那个在打包 exe 里坏了，因为 `sys.executable` 指向的是 `sift.exe` 不是
Python 解释器——所以子进程根本没弹对话框。线程里直接调 tkinter 在脚本模式
和冻结 exe (frozen exe) 里都能用。Win32 ctypes 被否决——~50 行 COM 仪式
换不来额外好处。

---

## 17. 落地页托管 (Landing-page hosting)

**考虑过的选项：** GitHub Pages、Netlify、Vercel、Cloudflare Pages、自建
服务器。

**决策：** GitHub Pages，从同一个仓库的 `docs/` 文件夹托管。

**原因：** 免费、不用额外账号、每次 push 自动部署 (auto-deploy)、和代码
放一起。其它几个同样免费但在这个规模下多一个平台没好处。

---

## 18. 仓库可见性 (Repository visibility)

**考虑过的选项：** 私有 (private)、公开 (public)。

**决策：** 公开（先私有，后翻转）。

**原因：** 免费 GitHub Pages 和匿名 release 下载都要求仓库公开。Sift 里
没有秘密（令牌 token 一律走 stdin 或环境变量并清除过；`.gitignore` 排除
状态文件），所以公开没成本，反而有真实收益（下载、落地页、分享）。

---

## 19. 内部模块命名 (Internal module naming)

**问题：** 产品叫 "Sift" 但核心模块还是 `dupfinder.py`。

**考虑过的选项：** 全部改名成 `sift.py`、内部保留 `dupfinder.py`。

**决策：** 保留 `dupfinder.py`（以及 `.dupfinder_quarantine/`、
`.dupfinder_cache.sqlite3`）；只有面向用户的字符串写 "Sift"。

**原因：** 改模块名会波及每个 import、测试套件、以及最关键的——磁盘上的
存储路径名。改存储路径会让用户已经隔离的文件变成孤儿 (orphan)。内部改名
这点表面收益不值这个险；用户根本看不到模块名。

---

## 20. 许可证 (License)

**考虑过的选项：** MIT、Apache 2.0、GPL v3、不放。

**决策：** 暂时不放（留给项目所有者决定）。

**原因：** 仓库一开始是私有的；正式公开推广前再加许可证也来得及。所有者
自己定——记在这里是为了说明这个空缺是有意的，不是忘了。

---

## 明确**没做**的事（及原因）

- **Mac / Linux 构建**——PyInstaller 不能交叉编译 (cross-compile)；构建 Mac
  二进制要有台 Mac，加上签名要 ~$99/年 Apple 开发者计划 (Apple Developer
  Program)。暂缓。
- **Android / iPhone**——不是移植 (port)；移动端沙盒 (sandboxing) 让 Sift
  的"遍历硬盘"模型不可能成立。手机照片去重应该是个独立产品（Swift/Kotlin
  重写）。
- **代码签名 (code signing)**——~$300/年；下载量上来之前不值。用户目前
  手动点过 Windows SmartScreen（智能屏幕）警告。
- **自动更新 (auto-update)**——v0.1.0 还太早。
- **遥测 / 崩溃上报 (telemetry / crash reporting)**——和"纯本地、无网络"
  的隐私承诺冲突。
