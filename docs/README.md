# 服务生命周期与问答工作流

## 启动与退出

| 入口或事件 | 行为 |
| --- | --- |
| `Start.cmd` | 校验配置、启动服务、注册搜索 Pipe，随后在系统默认浏览器打开 Open WebUI。 |
| `Start.cmd -NoBrowser` | 启动同一组服务并保留本机 API。通过 `Stop.cmd` 停机。 |
| `Start.cmd -ManagedBrowser auto` | 解析 Windows 的 HTTP 默认浏览器，启动受管会话。支持 Edge、Chrome、Firefox 正式版和 ESR。 |
| `Start.cmd -ManagedBrowser "C:\path\to\firefox.exe"` | 按指定路径启动独立的受管会话。关闭该会话最后一个窗口时整套停机。 |
| 关闭普通项目页 | 仍有其他项目页时继续处理请求。最后一页关闭且在途任务结束后，卸载支持路由的项目自有模型。 |
| `Stop.cmd` | 停止接收新任务，在宽限期内等待在途任务结束，随后取消剩余任务并停止本次启动的服务。 |

受管模式使用 `runtime/browser-*` 存放独立浏览器配置。Firefox 正式版和 ESR 按可执行文件路径使用各自的配置目录。切换浏览器，或从普通入口及无浏览器模式改用受管模式时，先运行 `Stop.cmd`。

[启动脚本](../Start.ps1) · [停止脚本](../Stop.ps1) · [服务管理器](../integration/service_manager.py)

[关闭浏览器时停止服务的设计](browser-shutdown-design.md)

## 服务与模型

每个项目目录有一个常驻管理进程。它验证本机配置、聊天数据库、运行依赖及端口。端点被占用时，依据模型列表、重排接口或 SearXNG 配置判断服务是否可复用。Open WebUI 需要提供同一项目的页面状态接口。自动启用 Pipe 后才打开浏览器。

Windows Job Object 管理本次创建的模型路由、Open WebUI、SearXNG 和子进程。启动失败或管理进程异常退出时回收自启的进程。复用实例由原启动者管理。显式停止入口会按 PID、启动时间及程序路径核对旧版进程记录。

默认地址如下。

| 服务 | 地址 |
| --- | --- |
| Open WebUI | `http://127.0.0.1:3000` |
| SearXNG | `http://127.0.0.1:8888` |
| Qwen API | `http://127.0.0.1:8080/v1` |
| BGE API | `http://127.0.0.1:8081/v1` |

各自的 llama.cpp 路由只注册配置中的 GGUF，第一次推理时加载。Qwen 使用配置的上下文和 GPU 层数，启用 Flash Attention、`q8_0` KV 缓存和 Jinja。BGE 使用重排和 `rank` 池化。llama.cpp 缺少路由预设或卸载支持时，对相应模型使用常驻单模型模式。

公开的模型地址经管理进程转发，保留模型发现、流式响应和推理错误。客户端断开会关闭上游请求，卸载前核对模型槽位。Open WebUI 通过已认证的 Socket.IO 连接统计各项目页。至少出现一个项目页后，最后一个连接断开才启动卸载宽限期。页面快速重连会刷新计时，连接状态不可用时推迟卸载。

宽限期结束且没有在途问答、模型调用和活跃推理槽位时，管理器通过 `POST /models/unload` 卸载模型，并用 `GET /models` 核对状态。卸载进行时的新请求等待卸载完成。状态无法确认时，推理请求收到可重试的 HTTP 503；路由状态可用后恢复准入。两个模型 API、Open WebUI 和 SearXNG 在卸载后继续监听。

[Windows Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects) · [llama.cpp 模型路由](https://github.com/ggml-org/llama.cpp/blob/5c53396b8/tools/server/README.md#using-multiple-models)

## 问答任务

问答任务的请求标识和截止时间覆盖追问改写、搜索、BGE 重排、正文提取、Qwen 生成以及调用 Pipe 的后台任务。Open WebUI 的停止按钮使用其任务取消入口。同步问答 API 的 HTTP 客户端断开时，同样取消对应问答。取消、超时和整套停机通过关闭模型连接和终止对应的 Node 进程树清理工作，待读取线程退出后释放问答记录。

状态栏在搜索、重排后读取网页、生成和完成时更新。候选数和入选数来自实际结果。正文读取失败时使用搜索摘要，并在完成状态显示回退数量。回答的数字引用对应 Open WebUI 来源卡片。

[搜索 Pipe](../integration/local_search_pipe.py) · [搜索桥接](../integration/search-bridge.mjs)

## 运行配置与升级

`config/local.json` 是本机配置，支持原有的 `llama_server`、`qwen_model`、`reranker_model`、`gpu_layers`、`qwen_context` 和 `reranker_context`。下列字段均可省略。

| 字段 | 默认值 | 用途 |
| --- | --- | --- |
| `qwen_url` | `http://127.0.0.1:8080/v1` | 回答模型 API |
| `bge_url` | `http://127.0.0.1:8081/v1` | 重排模型 API |
| `searxng_url` | `http://127.0.0.1:8888` | 搜索服务 |
| `qwen_name` | `Qwen3-4B` | 回答模型 API 标识 |
| `bge_name` | `bge-reranker-v2-m3` | 重排模型 API 标识 |
| `node_path` | 从 PATH 查找 | Node.js 程序路径 |
| `device` | llama.cpp 自动选择 | llama.cpp 设备名称，可用逗号分隔 |
| `unload_grace_seconds` | 30 | 末页断开后卸载宽限期 |
| `task_timeout_seconds` | 420 | 问答的总超时 |
| `shutdown_grace_seconds` | 15 | 停机前在途任务收尾时间 |

自定义 API 地址按外部服务访问。启动时从 Functions API 读取已有 Valves，将自定义地址迁入 `config/local.json`；显式本机配置优先。候选数和 Top K 仍在 Open WebUI 调整。Open WebUI 的项目默认连接随 Qwen 地址更新，用户自行配置的其他连接由 Open WebUI 管理。生效模型地址记录在 `logs/manager.log`，服务错误见各组件的 `*.err.log`。

升级时先运行 `Stop.cmd`。安装器保留 `config/local.json`、`config/searxng.yml` 和现有聊天数据，并校验本机配置及聊天数据库可读取。

## 验证入口

通过实际 `Start.cmd`、浏览器和问答 API 启动验证，并用 `Stop.cmd` 清理。可从公开端口读取两个模型的状态，显存由 GPU 工具独立测量。

```powershell
(Invoke-RestMethod 'http://127.0.0.1:8080/models').data |
    Select-Object id, @{Name='state'; Expression={$_.status.value}}
(Invoke-RestMethod 'http://127.0.0.1:8081/models').data |
    Select-Object id, @{Name='state'; Expression={$_.status.value}}
nvidia-smi --query-gpu=memory.used --format=csv
```

| 场景 | 预期 |
| --- | --- |
| 重复启动、启动中停止、占端口、Pipe 注册失败 | 单一实例。日志指向失败组件。自启进程回收，外部实例继续运行。 |
| 空闲和在途任务期间分别关闭末页 | 到达宽限期且结束在途任务后，Qwen 和 BGE 状态均为 `unloaded`，显存下降，四个服务端口仍可访问。 |
| 多标签页、多窗口、刷新、快速重连、API 与卸载同时发生 | 任一页面保持连接时继续服务。卸载开始后，新请求等待后恢复。 |
| 卸载后重新问答 | Qwen 和 BGE 按需加载。验证首次聊天、追问、重排、自动标题，记录冷启动、热请求和显存。 |
| UI 停止、HTTP 断开、任务超时、整套停机、管理器异常退出 | 关闭对应的 HTTP 请求，清理 Node 及项目拥有的进程，复用实例继续响应。 |
| 默认浏览器、独立浏览器、无浏览器 | 默认浏览器关页后卸载。独立浏览器关闭最后一个窗口后停机。无浏览器模式可以通过 `Stop.cmd` 退出。 |
| Windows 10/11 x64 清洁安装和已有配置升级 | 模型参数、端口、聊天记录、搜索设置和有效外部地址可继续使用。 |
