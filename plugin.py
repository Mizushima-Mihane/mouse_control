from __future__ import annotations

import os
import logging
import subprocess
import threading
import textwrap
import time
from pathlib import Path
from urllib.parse import urlparse

from sdk.plugin import PluginBase
from sdk.plugin_host_context import PluginHostContext
from sdk.register import PluginCapabilityRegistry
from sdk.types import FrontendConfigAction, FrontendConfigContribution, ToolsTabContribution
from plugins.mouse_control.config_omniparser import (
    DEFAULT_CONDA_PYTHON,
    DEFAULT_OMNIPARSER_DIR,
    DEFAULT_SERVER_URL,
)

# 注册 LLM 工具
from plugins.mouse_control import llm_tool as _mouse_llm_tool  # noqa: F401

logger = logging.getLogger(__name__)

DEFAULT_HF_ENDPOINT = "https://hf-mirror.com"


def _plugin_source_root() -> Path:
    return Path(__file__).resolve().parent


def _project_root() -> Path:
    return _plugin_source_root().parent.parent


def _resolve_plugin_data_root(plugin_root: Path) -> Path:
    root = Path(plugin_root).expanduser()
    if not root.is_absolute():
        root = _project_root() / root
    return root.resolve()


def _path_from_setting(value: str, base: Path) -> Path | None:
    raw = os.path.expandvars((value or "").strip())
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def _windows_cmd_path(value: str | Path) -> str:
    text = str(value)
    unc_prefix = "\\\\?\\UNC\\"
    extended_prefix = "\\\\?\\"
    # cmd.exe builtins such as pushd treat extended-length paths as UNC-like
    # paths and can fail with "The network name cannot be found".
    if text.startswith(unc_prefix):
        return "\\\\" + text[len(unc_prefix):]
    if text.startswith(extended_prefix):
        return text[len(extended_prefix):]
    return text


def _runtime_python() -> Path | None:
    runtime_dir = _project_root() / "runtime"
    for name in ("python.exe", "python3.exe", "python", "bin/python3", "bin/python"):
        candidate = runtime_dir / name
        if candidate.exists():
            return candidate.resolve()
    return None


def _resolve_python_executable(configured: str) -> str | None:
    text = (configured or "").strip()
    if text and text.lower() not in {"python", "python.exe", "python3", "python3.exe"}:
        configured_path = _path_from_setting(text, _project_root())
        if configured_path and configured_path.exists():
            return str(configured_path)
    runtime = _runtime_python()
    if runtime is not None:
        return str(runtime)
    return text or None


def _resolve_omniparser_source_dir(configured: str) -> Path:
    configured_dir = _path_from_setting(configured, _plugin_source_root())
    if configured_dir and configured_dir.exists():
        return configured_dir
    return _plugin_source_root()


def _server_port(server_url: str) -> int:
    parsed = urlparse(server_url)
    return parsed.port or 7862


def _health_url(server_url: str) -> str:
    return server_url.rstrip("/") + "/health"


def _tail_file(path: Path, limit: int = 4000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-limit:].strip()


def _pid_path(plugin_root: Path) -> Path:
    return Path(plugin_root) / "omniparser.pid"


def _write_omniparser_pid(plugin_root: Path, pid: int) -> None:
    root = Path(plugin_root)
    root.mkdir(parents=True, exist_ok=True)
    _pid_path(root).write_text(str(int(pid)), encoding="utf-8")


def _read_omniparser_pid(plugin_root: Path) -> int | None:
    try:
        text = _pid_path(Path(plugin_root)).read_text(encoding="utf-8").strip()
        pid = int(text)
    except (OSError, ValueError):
        return None
    return pid if pid > 0 else None


def _clear_omniparser_pid(plugin_root: Path) -> None:
    try:
        _pid_path(Path(plugin_root)).unlink()
    except FileNotFoundError:
        pass
    except OSError:
        logger.debug("failed to remove OmniParser pid file", exc_info=True)


def _subprocess_creationflags() -> int:
    return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def _subprocess_new_consoleflags() -> int:
    return subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0


def _pid_for_listening_port(port: int) -> int | None:
    if os.name != "nt":
        return None
    try:
        result = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=_subprocess_creationflags(),
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    marker = f":{int(port)}"
    for raw in result.stdout.splitlines():
        parts = raw.split()
        if len(parts) < 5:
            continue
        local, state, pid_text = parts[1], parts[3], parts[-1]
        if marker in local and state.upper() == "LISTENING":
            try:
                return int(pid_text)
            except ValueError:
                return None
    return None


def _pid_command_line(pid: int) -> str:
    if os.name != "nt":
        return ""
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=_subprocess_creationflags(),
        )
    except Exception:
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _pid_is_mouse_control_omniparser(pid: int) -> bool:
    cmd = _pid_command_line(pid).lower()
    return "omni_server.py" in cmd and "mouse_control" in cmd


def _terminate_omniparser_pid(pid: int) -> bool:
    if pid <= 0 or pid == os.getpid():
        return False
    if os.name == "nt":
        if not _pid_is_mouse_control_omniparser(pid):
            return False
        result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=_subprocess_creationflags(),
        )
        return result.returncode == 0
    try:
        os.kill(pid, 15)
        return True
    except OSError:
        return False


class MouseControlPlugin(PluginBase):
    """鼠标控制：手动 + 视觉（Moondream + OmniParser）+ OCR 定位。"""

    def __init__(self) -> None:
        super().__init__()
        self._omniparser_process: subprocess.Popen | None = None
        self._omniparser_start_thread: threading.Thread | None = None
        self._plugin_root: Path | None = None
        self._omniparser_pid: int | None = None

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
        return "0.3.1"

    @property
    def priority(self) -> int:
        return 70

    # ── OmniParser 后台进程管理 ─────────────────────────────────

    def _start_omniparser(self, plugin_root: Path) -> None:
        """如果配置启用了自动启动，则在后台拉起插件内置 OmniParser HTTP 服务。"""
        from plugins.mouse_control.config_omniparser import load_config

        plugin_root = _resolve_plugin_data_root(plugin_root)
        cfg = load_config(plugin_root)
        if not cfg.enabled or not cfg.auto_start:
            return

        # 检查是否已在运行
        try:
            from urllib.request import urlopen
            urlopen(_health_url(cfg.server_url), timeout=2)
            pid = _pid_for_listening_port(_server_port(cfg.server_url))
            if pid is not None and _pid_is_mouse_control_omniparser(pid):
                self._omniparser_pid = pid
                _write_omniparser_pid(plugin_root, pid)
            logger.info("OmniParser 服务已在运行 (%s)，跳过启动。", cfg.server_url)
            return
        except Exception:
            pass

        # omni_server.py 已内置在插件目录
        source_dir = _resolve_omniparser_source_dir(cfg.omniparser_dir)
        server_file = source_dir / "omni_server.py"
        python_exe = _resolve_python_executable(cfg.conda_python)
        if not python_exe:
            logger.warning("找不到 OmniParser Python，跳过自动启动。请在设置中配置路径。")
            return
        if not server_file.exists():
            logger.warning("OmniParser 服务文件不存在: %s，跳过自动启动。", server_file)
            return

        log_dir = plugin_root / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        out_log = open(str(log_dir / "omniparser_stdout.log"), "w")
        err_log = open(str(log_dir / "omniparser_stderr.log"), "w")
        logger.info("正在后台启动 OmniParser (%s) ...", python_exe)
        try:
            env = os.environ.copy()
            env["OMNIPARSER_WEIGHTS_DIR"] = str(plugin_root / "omniparser_weights")
            env["OMNIPARSER_EASYOCR_DIR"] = str(plugin_root / "easyocr")
            env["OMNIPARSER_PORT"] = str(_server_port(cfg.server_url))
            env.setdefault("HF_ENDPOINT", DEFAULT_HF_ENDPOINT)
            self._omniparser_process = subprocess.Popen(
                [python_exe, str(server_file)],
                cwd=str(source_dir),
                env=env,
                stdout=out_log,
                stderr=err_log,
                creationflags=_subprocess_creationflags(),
            )
            self._omniparser_pid = self._omniparser_process.pid
            _write_omniparser_pid(plugin_root, self._omniparser_process.pid)
            deadline = time.time() + 30
            while time.time() < deadline:
                if self._omniparser_process.poll() is not None:
                    logger.error(
                        "OmniParser 启动后退出 (code=%s)。stderr: %s",
                        self._omniparser_process.returncode,
                        _tail_file(log_dir / "omniparser_stderr.log"),
                    )
                    return
                try:
                    from urllib.request import urlopen
                    urlopen(_health_url(cfg.server_url), timeout=2)
                    logger.info("OmniParser 服务已就绪 (pid=%s)，日志: %s", self._omniparser_process.pid, log_dir)
                    return
                except Exception:
                    time.sleep(1)
            logger.info("OmniParser 进程已启动但仍在加载模型 (pid=%s)，日志: %s", self._omniparser_process.pid, log_dir)
        except Exception as e:
            logger.exception("OmniParser 启动失败: %s", e)

    def _start_omniparser_async(self, plugin_root: Path) -> None:
        """Schedule OmniParser startup without blocking the plugin loader."""
        thread = self._omniparser_start_thread
        if thread is not None and thread.is_alive():
            return
        self._omniparser_start_thread = threading.Thread(
            target=self._start_omniparser,
            args=(plugin_root,),
            name="mouse-control-omniparser-start",
            daemon=True,
        )
        self._omniparser_start_thread.start()

    def _stop_omniparser(self) -> None:
        """终止后台 OmniParser 进程。"""
        plugin_root = self._plugin_root
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
            self._omniparser_pid = None
            if plugin_root is not None:
                _clear_omniparser_pid(plugin_root)
            return

        pid = self._omniparser_pid
        if pid is None and plugin_root is not None:
            pid = _read_omniparser_pid(plugin_root)
        if pid is not None and _terminate_omniparser_pid(pid):
            logger.info("OmniParser 已停止 (pid=%s)。", pid)
        self._omniparser_pid = None
        if plugin_root is not None:
            _clear_omniparser_pid(plugin_root)

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
                    "description": "启动时自动在后台拉起插件内置 OmniParser HTTP 服务。关闭则需手动启动。",
                },
                {
                    "key": "server_url",
                    "label": "服务器地址",
                    "type": "string",
                    "defaultValue": DEFAULT_SERVER_URL,
                    "description": "OmniParser HTTP 服务的完整 URL。",
                },
                {
                    "key": "conda_python",
                    "label": "Python 路径",
                    "type": "string",
                    "defaultValue": DEFAULT_CONDA_PYTHON,
                    "description": "留空或 python 表示使用 Shinsekai runtime Python。",
                },
                {
                    "key": "omniparser_dir",
                    "label": "服务脚本目录",
                    "type": "string",
                    "defaultValue": DEFAULT_OMNIPARSER_DIR,
                    "description": "留空表示使用插件内置 omni_server.py。",
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
                            "description": "启动时自动在后台拉起插件内置 OmniParser HTTP 服务。",
                        },
                        "server_url": {
                            "label": "服务器地址",
                            "description": "OmniParser HTTP 服务地址，默认 http://127.0.0.1:7862。",
                        },
                        "conda_python": {
                            "label": "Python 路径",
                            "description": "留空或 python 表示使用 Shinsekai runtime Python。",
                        },
                        "omniparser_dir": {
                            "label": "服务脚本目录",
                            "description": "留空表示使用插件内置 omni_server.py。",
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
        plugin_root = _resolve_plugin_data_root(plugin_root)
        self._plugin_root = plugin_root

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
                "conda_python": cfg.conda_python,
                "omniparser_dir": cfg.omniparser_dir,
            }

        def save_values(values):
            current = load_config(plugin_root)
            cfg = OmniParserConfig(
                enabled=bool(values.get("enabled", False)),
                auto_start=bool(values.get("auto_start", True)),
                server_url=str(values.get("server_url", DEFAULT_SERVER_URL)),
                box_threshold=float(values.get("box_threshold", 0.03)),
                iou_threshold=float(values.get("iou_threshold", 0.1)),
                infer_max_side=int(values.get("infer_max_side", 1024)),
                conda_python=str(values.get("conda_python", current.conda_python or DEFAULT_CONDA_PYTHON)),
                omniparser_dir=str(values.get("omniparser_dir", current.omniparser_dir or DEFAULT_OMNIPARSER_DIR)),
            )
            save_config(cfg, plugin_root)
            # 如果用户开启了自动启动，立即尝试拉起服务
            if cfg.enabled and cfg.auto_start:
                self._start_omniparser_async(plugin_root)
            else:
                # 禁用或关闭自动启动时停止插件托管的后台服务，避免静默常驻。
                self._stop_omniparser()

        def test_connection(values):
            """测试连接 — 检查 OmniParser 服务是否在线。"""
            url = str(values.get("server_url", DEFAULT_SERVER_URL)).rstrip("/")
            try:
                from urllib.request import urlopen
                urlopen(f"{url}/health", timeout=5)
                return {"ok": True, "message": f"✓ 服务在线 ({url})"}
            except Exception as e:
                return {"ok": False, "message": f"✗ 无法连接: {e}"}

        def install_omniparser_deps(values):
            """一键安装 OmniParser 依赖 + 下载模型。"""
            python_exe = _resolve_python_executable(str(values.get("conda_python", DEFAULT_CONDA_PYTHON))) or "python"
            # 权重直接装到插件目录下
            weights_dir = plugin_root / "omniparser_weights"
            omniparser_dir = str(_resolve_omniparser_source_dir(str(values.get("omniparser_dir", DEFAULT_OMNIPARSER_DIR))))

            steps: list[str] = []
            import os as _os
            import subprocess as _sp

            env = _os.environ.copy()
            # HuggingFace Hub honors HF_ENDPOINT; default to hf-mirror for CN installs.
            env.setdefault("HF_ENDPOINT", DEFAULT_HF_ENDPOINT)
            pip_packages = [
                "ultralytics", "supervision==0.18.0", "gradio",
                "transformers", "accelerate", "timm", "einops", "opencv-python-headless",
                "rapidocr-onnxruntime", "easyocr",
            ]

            def _tail(text: str, limit: int = 1800) -> str:
                return (text or "").strip()[-limit:]

            def _run_step(label: str, cmd: list[str], timeout: int) -> bool:
                steps.append(label)
                try:
                    result = _sp.run(
                        cmd,
                        cwd=omniparser_dir,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=timeout,
                        env=env,
                    )
                except Exception as e:
                    steps.append(f"✗ {label} 失败: {e}")
                    return False
                if result.returncode != 0:
                    output = "\n".join(part for part in (_tail(result.stdout), _tail(result.stderr)) if part)
                    steps.append(f"✗ {label} 失败 (exit={result.returncode})")
                    if output:
                        steps.append(output)
                    return False
                output = _tail(result.stdout)
                if output:
                    steps.append(output)
                steps.append(f"✓ {label} 完成")
                return True

            download_code = f"""
from __future__ import annotations
import json
import os
import shutil
import time
from pathlib import Path
from urllib.parse import quote
import requests

os.environ.setdefault("HF_ENDPOINT", {DEFAULT_HF_ENDPOINT!r})
endpoint = os.environ.get("HF_ENDPOINT", {DEFAULT_HF_ENDPOINT!r}).rstrip("/")
weights_dir = Path({str(weights_dir)!r})
weights_dir.mkdir(parents=True, exist_ok=True)

def download_file(repo: str, name: str, target_root: Path) -> None:
    target = target_root / name
    if name.startswith("icon_caption/"):
        merged = target_root / "icon_caption_florence" / name.split("/", 1)[1]
        if merged.exists() and merged.stat().st_size > 0:
            print(f"skip existing: {{merged}}")
            return
    if target.exists() and target.stat().st_size > 0:
        print(f"skip existing: {{target}}")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    url = f"{{endpoint}}/{{repo}}/resolve/main/{{quote(name, safe='/')}}"
    print(f"download: {{url}} -> {{target}}")
    with requests.get(url, stream=True, timeout=60, allow_redirects=True) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("Content-Length") or 0)
        downloaded = 0
        started = time.monotonic()
        last_report = started
        tmp = target.with_suffix(target.suffix + ".tmp")
        with tmp.open("wb") as fh:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    fh.write(chunk)
                    downloaded += len(chunk)
                    now = time.monotonic()
                    if now - last_report >= 1 or (total and downloaded >= total):
                        elapsed = max(now - started, 0.001)
                        speed = downloaded / elapsed / 1024 / 1024
                        if total:
                            percent = downloaded * 100 / total
                            print(f"  {{downloaded / 1024 / 1024:.1f}}/{{total / 1024 / 1024:.1f}} MiB ({{percent:.1f}}%) {{speed:.2f}} MiB/s", flush=True)
                        else:
                            print(f"  {{downloaded / 1024 / 1024:.1f}} MiB {{speed:.2f}} MiB/s", flush=True)
                        last_report = now
        tmp.replace(target)

for name in [
    "icon_detect/train_args.yaml",
    "icon_detect/model.pt",
    "icon_detect/model.yaml",
    "icon_caption/config.json",
    "icon_caption/generation_config.json",
    "icon_caption/model.safetensors",
]:
    download_file("microsoft/OmniParser-v2.0", name, weights_dir)

src = weights_dir / "icon_caption"
dst = weights_dir / "icon_caption_florence"
if src.exists():
    dst.mkdir(parents=True, exist_ok=True)
    for child in src.iterdir():
        target = dst / child.name
        if target.exists():
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
        shutil.move(str(child), str(target))
    src.rmdir()

# OmniParser-v2.0 ships Florence weights/config but not processor/tokenizer code.
# Mirror the missing runtime files so omni_server.py can run local_files_only=True.
for name in [
    "configuration_florence2.py",
    "modeling_florence2.py",
    "processing_florence2.py",
    "preprocessor_config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "vocab.json",
]:
    download_file("microsoft/Florence-2-base-ft", name, dst)

configuration_path = dst / "configuration_florence2.py"
configuration_text = configuration_path.read_text(encoding="utf-8")
needle = "        # ensure backward compatibility for BART CNN models\\n"
patch = (
    "        if not hasattr(self, \\"forced_bos_token_id\\"):\\n"
    "            self.forced_bos_token_id = kwargs.get(\\"forced_bos_token_id\\", None)\\n"
)
if patch not in configuration_text:
    configuration_text = configuration_text.replace(needle, needle + patch)
    configuration_path.write_text(configuration_text, encoding="utf-8")

processing_path = dst / "processing_florence2.py"
processing_text = processing_path.read_text(encoding="utf-8")
processing_text = processing_text.replace(
    "tokenizer.additional_special_tokens +",
    "getattr(tokenizer, \\"additional_special_tokens\\", []) +",
)
processing_path.write_text(processing_text, encoding="utf-8")

modeling_path = dst / "modeling_florence2.py"
modeling_text = modeling_path.read_text(encoding="utf-8")
# The Florence files mirrored from microsoft/Florence-2-base-ft are executed
# with the Shinsekai bundled transformers build. These small local patches keep
# loading offline and avoid generation-utils compatibility traps on fresh installs.
for old_tied_weights, new_tied_weights in [
    (
        '    _tied_weights_keys = ["encoder.embed_tokens.weight", "decoder.embed_tokens.weight"]',
        '    _tied_weights_keys = {{\\n'
        '        "encoder.embed_tokens.weight": "shared.weight",\\n'
        '        "decoder.embed_tokens.weight": "shared.weight",\\n'
        '    }}',
    ),
    (
        '    _tied_weights_keys = ["encoder.embed_tokens.weight", "decoder.embed_tokens.weight", "lm_head.weight"]',
        '    _tied_weights_keys = {{\\n'
        '        "model.encoder.embed_tokens.weight": "model.shared.weight",\\n'
        '        "model.decoder.embed_tokens.weight": "model.shared.weight",\\n'
        '    }}',
    ),
    (
        '    _tied_weights_keys = ["language_model.encoder.embed_tokens.weight", "language_model.decoder.embed_tokens.weight", "language_model.lm_head.weight"]',
        '    _tied_weights_keys = {{\\n'
        '        "language_model.model.encoder.embed_tokens.weight": "language_model.model.shared.weight",\\n'
        '        "language_model.model.decoder.embed_tokens.weight": "language_model.model.shared.weight",\\n'
        '    }}',
    ),
]:
    modeling_text = modeling_text.replace(old_tied_weights, new_tied_weights)
modeling_text = modeling_text.replace(
    "class Florence2ForConditionalGeneration(Florence2PreTrainedModel):\\n"
    "    _tied_weights_keys = {{\\n"
    '        "language_model.model.encoder.embed_tokens.weight": "language_model.model.shared.weight",\\n'
    '        "language_model.model.decoder.embed_tokens.weight": "language_model.model.shared.weight",\\n'
    "    }}",
    "class Florence2ForConditionalGeneration(Florence2PreTrainedModel):\\n"
    "    _supports_sdpa = False\\n"
    "    _tied_weights_keys = {{\\n"
    '        "language_model.model.encoder.embed_tokens.weight": "language_model.model.shared.weight",\\n'
    '        "language_model.model.decoder.embed_tokens.weight": "language_model.model.shared.weight",\\n'
    "    }}",
)
modeling_text = modeling_text.replace(
    "        # past_key_values_length\\n"
    "        past_key_values_length = past_key_values[0][0].shape[2] if past_key_values is not None else 0",
    "        # Transformers newer than the upstream Florence file may pass an\\n"
    "        # EncoderDecoderCache object instead of the older tuple cache.\\n"
    "        if past_key_values is not None and hasattr(past_key_values, \\"get_seq_length\\"):\\n"
    "            past_key_values_length = past_key_values.get_seq_length()\\n"
    "        else:\\n"
    "            past_key_values_length = past_key_values[0][0].shape[2] if past_key_values is not None else 0",
)
modeling_text = modeling_text.replace(
    "        if past_key_values is not None:\\n"
    "            past_length = past_key_values[0][0].shape[2]",
    "        if past_key_values is not None:\\n"
    "            if hasattr(past_key_values, \\"get_seq_length\\"):\\n"
    "                past_length = past_key_values.get_seq_length()\\n"
    "            else:\\n"
    "                past_length = past_key_values[0][0].shape[2]",
)
modeling_text = modeling_text.replace(
    "            past_key_value = past_key_values[idx] if past_key_values is not None else None",
    "            if past_key_values is not None and hasattr(past_key_values, \\"self_attention_cache\\"):\\n"
    "                layer_cache = list(past_key_values)[idx]\\n"
    "                if not layer_cache or layer_cache[0] is None:\\n"
    "                    past_key_value = None\\n"
    "                else:\\n"
    "                    past_key_value = layer_cache[:2] + layer_cache[3:5] if len(layer_cache) == 6 else layer_cache\\n"
    "            else:\\n"
    "                past_key_value = past_key_values[idx] if past_key_values is not None else None",
)
modeling_path.write_text(modeling_text, encoding="utf-8")

config_path = dst / "config.json"
cfg = json.loads(config_path.read_text(encoding="utf-8"))
auto_map = cfg.setdefault("auto_map", {{}})
auto_map["AutoConfig"] = "configuration_florence2.Florence2Config"
auto_map["AutoModelForCausalLM"] = "modeling_florence2.Florence2ForConditionalGeneration"
auto_map["AutoProcessor"] = "processing_florence2.Florence2Processor"
config_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"weights ready: {{weights_dir}}")
"""
            easyocr_code = f"""
import easyocr
easyocr.Reader(['en'], model_storage_directory={str(plugin_root / "easyocr")!r}, user_network_directory={str(plugin_root / "easyocr")!r})
print("easyocr model ready")
"""

            def _cmd_quote(value: str) -> str:
                return '"' + value.replace('"', '""') + '"'

            def _write_windows_installer() -> Path:
                install_dir = plugin_root / "logs" / "installer"
                install_dir.mkdir(parents=True, exist_ok=True)
                download_script = install_dir / "download_omniparser_models.py"
                easyocr_script = install_dir / "prepare_easyocr.py"
                cmd_script = install_dir / "install_omniparser.cmd"
                download_script.write_text(textwrap.dedent(download_code), encoding="utf-8")
                easyocr_script.write_text(textwrap.dedent(easyocr_code), encoding="utf-8")
                pip_args = " ".join(pip_packages)
                cmd_omniparser_dir = _windows_cmd_path(omniparser_dir)
                cmd_python_exe = _windows_cmd_path(python_exe)
                cmd_download_script = _windows_cmd_path(str(download_script))
                cmd_easyocr_script = _windows_cmd_path(str(easyocr_script))
                cmd_script.write_text(
                    "\n".join(
                        [
                            "@echo off",
                            "chcp 65001 >nul",
                            "title Mouse Control OmniParser Installer",
                            f"set HF_ENDPOINT={env['HF_ENDPOINT']}",
                            "echo Mouse Control OmniParser Installer",
                            "echo.",
                            "echo Step 1/3: installing Python dependencies...",
                            f"pushd {_cmd_quote(cmd_omniparser_dir)}",
                            f"{_cmd_quote(cmd_python_exe)} -m pip install {pip_args}",
                            "if errorlevel 1 goto failed",
                            "echo.",
                            "echo Step 2/3: downloading OmniParser and Florence model files...",
                            f"{_cmd_quote(cmd_python_exe)} {_cmd_quote(cmd_download_script)}",
                            "if errorlevel 1 goto failed",
                            "echo.",
                            "echo Step 3/3: preparing EasyOCR model files...",
                            f"{_cmd_quote(cmd_python_exe)} {_cmd_quote(cmd_easyocr_script)}",
                            "if errorlevel 1 goto failed",
                            "echo.",
                            "echo Install completed. You can close this window after checking the output.",
                            "goto end",
                            ":failed",
                            "echo.",
                            "echo Install failed. Check the error output above.",
                            ":end",
                            "popd",
                            "pause",
                        ]
                    )
                    + "\n",
                    encoding="utf-8",
                )
                return cmd_script

            if os.name == "nt":
                cmd_script = _write_windows_installer()
                _sp.Popen(
                    ["cmd.exe", "/c", str(cmd_script)],
                    cwd=_windows_cmd_path(omniparser_dir),
                    env=env,
                    creationflags=_subprocess_new_consoleflags(),
                )
                return {
                    "ok": True,
                    "message": (
                        "已打开 Mouse Control OmniParser Installer 安装窗口。\n"
                        "下载进度和速度会在窗口中实时显示；安装结束后请按任意键关闭窗口。\n"
                        f"安装脚本: {cmd_script}"
                    ),
                }

            # Step 1: pip install deps (不需要克隆仓库，omni_server 已内置)
            ok = _run_step(
                "安装依赖 (ultralytics, transformers, gradio, rapidocr, easyocr)",
                [python_exe, "-m", "pip", "install", *pip_packages],
                600,
            )
            if not ok:
                return {"ok": False, "message": "\n".join(steps)}

            ok = _run_step(
                f"下载模型权重与 Florence 运行文件 (HF_ENDPOINT={env['HF_ENDPOINT']})",
                [python_exe, "-c", textwrap.dedent(download_code)],
                1800,
            )
            if not ok:
                return {"ok": False, "message": "\n".join(steps)}

            ok = _run_step(
                "预加载 EasyOCR 模型",
                [python_exe, "-c", textwrap.dedent(easyocr_code)],
                600,
            )
            if not ok:
                return {"ok": False, "message": "\n".join(steps)}

            steps.append("✓ 安装完成，请启用插件并使用「测试连接」确认服务在线。")
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
                        description="安装依赖并通过 hf-mirror 下载模型权重（约 2-3GB，首次需 5-10 分钟）",
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
                                "auto_start": True,
                                "server_url": DEFAULT_SERVER_URL,
                                "box_threshold": 0.03,
                                "iou_threshold": 0.1,
                                "infer_max_side": 1024,
                                "conda_python": DEFAULT_CONDA_PYTHON,
                                "omniparser_dir": DEFAULT_OMNIPARSER_DIR,
                            },
                        },
                    ),
                ],
                order=45.5,
            )
        )

        # ── 自动启动 OmniParser 后台服务 ──
        self._start_omniparser_async(plugin_root)

    def shutdown(self) -> None:
        self._stop_omniparser()
