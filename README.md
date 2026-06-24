# Shinsekai Mouse Control

Shinsekai 鼠标操控插件，21 个 LLM 工具。作者：pipi_

## 功能

- **手动定位**：地标（中心/四角）/ 网格 / 百分比，DPI 无关
- **OCR 点击**：识别屏幕文字 → 精确坐标 → 点击
- **OmniParser**：Microsoft UI 解析模型，像素级包围盒，一键安装
- **Moondream 视觉**：自然语言描述目标，模型返回坐标
- **智能拖拽**：SendInput 长按拖动，PS/游戏均可用
- **中断检测**：手动动鼠标时自动取消

## 安装

1. 复制 `plugins/mouse_control/` 到 Shinsekai 的 `plugins/` 目录
2. 插件管理 → 启用"鼠标控制"
3. 运行 `install.bat` 装 pyautogui
4. （可选）插件设置 → OmniParser → 一键安装

## 使用

让角色调用工具即可：
- "点一下登录按钮"
- "把窗口拖到屏幕中间"
- "找一下屏幕上的提交按钮，点它"

## 依赖

pyautogui | rapidocr-onnxruntime | OmniParser（一键装）| Moondream 插件
