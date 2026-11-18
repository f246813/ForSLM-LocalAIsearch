# 关闭浏览器时停止服务的设计

ForSLM 可以用 Edge、Chrome、Firefox 正式版或 Firefox ESR 启动独立的受管浏览器会话。关闭该会话的最后一个主窗口会触发与 Stop.cmd 共用的停机流程。 [启动脚本](../Start.ps1) · [受管浏览器](../integration/managed_browser.py) · [服务管理器](../integration/service_manager.py)

## 用户行为

受管浏览器指本次由 ForSLM 启动、使用项目独立配置目录的实例。**关闭这一实例的最后一个浏览器主窗口即停止本次启动的 ForSLM 服务。** 用户可以先关掉所有项目标签页，再关闭浏览器。整窗关闭信号由 Windows 提供，退出确认框暂缓。

| 操作 | 结果 |
| --- | --- |
| 关闭最后一个 ForSLM 标签页，浏览器仍有窗口 | 按已有规则等待在途任务结束并回收模型显存。服务继续监听。 |
| 关闭受管会话的一个窗口，仍有其他受管窗口 | 服务继续运行。剩余窗口即使只打开其他网站，也代表浏览器会话仍在使用。 |
| 关闭最后一个受管窗口，或受管浏览器异常退出且确认窗口已经消失 | 整套停机优先于模型卸载。项目启动的模型、搜索、Open WebUI 和任务子进程退出。复用服务由原启动者管理。 |
| 关闭最后一个标签页的操作同时关闭最后一个受管窗口 | 按整窗退出处理。 |
| 最小化、切换虚拟桌面，或关闭个人浏览器窗口 | 受管窗口仍存在，服务继续运行。 |
| 从普通浏览器直接访问，或通过 `-NoBrowser` 启动 | 普通入口使用原有页面机制。显式全停通过 `Stop.cmd`。 |

## 启动和浏览器兼容性

继续接受 `Start.cmd -ManagedBrowser "<浏览器可执行文件>"`。使用 `Start.cmd -ManagedBrowser auto`，从 Windows 的 HTTP 协议关联解析默认浏览器并选择适配器。默认仍使用普通 `Start.cmd`，自动停机由用户通过受管入口启用。关联无法解析为已支持的程序时，在启动前报告原因，用户可直接传入路径。独立配置目录与个人浏览器配置分别保存。 [Windows 协议关联查询](https://learn.microsoft.com/en-us/windows/win32/api/shlwapi/nf-shlwapi-assocquerystringw)

| 桌面浏览器 | 受管入口 |
| --- | --- |
| Edge、Chrome | 沿用独立 `--user-data-dir` 和已有 Windows 窗口观察。即使浏览器留下后台进程，仍按最后一个主窗口关闭判断。 |
| Firefox 正式版、ESR | 支持直接指定 `firefox.exe`。当 Firefox 是 Windows 默认浏览器时，`-ManagedBrowser auto` 选择同一适配器。 |
| 其他桌面浏览器 | 可正常打开 Open WebUI。受管能力以各适配器验收结果为准。 |

每个项目管理实例绑定一个受管浏览器会话。再次用同一浏览器启动时复用已绑定的会话。切换浏览器，或从普通入口及 `-NoBrowser` 改用受管模式时，先显式结束已有实例再启动，避免把常驻 API 服务意外绑定到浏览器窗口。启动前检查配置目录占用和适用的浏览器策略。启动后以浏览器实际持有的配置目录确认归属。Edge 的 `UserDataDir` 策略可以覆盖启动参数，遇到覆盖时应报告受管模式不可用。 [Chromium 配置目录说明](https://github.com/chromium/chromium/blob/main/docs/user_data_dir.md) · [Firefox 启动参数](https://firefox-source-docs.mozilla.org/browser/CommandLineParameters.html#startup-and-profiles) · [Edge 配置目录策略](https://learn.microsoft.com/en-us/deployedge/microsoft-edge-policies/userdatadir) · [Edge 后台进程说明](https://learn.microsoft.com/en-us/deployedge/microsoft-edge-policies/startupboostenabled)

### Firefox 适配

- **启动与配置目录。** 接受 `Start.cmd -ManagedBrowser "<Firefox 安装路径>\firefox.exe"`。适配器以 `--new-instance --profile "<项目配置目录>" --new-window "<Open WebUI 地址>"` 启动。按 Firefox 可执行文件的规范化绝对路径复用对应的项目配置目录，让不同安装的正式版和 ESR 各自保存配置。配置目录已被占用时按启动失败处理，个人 Firefox 会话继续运行。 [Firefox 启动参数](https://firefox-source-docs.mozilla.org/browser/CommandLineParameters.html) · [Firefox 实例启动源码](https://searchfox.org/firefox-main/source/toolkit/xre/nsAppRunner.cpp#10727-10743)
- **窗口归属。** 以 `MozillaWindowClass` 为主窗口候选，结合顶层窗口类型和浏览器专用 Job Object 的成员归属验证。首次确认的是实际浏览器主窗口，启动器进程先退出时继续等待浏览器进程交接。Firefox 的配置文件提示、恢复对话框和开发工具窗口需要与普通主窗口区分。 [Firefox 原生窗口类](https://searchfox.org/firefox-main/source/widget/windows/nsWindowDefs.h#710-717) · [Firefox Windows 启动器](https://searchfox.org/firefox-main/source/browser/app/winlauncher/LauncherProcessWin.cpp)
- **停机。** Firefox 使用下节的窗口观察规则和同一个停机入口。常用入口与 Edge、Chrome 共用服务管理、任务收尾及异常回滚逻辑。

## 窗口观察与停机

1. **建立归属。** 启动前确定可执行文件、项目配置目录和当前管理实例。复用现有 Job Object 启动机制，为本次受管浏览器单独建立进程组。检查主窗口所属进程在该组内。Chromium 通过配置目录对应的进程单例窗口核实目录归属。Firefox 通过主窗口所属进程的配置参数及项目配置锁核实目录归属。首次确认主窗口后才建立绑定。Firefox 的启动器可能先于浏览器主进程退出，因此按实际持有窗口的进程核对。若配置目录被占用或无法在截止时间内确认归属，清理本次浏览器进程组。专为本次受管启动创建的服务实例也执行回滚。 [现有 Job Object 实现](../integration/windows_job.py) · [Firefox Windows 启动器](https://searchfox.org/firefox-main/source/browser/app/winlauncher/LauncherProcessWin.cpp) · [Windows 进程组成员查询](https://learn.microsoft.com/en-us/windows/win32/api/jobapi/nf-jobapi-isprocessinjob)
2. **观察窗口。** 复用 `EnumWindows` 和窗口所属进程查询。浏览器适配器核对原生窗口类型和浏览器界面标识。Firefox 使用主浏览器文档标识，Chromium 使用原生浏览器工具栏标识。通知、独立开发工具、启动提示和辅助对话框不计入主窗口。已经确认的主窗口继续计数，直到窗口销毁或身份发生变化。最小化和切换虚拟桌面不会移除仍有效的主窗口。标题、当前是否前台及浏览器进程名都无法单独证明归属。
3. **确认最后一个窗口关闭。** 从观察到有效窗口之后开始判断。确认所有已知主窗口均已销毁，并连续约 1 秒未出现新的受管主窗口后，提交关闭事件。拖拽标签页、新建窗口等短暂过渡出现新窗口时清零计时。枚举失败时将观测记为未知，恢复后重新核对。控制状态与日志记录窗口变化、观察失败与恢复。停机时记录触发原因，详见 `logs/manager.log`。
4. **走统一停机流程。** 校验关闭事件仍属于当前管理实例和浏览器会话，再通知现有停止入口。关闭和 `Stop.cmd` 同时到达时只处理一次。管理器停止接纳新任务，允许已获准的问答继续使用模型直到任务结束或达到现有 `shutdown_grace_seconds`，随后取消超时工作、关闭本地服务端口并回收项目进程。受管浏览器有独立的短时退出期限，正常关窗时允许它写回配置，超时只回收其专用进程组。

默认入口的页面连接仍用于末页卸载。整套停机依据受管会话的真实窗口状态，因此在 ForSLM 页面已经关闭、浏览器后台进程仍存活或浏览器跳过确认框时都能判定。观察状态无法确认时，`Stop.cmd` 仍可停止服务。 [Win32 顶层窗口枚举](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-enumwindows) · [Win32 窗口进程查询](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getwindowthreadprocessid)

## 验收

从实际 `Start.cmd -ManagedBrowser` 进入。记录浏览器版本、触发来源、受管窗口变化、在途任务处理结果和项目进程退出情况。Edge、Chrome、Firefox 正式版及 Firefox ESR 均执行关窗用例。

| 操作 | 验收结果 |
| --- | --- |
| 开两个受管窗口，先关一个，再关最后一个 | 第一次关窗时服务继续响应。最后一个窗口关闭后完成一次停机。 |
| 先关闭 ForSLM 最后一个标签页，保留其他页面，再退出浏览器 | 先观察模型卸载和服务端口继续监听，再观察整套停机。 |
| 在已有个人浏览器窗口时启动受管会话，最小化或快速新开受管窗口 | 个人窗口与受管窗口的计数互相独立。短暂交接和最小化继续运行。仅受管会话最后一个窗口关闭触发停机。 |
| Firefox 正式版与 ESR 分别通过显式路径和 `auto` 启动，已有个人 Firefox 窗口 | 启动独立 Firefox 会话，首个主窗口能在启动器交接后识别。关闭个人窗口只影响个人会话，关闭最后一个受管 Firefox 窗口后核对项目服务端口和进程均已退出。 |
| 浏览器后台常驻、浏览器异常退出、窗口枚举失败 | 后台进程常驻时仍能按主窗口关闭停机。异常退出按实例归属及窗口消失判定。枚举失败记录原因，观测恢复前服务保持可用。 |
| 浏览器路径错误、策略覆盖配置目录、配置目录冲突、启动器交接失配、重复启动 | 报告具体故障。回滚本次受管启动的资源，个人浏览器和已有普通或无浏览器实例继续运行。 |
| 在搜索、生成或直接模型请求期间关窗，同时调用 `Stop.cmd` | 只进入一次停机。获准的任务在宽限期内收尾或被取消。核对项目自启的端口和进程均已退出，复用服务仍响应。 |

用实际配置中的端口核对停机。默认检查 `3000`、`8888`、`8080` 和 `8081`，以及项目创建的模型路由、模型子进程、Python、Node 和服务管理器。外部复用端口按归属单独验证。

运行窗口观察与浏览器选择的回归测试。

```powershell
.\runtime\webui-env\Scripts\python.exe -m unittest discover -s tests -v
```
