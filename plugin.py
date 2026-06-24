from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from sdk.plugin import PluginBase
from sdk.plugin_host_context import PluginHostContext
from sdk.register import PluginCapabilityRegistry
from sdk.types import FrontendConfigAction, FrontendConfigContribution, ToolsTabContribution

# 注册 LLM 工具
from plugins.mouse_control import llm_tool as _mouse_llm_tool  # noqa: F401

logger = logging.getLogger(__name__)


class MouseControlPlugin(PluginBase):
    """鼠标控制：手动 + 视觉（Moondream + OmniParser）+ OCR 定位。"""

    def __init__(self) -> None:
        super().__init__()
        self._omniparser_process: subprocess.Popen | None = None

    @property
    def plugin_id(self) -> str:
        return "com.shinsekai.mouse_control"

    @property
    def plugin_name(self) -> str:
        return "鼠标控制"

    @property
    def plugin_description(self) -> str:
        return (
            "19 个鼠标操控工具：地标/网格/百分比手动定位，"
            "Moondream 视觉定位，OmniParser UI 解析，OCR 文字定位。"
        )

    @property
    def plugin_author(self) -> str:
        return "pipi_"

    @property
    def plugin_version(self) -> str:
        return "0.3.0"

    @property
    def priority(self) -> int:
        return 70

    # ── OmniParser 后台进程管理 ─────────────────────────────────

    def _start_omniparser(self, plugin_root: Path) -> None:
        """如果配置启用了自动启动，则在后台拉起 OmniParser Gradio 服务。"""
        from plugins.mouse_control.config_omniparser import load_config

        cfg = load_config(plugin_root)
        if not cfg.enabled or not cfg.auto_start:
            return

        # 检查是否已在运行
        try:
            from urllib.request import urlopen
            urlopen(cfg.server_url.rstrip("/") + "/", timeout=2)
            logger.info("OmniParser 服务已在运行 (%s)，跳过启动。", cfg.server_url)
            return
        except Exception:
            pass

        # omni_server.py 已内置在插件目录
        omniparser_dir = str(plugin_root)
        python_exe = cfg.conda_python
        # 默认为 "python" → 自动找 Shinsekai runtime Python
        if python_exe == "python" or not Path(python_exe).exists():
            runtime_py = (plugin_root / ".." / ".." / "runtime" / "python.exe").resolve()
            if not runtime_py.exists():
                runtime_py = (plugin_root / ".." / ".." / "runtime" / "python3.exe").resolve()
            if runtime_py.exists():
                python_exe = str(runtime_py)
            elif not Path(python_exe).exists():
                logger.warning("找不到 OmniParser Python，跳过自动启动。请在设置中配置路径。")
                return
        if not Path(omniparser_dir).exists():
            logger.warning("OmniParser 目录不存在: %s，跳过自动启动。", omniparser_dir)
            return

        import os as _os
        log_dir = plugin_root / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        out_log = open(str(log_dir / "omniparser_stdout.log"), "w")
        err_log = open(str(log_dir / "omniparser_stderr.log"), "w")
        logger.info("正在后台启动 OmniParser (%s) ...", python_exe)
        try:
            self._omniparser_process = subprocess.Popen(
                [python_exe, "omni_server.py"],
                cwd=omniparser_dir,
                stdout=out_log,
                stderr=err_log,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            logger.info("OmniParser 已启动 (pid=%s)，日志: %s", self._omniparser_process.pid, log_dir)
        except Exception as e:
            logger.exception("OmniParser 启动失败: %s", e)

    def _stop_omniparser(self) -> None:
        """终止后台 OmniParser 进程。"""
        if self._omniparser_process is not None:
            try:
                self._omniparser_process.terminate()
                self._omniparser_process.wait(timeout=5)
                logger.info("OmniParser 已停止。")
            except Exception:
                try:
                    self._omniparser_process.kill()
                except Exception:
                    pass
            self._omniparser_process = None

    # ── OmniParser 配置 schema（React 前端可渲染）────────────────

    _OMNI_SCHEMA = [
        {
            "id": "server",
            "title": "服务连接",
            "fields": [
                {
                    "key": "enabled",
                    "label": "启用 OmniParser",
                    "type": "boolean",
                    "defaultValue": False,
                    "description": "开启后 mouse_omniparser_* 系列工具可用。",
                },
                {
                    "key": "auto_start",
                    "label": "自动启动服务",
                    "type": "boolean",
                    "defaultValue": True,
                    "description": "启动时自动在后台拉起 OmniParser Gradio 服务。关闭则需手动启动。",
                },
                {
                    "key": "server_url",
                    "label": "服务器地址",
                    "type": "string",
                    "defaultValue": "http://127.0.0.1:7861",
                    "description": "OmniParser Gradio 服务的完整 URL。",
                },
            ],
        },
        {
            "id": "detection",
            "title": "检测参数",
            "fields": [
                {
                    "key": "box_threshold",
                    "label": "检测阈值",
                    "type": "number",
                    "defaultValue": 0.03,
                    "min": 0.001,
                    "max": 0.5,
                    "step": 0.005,
                    "description": "YOLO 图标检测置信度。越低检出越多（可能误检），越高越精准。",
                },
                {
                    "key": "iou_threshold",
                    "label": "IOU 阈值",
                    "type": "number",
                    "defaultValue": 0.1,
                    "min": 0.01,
                    "max": 1.0,
                    "step": 0.05,
                    "description": "NMS 去重阈值。越高保留越多重叠框。",
                },
                {
                    "key": "infer_max_side",
                    "label": "推理最长边",
                    "type": "integer",
                    "defaultValue": 1024,
                    "min": 0,
                    "max": 4096,
                    "step": 128,
                    "description": "推理前图片最长边缩放。0=不缩放，1024=平衡速度与精度。",
                },
            ],
        },
    ]

    _OMNI_I18N = {
        "zh_CN": {
            "title": "OmniParser 识屏",
            "description": "Microsoft OmniParser — 专为 UI 截图训练的视觉解析模型，提供像素级 UI 元素定位。",
            "restartHint": "修改后无需重启，设置实时生效。",
            "groups": {
                "server": {
                    "title": "服务连接",
                    "fields": {
                        "enabled": {
                            "label": "启用 OmniParser",
                            "description": "开启后可使用 mouse_omniparser_locate / mouse_omniparser_click 工具。",
                        },
                        "auto_start": {
                            "label": "自动启动服务",
                            "description": "启动时自动在后台拉起 OmniParser Gradio 服务。",
                        },
                        "server_url": {
                            "label": "服务器地址",
                            "description": "OmniParser Gradio 服务地址，默认 http://127.0.0.1:7861。",
                        },
                    },
                },
                "detection": {
                    "title": "检测参数",
                    "fields": {
                        "box_threshold": {
                            "label": "检测阈值",
                            "description": "YOLO 图标检测置信度阈值。",
                        },
                        "iou_threshold": {
                            "label": "IOU 阈值",
                            "description": "NMS 去重 IOU 阈值。",
                        },
                        "infer_max_side": {
                            "label": "推理最长边",
                            "description": "推理前缩放图片的最长边像素数。",
                        },
                    },
                },
            },
        },
    }

    def initialize(
        self,
        register: PluginCapabilityRegistry,
        plugin_root: Path,
        host: PluginHostContext,
    ) -> None:
        # ── 注册 Qt 设置页（桌面端兼容） ──
        def build_omni_tab(plg):
            from plugins.mouse_control.settings_tab import OmniParserSettingsTab

            return OmniParserSettingsTab(plg, plugin_root)

        register.register_tools_tab(
            ToolsTabContribution(
                tab_id="mouse_control_omniparser",
                title="OmniParser 识屏",
                build=build_omni_tab,
                order=45.5,
            )
        )

        # ── 注册 React 前端配置页 ──
        from plugins.mouse_control.config_omniparser import (
            OmniParserConfig,
            load_config,
            save_config,
        )

        def load_values():
            cfg = load_config(plugin_root)
            return {
                "enabled": cfg.enabled,
                "auto_start": cfg.auto_start,
                "server_url": cfg.server_url,
                "box_threshold": cfg.box_threshold,
                "iou_threshold": cfg.iou_threshold,
                "infer_max_side": cfg.infer_max_side,
            }

        def save_values(values):
            cfg = OmniParserConfig(
                enabled=bool(values.get("enabled", False)),
                auto_start=bool(values.get("auto_start", True)),
                server_url=str(values.get("server_url", "http://127.0.0.1:7861")),
                box_threshold=float(values.get("box_threshold", 0.03)),
                iou_threshold=float(values.get("iou_threshold", 0.1)),
                infer_max_side=int(values.get("infer_max_side", 1024)),
            )
            save_config(cfg, plugin_root)
            # 如果用户开启了自动启动，立即尝试拉起服务
            if cfg.enabled and cfg.auto_start:
                self._start_omniparser(plugin_root)

        def test_connection(values):
            """测试连接 — 检查 OmniParser 服务是否在线。"""
            url = str(values.get("server_url", "http://127.0.0.1:7862")).rstrip("/")
            try:
                from urllib.request import urlopen
                urlopen(f"{url}/health", timeout=5)
                return {"ok": True, "message": f"✓ 服务在线 ({url})"}
            except Exception as e:
                return {"ok": False, "message": f"✗ 无法连接: {e}"}

        def install_omniparser_deps(values):
            """一键安装 OmniParser 依赖 + 下载模型。"""
            # 自动找 Shinsekai runtime Python
            py = (plugin_root / ".." / ".." / "runtime" / "python.exe").resolve()
            if not py.exists():
                py = (plugin_root / ".." / ".." / "runtime" / "python3.exe").resolve()
            python_exe = str(py) if py.exists() else str(values.get("conda_python", "python"))
            # 权重直接装到插件目录下
            weights_dir = str(plugin_root / "omniparser_weights")
            omniparser_dir = str(plugin_root)

            steps: list[str] = []
            import subprocess as _sp, os as _os

            # Step 1: pip install deps (不需要克隆仓库，omni_server 已内置)
            steps.append("正在安装依赖 (ultralytics, transformers, gradio)...")
            try:
                _sp.run(
                    [python_exe, "-m", "pip", "install",
                     "ultralytics", "supervision==0.18.0", "gradio",
                     "transformers", "accelerate", "timm", "einops", "opencv-python-headless",
                     "-q"],
                    cwd=omniparser_dir, capture_output=True, text=True, timeout=600,
                )
                steps.append("✓ 依赖安装完成")
            except Exception as e:
                steps.append(f"⚠ 依赖安装失败: {e}")

            # Step 2: download weights into plugin
            steps.append("正在下载模型权重 (约 2GB，首次需几分钟)...")
            try:
                _os.makedirs(weights_dir, exist_ok=True)
                for f in [
                    "icon_detect/train_args.yaml", "icon_detect/model.pt", "icon_detect/model.yaml",
                    "icon_caption/config.json", "icon_caption/generation_config.json", "icon_caption/model.safetensors",
                ]:
                    _sp.run(
                        [python_exe, "-c",
                         f"from huggingface_hub import hf_hub_download; "
                         f"hf_hub_download('microsoft/OmniParser-v2.0', '{f}', local_dir='{weights_dir}')"],
                        timeout=300,
                    )
                # rename
                if Path(f"{weights_dir}/icon_caption").exists():
                    _os.rename(f"{weights_dir}/icon_caption", f"{weights_dir}/icon_caption_florence")
                steps.append("✓ 模型权重下载完成")
            except Exception as e:
                steps.append(f"⚠ 权重下载失败: {e}。请设置 HuggingFace 镜像或手动下载。")

            steps.append("✅ 安装完成！请在「服务器地址」确认端口，然后启用插件。")
            return {"ok": True, "message": "\n".join(steps)}

        register.register_frontend_config_page(
            FrontendConfigContribution(
                page_id="mouse_control_omniparser",
                title="OmniParser 识屏",
                description=(
                    "Microsoft OmniParser — UI 截图专用视觉解析模型（已内置于插件）。\n"
                    "GitHub: https://github.com/microsoft/OmniParser\n"
                    "首次使用点「一键安装」下载依赖 (ultralytics, transformers) 和模型权重 (约 2GB)。"
                ),
                restart_hint="修改后无需重启，设置实时生效。",
                kind="tools",
                schema=self._OMNI_SCHEMA,
                i18n=self._OMNI_I18N,
                load_values=load_values,
                save_values=save_values,
                actions=[
                    FrontendConfigAction(
                        id="install_deps",
                        label="一键安装",
                        description="自动克隆仓库、安装依赖、下载模型权重（约 2-3GB，首次需 5-10 分钟）",
                        variant="primary",
                        run=install_omniparser_deps,
                    ),
                    FrontendConfigAction(
                        id="test_connection",
                        label="测试连接",
                        description="检查 OmniParser 服务是否在线",
                        variant="secondary",
                        run=test_connection,
                    ),
                    FrontendConfigAction(
                        id="reset_defaults",
                        label="恢复默认",
                        description="将所有参数恢复为默认值",
                        variant="ghost",
                        run=lambda _: {
                            "ok": True,
                            "defaults": {
                                "enabled": False,
                                "server_url": "http://127.0.0.1:7862",
                                "box_threshold": 0.03,
                                "iou_threshold": 0.1,
                                "infer_max_side": 1024,
                            },
                        },
                    ),
                ],
                order=45.5,
            )
        )

        # ── 自动启动 OmniParser 后台服务 ──
        self._start_omniparser(plugin_root)

    def shutdown(self) -> None:
        self._stop_omniparser()
