# 熊猫领航 · 评论客户工作台

Python + PySide6 桌面应用，SQLite 本地数据库，QtWebEngine 内嵌多账号浏览器，Playwright 在后台直接驱动这些内嵌页面。

**当前是可运行、可配置的开发版本，不是已完成实号验收的自动营销成品。** 界面、数据库、AI 客户筛选及私信生成、后台任务、账号隔离、导入导出已经实现。抖音页面在开发环境访问超时，评论选择器、数据响应格式和真实私信流程未获实号验证；发送送达回执也尚未适配。发送动作正常完成后应用直接标记“已私信”，该状态表示操作完成，不代表站点确认送达。不能把本版本理解为已验证的无人值守全自动流程。

## 环境要求

在全新电脑（macOS 或 Windows）上从零开始部署，请先满足以下条件：

| 项目 | 要求 |
| --- | --- |
| 操作系统 | macOS 13 及以上（Intel / Apple Silicon 均可），或 Windows 10 / 11 64 位 |
| uv | 建议使用当前稳定版；用于管理 Python 环境与依赖，可按 `pyproject.toml` 自动下载所需 Python |
| 浏览器 | 由 PySide6 附带的 QtWebEngine 内嵌提供，无需另外安装 Chrome / Edge，也不调用系统默认浏览器 |
| 网络 | 能访问抖音（douyin.com）以及所配置的 AI 服务地址 |

> 依赖在 `pyproject.toml` 中声明（PySide6 / QtWebEngine、playwright、httpx、keyring，开发依赖 pytest），用 `uv sync` 一键安装，**不需要**执行 `playwright install`，也不需要安装外部浏览器。

## 部署与运行

### 第 1 步：获取代码

把整个项目放到本地任意目录（路径中可以包含中文和空格）。两种方式任选：

- **下载压缩包**：下载 zip 后解压到目标目录。
- **git 克隆**（需先安装 git）：

```sh
git clone <仓库地址> douyin_comment_attach
cd douyin_comment_attach
```

### 第 2 步：安装 uv

uv 会按 `pyproject.toml` 管理 Python 版本和依赖。若本机没有兼容的 Python，uv 会从境外下载 Python；在中国境内部署时，建议先确认本机有 Python 3.10 及以上（推荐 3.12），或准备可信来源的 Python 安装包。

**国内网络优先**：若已安装 Python 和 pip，可以直接从清华 PyPI 镜像安装 uv（macOS 终端或 Windows PowerShell 均可）：

```sh
python -m pip install -U uv -i https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple
uv --version
```

Windows 上若 `python` 命令不可用，可将第一行的 `python` 换成 `py`。若安装成功后仍找不到 `uv`，检查 pip 提示的脚本安装目录是否加入 PATH，再重新打开终端。以下官方安装方式适用于能够访问安装脚本和其下载地址的网络。

**macOS**

打开“终端”（启动台 → 其他 → 终端），执行：

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**Windows**

打开“PowerShell”（按 Win+R，输入 `powershell` 回车），执行：

```powershell
irm https://astral.sh/uv/install.ps1 | iex
```

安装完成后**关闭并重新打开**终端 / PowerShell，验证：

```sh
uv --version
```

> 如果提示找不到 `uv`：macOS 默认装在 `~/.local/bin`，Windows 默认装在 `%USERPROFILE%\.local\bin`；安装脚本通常会自动写入 PATH，重开终端即可生效。
>
> macOS 已配置 Homebrew 时也可执行 `brew install uv`；Homebrew 本身及软件包下载地址仍需按当地网络情况确认。

### 第 3 步：浏览器准备

无需操作。内嵌浏览器内核由 PySide6 依赖一起安装，不会读取日常 Chrome / Edge 的用户资料。

### 第 4 步：安装依赖（uv sync）

在项目根目录打开终端，执行一条命令即可创建 `.venv`、下载所需 Python 并安装全部依赖：

```sh
cd /路径/douyin_comment_attach
uv sync
```

`uv sync` 会根据 `pyproject.toml` 和 `uv.lock` 生成可复现的环境；第一次运行会自动下载 Python 和依赖，之后的运行会自动判断是否需要更新。

> **国内依赖下载**：项目自带 `uv.toml`，`uv.lock` 的包下载地址也已指向清华 PyPI 镜像。这个配置覆盖 PySide6 / QtWebEngine 等 Python 包，不覆盖 uv 或 Python 本身的安装包。若 uv 需要下载 Python 而网络超时，见下方 FAQ。

### 第 5 步：启动应用

**macOS**

- 双击项目根目录的 `start.command`（首次双击若被系统拦截，见下方 FAQ）。
- 或在终端运行：

```sh
uv run main.py
```

**Windows**

- 双击项目根目录的 `start.bat`（会保留一个命令行窗口显示运行日志，运行期间不要关闭它）。
- 或在命令提示符运行：

```bat
uv run main.py
```

`uv run` 会先确保环境已同步，再启动应用。启动后即可按「使用顺序」开始配置。

### 运行方式说明

每个账号通过“登录 / 查看内嵌浏览器”创建自己的页面，所有页面集中在“内嵌浏览器”工作区，可以像标签页一样随时切换查看。支持主号和多个从号并行，登录状态保存在 `data/embedded_profiles/<账号 UUID>`，不读取日常浏览器资料。任务运行时可查看页面，但不要手动跳转或点击正在自动操作的账号页。

> 从旧版独立 Chrome 窗口升级后，需要在新的内嵌页中为每个账号重新登录一次。旧 `data/profiles/` 不会被删除，但新版不再使用它。

## 环境变量（可选）

| 变量 | 作用 | 示例 |
| --- | --- | --- |
| `DOUYIN_AI_API_KEY` | 指定 AI API Key，优先级高于系统钥匙串 / 凭据管理器里保存的 Key | macOS：`export DOUYIN_AI_API_KEY=sk-xxx`；Windows cmd：`set DOUYIN_AI_API_KEY=sk-xxx` |
| `DOUYIN_DATA_DIR` | 指定数据目录（数据库、登录 profile、备份） | 见「数据与状态 → 恢复备份」 |

API Key 默认存系统钥匙串（macOS 钥匙串 / Windows 凭据管理器）；如果保存或读取失败，可直接用环境变量 `DOUYIN_AI_API_KEY` 传入，Key 不会写入客户数据库或导出文件。

## 常见问题（FAQ）

- **macOS 双击 `start.command` 提示“无法打开 / 未验证开发者”**：右键该文件 → 打开 → 打开；或到“系统设置 → 隐私与安全性”点“仍要打开”。
- **内嵌浏览器一直显示“正在连接”**：先关闭并重启整个应用；不要在同一数据目录启动多个实例。如果系统安全软件拦截本机 `127.0.0.1` 端口，需允许本应用的本机进程间连接。
- **依赖下载很慢或超时**：项目级 `uv.toml` 优先于全局 uv 配置，且当前 `uv.lock` 含清华镜像的包地址。要换源，请修改项目根目录的 `uv.toml`，运行 `uv lock` 更新锁文件，再运行 `uv sync`；只改全局配置不会覆盖项目设置。
- **uv 需要下载 Python 但 GitHub 很慢**：先安装可信来源的 Python 3.10+（推荐 3.12），确认 `python --version` 或 Windows 的 `py --version` 正常，再运行 `uv sync --no-python-downloads`。uv 的 `UV_PYTHON_INSTALL_MIRROR` 只适用于提供完整 `python-build-standalone` 发布目录结构的可信镜像，不能把任意 GitHub 网页代理直接当作稳定下载源。
- **提示找不到 `uv` 命令**：确认已安装并重新打开终端；macOS 默认装在 `~/.local/bin`，Windows 装在 `%USERPROFILE%\.local\bin`，检查该目录是否在 PATH 中。
- **是否需要 `playwright install`？** 不需要。应用使用 PySide6 自带的 QtWebEngine，不需要 Playwright 额外下载 Chromium。
- **首次运行弹出钥匙串 / 凭据访问请求**：允许即可；若不想用系统钥匙串，用 `DOUYIN_AI_API_KEY` 环境变量。
- **访问抖音超时或页面空白**：先确认浏览器能打开 douyin.com 并检查网络 / 代理；本版本站点适配尚未实号验证，见「当前站点适配边界」。

## 使用顺序

1. “AI 与任务设置”填写服务根地址（例如服务商提供的 `https://域名/v1`）、模型名和 API Key。使用兼容 Chat Completions 的 JSON 接口。配置默认可留空，也可随时修改保存。
2. 填写产品和目标客户筛选提示词，以及个性化私信提示词。评论是 AI 的输入数据，不作为指令执行。API Key 存系统钥匙串，也支持环境变量 `DOUYIN_AI_API_KEY`；环境变量优先。Key 不写入客户数据库或导出文件。
3. “主号 / 从号”添加一个主号，点“登录 / 查看内嵌浏览器”，应用会切换到对应账号标签，由本人完成抖音登录。应用不自动判断“页面已打开”等于“登录成功”。添加多个从号同理。
4. “监控博主”录入博主完整主页链接和备注。分别设置最近 N 天的视频、最近 N 天的评论、轮询间隔与每页滚动上限。
5. 选择主号，开启持续任务。浏览器访问博主作品和视频，监听网页正常加载的数据，不伪造请求签名、不调用回复接口。只有配置时间范围内的新顶层评论会经 AI 判别后入库，过期评论不会提交给 AI。新增目标客户在界面刷新。
6. 选择从号，设置该号发送间隔，开启持续任务。各从号以 SQLite 原子事务独占领取客户，生成私信并打开目标主页私信框。客户主页或私信控件定位失败时只标记当前客户失败，从号会继续运行；遇到验证、登录或频率限制等账号级页面提示时才停止该号并显示错误。
7. 发送动作正常完成后状态直接变为“已私信”。如发送过程报错，客户会标记为“失败”并保留异常说明，不会自动重发；重新排队后会直接复用已有私信文案，只有文案为空时才再请求大模型。重新排队前请在对应从号浏览器里检查聊天记录。

从号检查和发送复用同一客户主页，等待操作区就绪，不在 AI 生成文案后再次刷新。误打开右侧“消息”列表时，会根据当前页面位置点击右上角关闭，确认消失后再打开客户私信。主页检查总时限 45 秒，发送流程总时限 90 秒；页面超时或交互异常会记录当前客户失败并按账号间隔继续，已开始提交的消息不会自动重发。状态栏显示文案生成、发送阶段和间隔秒数。代码更新后需重启应用才会使用新流程。

打开私信后优先等待可操作的输入框。仅“消息”文字或标题栏尺寸不能判定为消息列表；有会话输入框时不会关闭该会话。输入框加载期间暂时找不到关闭按钮不会立即报失败。私信交互失败时尝试保存 `data/diagnostics/<账号>/chat-failure-*.png` 并在日志中显示路径，截图可能包含页面上的聊天内容，请按客户数据保管。

内嵌浏览器会拦截 `bitbrowser://` 等非 HTTP/HTTPS 的未知外部应用链接，避免 Windows 弹出“在 Microsoft Store 中查找应用”的提示，正常抖音网页导航不受影响。

如果单个账号的页面超时或 WebEngine 内核异常，程序会保留当前客户的安全状态，并自动重建该账号的页面后继续队列。如果界面仍完全无响应，先停止该账号任务，再在它的内嵌浏览器工具栏点击“重启浏览器”，或在“主号 / 从号”页点击“重启选中浏览器”。该操作只重建选中账号页面，保留登录数据，不影响其他账号。

未选中的账号标签会被 Qt 界面隐藏，应用会持续将其 WebEngine 页面保持为 Active/Visible，并禁用 Chromium 的后台计时器、遮挡窗口和渲染降频。启动从号后日志应出现“后台页面已保持活跃”，无需切换到该账号页。这些启动参数只在应用启动时读取，更新后必须完全退出并重启应用。

主界面和账号浏览器的标签容器使用叠放模式：当前页在最上层，其他页保持挂载和渲染。这使“关注成功”之后的页面状态、私信入口和定时器在运行日志页或其他账号标签处于前台时也会继续更新。

**开始任务会实际尝试发送私信，并非演示开关。** 未配置 AI 时不会运行。程序未内置任何真实客户或演示收件人，不会在启动时自动采集或发送。

## 数据与状态

- `data/customers.sqlite3`：账号备注、博主、所有已判别顶层评论、去重客户、配置和操作事件。
- `data/embedded_profiles/<账号 UUID>`：每个账号的 QtWebEngine 登录状态。
- `data/backups/`：清空前自动生成的完整数据库备份。
- 评论按 `cid` 去重；客户按完整 `sec_uid` 去重。它是主页 `/user/` 后的 MS4… 字符串，不是数字抖音号。
- 同一客户的新评论可更新等待队列里的素材，但不会把已发送或失败客户重新排队。历史评论保存在 comments 表。
- 状态：等待私信、处理中、已私信、失败、已跳过。私密或页面明确提示无法私信的客户会记录原因并跳过，不再自动分配；可在客户列表人工标记跳过、单条重新排队，或一键将全部失败客户重新排队。批量重排前必须停止任务并确认，请先核对聊天记录以避免重复私信。
- 多账号通过 `BEGIN IMMEDIATE` 原子领取任务，避免重复领取。退出后遗留的处理中客户转为失败，禁止自动重发；历史“待核验”记录也会转为失败并保留异常说明。该机制优先避免重复发送，不声称能跨网络实现严格 exactly-once。
- 开启中的任务不会自动重启。停止时未点击发送的客户可回到待发送；点击后的不确定结果需在聊天记录中检查后再决定是否重新排队。
- 账号删除只删账号记录，关闭其浏览器，保留本地 profile 和客户历史。
- 清空前必须停止任务并确认，会先备份，再清空客户及评论去重记录。以后重新监控可能再次发现这些客户。
- 恢复备份：关闭应用，将备份复制到一个新的数据目录并命名为 `customers.sqlite3`，用 `DOUYIN_DATA_DIR=/新目录 uv run main.py` 启动（Windows 在命令提示符里用 `set DOUYIN_DATA_DIR=D:\新目录&& uv run main.py`）。登录目录需要另外复制 `embedded_profiles/`；不要在 SQLite 正在打开时直接覆盖数据库。

## 导入与导出

支持 UTF-8 JSON 数组和 UTF-8 CSV。至少有 `uid` 或 `url`，其余字段可以为空：

```json
[
  {
    "uid": "这里填真实完整的MS4开头sec_uid",
    "name": "昵称",
    "text": "原始评论",
    "comment_time": "2026-09-14T08:00:00+00:00",
    "video_title": "视频名称",
    "blogger": "博主名称",
    "status": "pending"
  }
]
```

示例 ID 是占位说明，不能直接导入。导入文件整批校验，已存在的客户不覆盖，不会将已发送状态重置。JSON 导出保留原始文本，推荐用作数据迁移；CSV 对公式前缀加单引号以便用表格软件安全查看。导出包含全部客户，不受当前列表筛选影响。所有时间以记录的 ISO 格式展示，发送时间使用 UTC。

## 当前站点适配边界

`app/browser.py` 隔离站点变动。监听路径 `/aweme/v1/web/aweme/post/` 和 `/aweme/v1/web/comment/list/`，解析 `aweme_list` / `comments`；这些预置适配尚未在本次实号页面核验。评论滚动区域默认 `[data-e2e="comment-list"]`，私信输入框默认 `[contenteditable="true"]`，可在界面调整。找不到唯一目标时失败关闭，不任意点击猜测元素。

每页有滚动上限与无变化停止条件，因此大号作品或大量评论可能只覆盖部分数据；日志明确提示不完整。不保证读取平台未提供、权限不允许或尚未加载的数据。下一轮从页面重新扫描，数据库跳过已经判别的评论。作品每批加载后检查日期：忽略 is_top 标记的置顶作品，普通作品按发布时间降序且批次尾部早于截止日期时停止滚动。批次内符合日期的作品仍全部保留；未知时间或顺序异常时不提前停止。置顶标记依赖站点响应格式。

仍需实号联调：作品分页、评论面板、登录失效提示、聊天对象身份校验、发送失败提示、与目标和消息对应的服务端成功回执。回执未完成前不应自动标为 sent。

## 验证

```sh
uv sync
QT_QPA_PLATFORM=offscreen uv run pytest -q
```

测试包括唯一主号、并发客户领取、评论/客户去重、已发送状态保护、导入事务、崩溃恢复、顶层评论过滤、备份和 PySide6 界面构建。

内嵌页由 QtWebEngine 显示，应用在随机的本机回环端口上启用 Chromium DevTools 协议，Playwright 后台线程仅通过该本机端口操作页面；不启动外部 Chrome 进程或窗口。

技术资料：[Playwright BrowserType](https://playwright.dev/python/docs/api/class-browsertype)、[Qt WebEngine](https://doc.qt.io/qtforpython-6/PySide6/QtWebEngineWidgets/index.html)。
