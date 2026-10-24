# 贡献说明

运行环境与构建、验证命令见 [README](README.md)。Python 服务只依赖标准库，页面使用原生 JavaScript，本机助手使用 macOS 框架。

源码入口：

- `plugins/reference-native-test/server.py`：MCP 工具、路径列表和本机助手管理。
- `plugins/reference-native-test/preview.py`：预览片段、权限范围、缩略图缓存。
- `plugins/reference-native-test/panel.html`：MCP Apps 界面和交互。
- `plugins/reference-native-test/native-picker.m`：文件选择和拖放路径读取。
- `plugins/reference-native-test/native-render.m`：文档、图片、缩略图和媒体元数据。
- `verification/`：临时数据测试及本地浏览器验证环境。

提交修改前运行全部 Python 和页面检查。涉及本机助手时重新构建；涉及交互时注明测试的是模拟桥接、本地浏览器还是实际原生宿主。测试素材在临时目录生成；未经许可不要提交个人截图、视频或诊断日志。

请保持点击预览、用户主动增删、移除保留原文件的行为。避免添加自动收集或未经用户操作的文件读取功能。插件 ID 和状态目录涉及已有用户数据，修改前说明迁移方案。

报告问题时提供 macOS 版本、芯片类型、宿主版本、文件格式和重现步骤。包含敏感数据或安全问题的记录，请先脱敏；不要在公开 issue 中上传原始本机诊断目录。
