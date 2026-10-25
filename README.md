# Reference Panel

A local reference shelf for MCP Apps-enabled AI harnesses on macOS.

把视频、图片和文档放进 AI harness 的参考面板，点击预览，右键在 Finder 中定位。通过标准 MCP stdio 与 MCP Apps 接入宿主；添加的是文件路径，移除参考会保留原文件。

**状态：0.7.0，实验性源码预览版。** 当前后台已在 Codex 中运行，自动检查与本地浏览器验证通过；当前版本的完整原生缩略图和视频播放验收仍未完成。请先用非敏感测试文件试用。独立社区项目。

## 功能

- 拖入文件或通过 macOS 文件选择器多选添加。
- 缩略图网格与列表视图，视频显示时长。
- 面板内预览视频、音频、图片、PDF、HTML 网页和文本；部分办公文档可提取文字。
- Show in Finder、用默认应用打开、复制路径、重新定位失效文件。
- 多选移除与撤销；同一项目的对话共享参考，其他项目保持独立。
- 跟随宿主的浅色／深色主题。

## 从源码安装

需要 macOS 12+、Python 3.9+ 和 Xcode Command Line Tools。开发测试还需要 Node.js 18+。编译器为 Apple silicon 和 Intel 生成 universal 本机助手。

### 通用 MCP Apps 宿主

1. 克隆或下载本仓库，在终端进入仓库根目录。
2. 如果没有开发工具，先运行 `xcode-select --install`，完成系统安装。
3. 编译本机助手，并生成适用于当前路径的 MCP 配置：

```sh
sh plugins/reference-native-test/build-picker.sh
python3 scripts/mcp-config.py
```

将输出中的 `reference-panel` 条目合并到宿主的本地 MCP server 配置中，保留已有服务。脚本只输出配置，不修改应用设置。连接后可请求模型调用 `open_reference_probe` 打开参考面板。

宿主需要支持本地 stdio 服务、MCP Apps HTML 界面、面板调用 server tools，以及 `blob:` 图片/媒体。只支持文本 MCP 的终端 harness 能访问工具，但不能显示面板。不同宿主的配置入口和面板位置不同；通用配置与无对话 ID 的逻辑已有自动检查，具体其他应用尚未实际验收。

### Codex 的侧栏集成

Codex 用户可选用额外的 marketplace 入口，提供对话侧栏面板：

```sh
sh scripts/install-codex.sh
```

脚本先编译两个助手，再注册当前目录并安装 `reference-panel@reference-panel`。需要 `codex` 位于 PATH，或通过 `CODEX_BIN=/path/to/codex` 指定 CLI。不会自动重启应用；如果更新后仍显示旧版，重新打开宿主加载新后台。

本源码包没有预编译助手，因此应先克隆到本地并编译。直接从远程 Git marketplace 安装会缺少 `bin/`。旧原型的 `reference-native-test@reference-panel-lab` 和公开插件是不同 ID；确认公开版可用后，可手动停用旧原型，避免两个入口。

## 项目共享

同一项目中的对话共用一份参考列表，添加和移除会同步；面板显示当前项目名称。打开的面板每 3 秒检查一次项目列表，返回面板时也会刷新。并发保存通过修订号合并，项目切换会校验列表身份。

Codex 中使用公开的 `thread/read` 接口读取当前对话的目录，不恢复对话、不运行模型、不读取完整聊天记录；需要本机 `codex` CLI，或以 `CODEX_BIN` 指定。目录查询使用短时缓存，界面不使用插件安装目录推断项目。

其他宿主可在工具元数据中提供 `projectRoot`、`project.root`、`workspaceRoot` 或 `cwd`（绝对目录），也支持 `projectId`。也可为项目配置固定目录：

```sh
python3 scripts/mcp-config.py --project /absolute/path/to/project
```

生成配置中的 `REFERENCE_PROJECT_ROOT` 固定此服务的共享项目。未识别项目时退回当前对话；没有对话身份时使用全局共享列表，面板会说明范围。

升级前的对话列表在该对话首次打开新版面板时自动合并一次，按路径去重，原记录保留。合并标记会保留，因此已移除的参考不会因重开旧对话而重新出现。旧对话移到其他项目时，不会再次携带其历史列表。

## 数据与权限

为保留原型用户的数据，默认目录仍是 `~/Library/Application Support/codex-reference-panel/`，也可用 `REFERENCE_STATE_DIR` 指定其他目录：`lists/` 保存路径列表，`thumbnails/` 保存缩略图缓存，`diagnostics/` 保存本机诊断信息。删除列表项目不删除原文件，卸载插件也不会自动清理上述目录。

插件后端不向网络发送文件。模型可调用工具查看文件路径及元数据；预览内容和缩略图通过 MCP Apps 的 widget 通道交给宿主。宿主自身的数据处理规则独立适用。详见 [隐私与数据说明](PRIVACY.md)。

## 已知边界

- 当前仅支持 macOS；项目以规范化目录或宿主项目 ID 识别。不同 worktree 目录需要配置相同的 `REFERENCE_PROJECT_ROOT` 才能共用。
- 视频完整读入后才播放；没有原先的 64 MiB 总大小门槛，但大文件仍需等待并占用浏览器存储。超大文件和长期运行尚未完成压力测试。
- 播放依赖宿主支持的编码，不能保证所有 MOV、MP4 或其他容器均可播放。
- HTML 支持完整排版和内嵌图片的静态预览；脚本、外部资源和外部跳转禁用。
- iCloud 文件需要已经下载到本机。尚未下载的文件会给出提示。
- Agent 目前只能打开面板和读取列表，不能直接批量增删参考。
- 未提供项目信息的宿主按对话保存；连对话 ID 也没有时共用一份列表。其他宿主尚未实际验证。
- 本项目提供源码构建，没有 Developer ID 签名、公证或面向普通用户的一键安装包。

## 开发与验证

```sh
sh plugins/reference-native-test/build-picker.sh
python3 -m unittest discover -s verification -p 'test_*.py' -v
node verification/test_probe_ui.cjs
```

目前包含 66 项 Python 检查和合成页面检查。测试使用临时数据；通过它们不代表真实 Finder 拖放、选择器或原生宿主播放已验收。

可在本机运行相同页面与后台的浏览器验证环境：

```sh
python3 verification/preview_browser_fixture.py /absolute/path/to/test-video.mp4
```

它只开放指定测试文件和生成的样例，使用独立临时记录；不要把它的结果等同于 Codex 原生面板验收。开发细节见 [贡献说明](CONTRIBUTING.md)。

面板协议参考 [MCP Apps 官方规范与 SDK](https://apps.extensions.modelcontextprotocol.io/)。Codex 专属安装命令参考 [Codex developer commands](https://learn.chatgpt.com/docs/developer-commands#codex-plugin-marketplace)。

## 许可证

[MIT](LICENSE)。
