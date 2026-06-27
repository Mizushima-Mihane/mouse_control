from __future__ import annotations

import json
import re
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image


SOURCE_DIR = Path(__file__).resolve().parents[1]
if (SOURCE_DIR / "plugin.py").is_file():
    PROJECT_ROOT = SOURCE_DIR.parent.parent
    PLUGIN_DIR = SOURCE_DIR

    plugins_pkg = sys.modules.setdefault("plugins", types.ModuleType("plugins"))
    plugins_pkg.__path__ = [str(SOURCE_DIR.parent)]
    mouse_pkg = sys.modules.setdefault("plugins.mouse_control", types.ModuleType("plugins.mouse_control"))
    mouse_pkg.__path__ = [str(SOURCE_DIR)]
else:
    PROJECT_ROOT = Path(__file__).resolve().parents[3]
    PLUGIN_DIR = PROJECT_ROOT / "plugins" / "mouse_control"

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _install_sdk_stubs() -> None:
    """Allow source-repo tests to import plugin modules without Shinsekai SDK."""
    if "sdk.tool_registry" in sys.modules:
        return

    sdk_pkg = sys.modules.setdefault("sdk", types.ModuleType("sdk"))
    sdk_pkg.__path__ = []

    tool_registry = types.ModuleType("sdk.tool_registry")

    class ToolNotReady(RuntimeError):
        pass

    def tool(func=None, **_kwargs):
        def decorator(inner):
            return inner

        return decorator(func) if callable(func) else decorator

    tool_registry.ToolNotReady = ToolNotReady
    tool_registry.tool = tool
    sys.modules["sdk.tool_registry"] = tool_registry

    plugin_mod = types.ModuleType("sdk.plugin")

    class PluginBase:
        pass

    plugin_mod.PluginBase = PluginBase
    sys.modules["sdk.plugin"] = plugin_mod

    host_context = types.ModuleType("sdk.plugin_host_context")
    host_context.PluginHostContext = type("PluginHostContext", (), {})
    host_context.PluginSettingsUIContext = type("PluginSettingsUIContext", (), {})
    sys.modules["sdk.plugin_host_context"] = host_context

    register_mod = types.ModuleType("sdk.register")
    register_mod.PluginCapabilityRegistry = type("PluginCapabilityRegistry", (), {})
    sys.modules["sdk.register"] = register_mod

    types_mod = types.ModuleType("sdk.types")

    class _Contribution:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    types_mod.FrontendConfigAction = _Contribution
    types_mod.FrontendConfigContribution = _Contribution
    types_mod.ToolsTabContribution = _Contribution
    sys.modules["sdk.types"] = types_mod


_install_sdk_stubs()


class OmniParserContractTests(unittest.TestCase):
    def test_defaults_match_embedded_server(self) -> None:
        from plugins.mouse_control.config_omniparser import OmniParserConfig

        cfg = OmniParserConfig.from_dict({}).to_dict()
        self.assertEqual(cfg["server_url"], "http://127.0.0.1:7862")
        self.assertEqual(cfg["conda_python"], "python")
        self.assertEqual(cfg["omniparser_dir"], "")

    def test_no_legacy_port_or_machine_path_in_user_facing_code(self) -> None:
        checked = {
            name: (PLUGIN_DIR / name).read_text(encoding="utf-8")
            for name in ("plugin.py", "llm_tool.py", "settings_tab.py", "omniparser_config.json")
        }
        for name, text in checked.items():
            self.assertNotIn("127.0.0.1:7861", text, name)
            self.assertIsNone(re.search(r"(?<![A-Za-z])[A-Za-z]:[\\/][^\"'\n\r]+", text), name)
        self.assertNotIn('.replace(":7861", ":7862")', checked["llm_tool.py"])

    def test_llm_tool_uses_data_config_and_sends_thresholds(self) -> None:
        text = (PLUGIN_DIR / "llm_tool.py").read_text(encoding="utf-8")
        self.assertIn("data/plugins/com.shinsekai.mouse_control/omniparser_config.json", text)
        self.assertIn('"box_threshold"', text)
        self.assertIn('"iou_threshold"', text)
        self.assertIn('"infer_max_side"', text)

    def test_installer_uses_hf_mirror_and_checks_subprocesses(self) -> None:
        text = (PLUGIN_DIR / "plugin.py").read_text(encoding="utf-8")
        self.assertIn('"HF_ENDPOINT"', text)
        self.assertIn("https://hf-mirror.com", text)
        self.assertIn("returncode", text)
        self.assertIn("_run_step", text)

    def test_windows_installer_uses_visible_console_with_pause_and_progress(self) -> None:
        text = (PLUGIN_DIR / "plugin.py").read_text(encoding="utf-8")
        self.assertIn("CREATE_NEW_CONSOLE", text)
        self.assertIn("Mouse Control OmniParser Installer", text)
        self.assertIn("cmd.exe", text)
        self.assertIn("pause", text)
        self.assertIn("MiB/s", text)

    def test_windows_cmd_path_strips_extended_length_prefix(self) -> None:
        from plugins.mouse_control import plugin

        self.assertEqual(
            plugin._windows_cmd_path(r"\\?\F:\Alter Ego\Shinsekai\runtime\python.exe"),
            r"F:\Alter Ego\Shinsekai\runtime\python.exe",
        )

    def test_windows_installer_normalizes_cmd_paths(self) -> None:
        text = (PLUGIN_DIR / "plugin.py").read_text(encoding="utf-8")
        self.assertIn("_windows_cmd_path(omniparser_dir)", text)
        self.assertIn("_windows_cmd_path(python_exe)", text)
        self.assertIn("_windows_cmd_path(str(download_script))", text)
        self.assertIn("_windows_cmd_path(str(easyocr_script))", text)
        self.assertIn("cwd=_windows_cmd_path(omniparser_dir)", text)

    def test_background_installer_tolerates_non_utf8_output(self) -> None:
        text = (PLUGIN_DIR / "plugin.py").read_text(encoding="utf-8")
        self.assertIn('encoding="utf-8"', text)
        self.assertIn('errors="replace"', text)

    def test_source_config_is_portable(self) -> None:
        cfg = json.loads((PLUGIN_DIR / "omniparser_config.json").read_text(encoding="utf-8"))
        self.assertEqual(cfg["server_url"], "http://127.0.0.1:7862")
        self.assertEqual(cfg["conda_python"], "python")
        self.assertEqual(cfg["omniparser_dir"], "")

    def test_runtime_python_finds_linux_embedded_layout(self) -> None:
        from plugins.mouse_control import plugin

        old_project_root = plugin._project_root
        try:
            with TemporaryDirectory() as td:
                root = Path(td)
                python_path = root / "runtime" / "bin" / "python"
                python_path.parent.mkdir(parents=True)
                python_path.write_text("#!/usr/bin/env python\n", encoding="utf-8")
                plugin._project_root = lambda: root

                self.assertEqual(plugin._runtime_python(), python_path.resolve())
        finally:
            plugin._project_root = old_project_root

    def test_relative_plugin_data_root_resolves_from_project_root(self) -> None:
        from plugins.mouse_control.plugin import _resolve_plugin_data_root

        root = _resolve_plugin_data_root(Path("data/plugins/com.shinsekai.mouse_control"))
        self.assertEqual(root, PROJECT_ROOT / "data" / "plugins" / "com.shinsekai.mouse_control")
        self.assertTrue(root.is_absolute())

    def test_initialize_does_not_block_on_omniparser_start(self) -> None:
        from plugins.mouse_control.plugin import MouseControlPlugin

        class DummyRegister:
            def register_tools_tab(self, contribution) -> None:
                self.tools_tab = contribution

            def register_frontend_config_page(self, contribution) -> None:
                self.config_page = contribution

        plugin = MouseControlPlugin()
        entered = threading.Event()
        release = threading.Event()

        def slow_start(plugin_root: Path) -> None:
            entered.set()
            release.wait(2)

        plugin._start_omniparser = slow_start  # type: ignore[method-assign]
        start = time.perf_counter()
        plugin.initialize(DummyRegister(), Path("data/plugins/com.shinsekai.mouse_control"), object())
        elapsed = time.perf_counter() - start
        try:
            self.assertLess(elapsed, 0.5)
            self.assertTrue(entered.wait(1))
        finally:
            release.set()

    def test_overlap_filter_keeps_dict_shape_without_ocr(self) -> None:
        from plugins.mouse_control._omniparser_util import remove_overlap_new

        boxes = [
            {"type": "icon", "bbox": [0.1, 0.1, 0.2, 0.2], "interactivity": True, "content": None}
        ]
        filtered = remove_overlap_new(boxes=boxes, iou_threshold=0.1, ocr_bbox=[])
        self.assertEqual(filtered[0]["bbox"], boxes[0]["bbox"])
        self.assertIsNone(filtered[0]["content"])

    def test_text_ocr_falls_back_when_moondream_boxes_api_is_absent(self) -> None:
        from plugins.mouse_control import llm_tool

        old_modules = {
            name: sys.modules.get(name)
            for name in ("plugins.moondream_vision.chinese_ocr", "rapidocr_onnxruntime")
        }
        old_get_pg = llm_tool._get_pg
        old_error = llm_tool._OCR_IMPORT_ERROR

        class FakePg:
            def screenshot(self):
                return Image.new("RGB", (200, 100), "white")

        class FakeRapidOCR:
            def __call__(self, image):
                return [
                    ([[10, 20], [110, 20], [110, 60], [10, 60]], "登录 设置", 0.99)
                ], None

        try:
            # Current moondream_vision.chinese_ocr exposes text-only APIs, not ocr_image_with_boxes.
            sys.modules["plugins.moondream_vision.chinese_ocr"] = types.ModuleType(
                "plugins.moondream_vision.chinese_ocr"
            )
            rapidocr_module = types.ModuleType("rapidocr_onnxruntime")
            rapidocr_module.RapidOCR = lambda: FakeRapidOCR()
            sys.modules["rapidocr_onnxruntime"] = rapidocr_module
            llm_tool._get_pg = lambda: FakePg()
            llm_tool._OCR_IMPORT_ERROR = None

            result = llm_tool._ocr_find_text("登录")

            self.assertEqual(result[0]["text"], "登录 设置")
            self.assertEqual(result[0]["x_pixel"], 60)
            self.assertEqual(result[0]["y_pixel"], 40)
        finally:
            for name, module in old_modules.items():
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module
            llm_tool._get_pg = old_get_pg
            llm_tool._OCR_IMPORT_ERROR = old_error

    def test_omniparser_ocr_uses_rapidocr_for_text_boxes(self) -> None:
        from plugins.mouse_control import _omniparser_util as util

        old_module = sys.modules.get("rapidocr_onnxruntime")
        old_easyocr = util._get_easyocr_reader
        old_rapidocr = getattr(util, "_rapidocr_ocr", None)

        class FakeRapidOCR:
            def __call__(self, image):
                return [
                    ([[10, 20], [110, 20], [110, 60], [10, 60]], "登录", 0.99)
                ], None

        def fail_easyocr():
            raise RuntimeError("EasyOCR should not be used for Chinese text boxes")

        try:
            rapidocr_module = types.ModuleType("rapidocr_onnxruntime")
            rapidocr_module.RapidOCR = lambda: FakeRapidOCR()
            sys.modules["rapidocr_onnxruntime"] = rapidocr_module
            util._get_easyocr_reader = fail_easyocr
            if hasattr(util, "_rapidocr_ocr"):
                util._rapidocr_ocr = None

            (text, boxes), _ = util.check_ocr_box(
                Image.new("RGB", (200, 100), "white"),
                display_img=False,
                output_bb_format="xyxy",
                easyocr_args={"text_threshold": 0.8},
            )

            self.assertEqual(text, ["登录"])
            self.assertEqual([list(box) for box in boxes], [[10, 20, 110, 60]])
        finally:
            if old_module is None:
                sys.modules.pop("rapidocr_onnxruntime", None)
            else:
                sys.modules["rapidocr_onnxruntime"] = old_module
            util._get_easyocr_reader = old_easyocr
            if hasattr(util, "_rapidocr_ocr"):
                util._rapidocr_ocr = old_rapidocr

    def test_text_tools_accept_common_lookup_aliases(self) -> None:
        from plugins.mouse_control import llm_tool

        old_find = llm_tool._ocr_find_text
        try:
            llm_tool._ocr_find_text = lambda lookup: [
                {"text": f"{lookup} 设置", "x_pct": 0.25, "y_pct": 0.5, "x_pixel": 100, "y_pixel": 200}
            ]

            result = llm_tool.mouse_find_text(text="登录")

            self.assertEqual(result["lookup"], "登录")
            self.assertEqual(result["best"]["text"], "登录 设置")
        finally:
            llm_tool._ocr_find_text = old_find

    def test_omniparser_click_accepts_common_lookup_aliases(self) -> None:
        from plugins.mouse_control import llm_tool

        old_locate = llm_tool.mouse_omniparser_locate
        old_get_pg = llm_tool._get_pg
        old_to_pixels = llm_tool._to_pixels
        old_clamp = llm_tool._clamp
        old_interrupt = llm_tool._check_user_interrupt
        clicked: list[dict[str, object]] = []

        class FakePg:
            def click(self, **kwargs):
                clicked.append(kwargs)

        try:
            llm_tool.mouse_omniparser_locate = lambda: {
                "elements": [{"type": "text", "text": "登录 设置", "x_pct": 0.25, "y_pct": 0.5}]
            }
            llm_tool._get_pg = lambda: FakePg()
            llm_tool._to_pixels = lambda fx, fy: (100, 200)
            llm_tool._clamp = lambda x, y: (x, y)
            llm_tool._check_user_interrupt = lambda pg: None

            result = llm_tool.mouse_omniparser_click(query="登录")

            self.assertEqual(result["lookup"], "登录")
            self.assertEqual(result["matched"], "登录 设置")
            self.assertEqual(clicked[0]["x"], 100)
        finally:
            llm_tool.mouse_omniparser_locate = old_locate
            llm_tool._get_pg = old_get_pg
            llm_tool._to_pixels = old_to_pixels
            llm_tool._clamp = old_clamp
            llm_tool._check_user_interrupt = old_interrupt

    def test_save_values_stops_omniparser_when_disabled(self) -> None:
        from plugins.mouse_control.plugin import MouseControlPlugin

        class DummyRegister:
            def register_tools_tab(self, contribution) -> None:
                self.tools_tab = contribution

            def register_frontend_config_page(self, contribution) -> None:
                self.config_page = contribution

        with TemporaryDirectory() as td:
            plugin = MouseControlPlugin()
            stopped = threading.Event()
            plugin._stop_omniparser = stopped.set  # type: ignore[method-assign]
            register = DummyRegister()
            plugin.initialize(register, Path(td), object())

            register.config_page.save_values({"enabled": False, "auto_start": False})
            self.assertTrue(stopped.is_set())

    def test_omniparser_pid_file_roundtrip(self) -> None:
        from plugins.mouse_control.plugin import (
            _clear_omniparser_pid,
            _read_omniparser_pid,
            _write_omniparser_pid,
        )

        with TemporaryDirectory() as td:
            root = Path(td)
            _write_omniparser_pid(root, 12345)
            self.assertEqual(_read_omniparser_pid(root), 12345)
            _clear_omniparser_pid(root)
            self.assertIsNone(_read_omniparser_pid(root))

    def test_omniparser_returns_boxes_when_icon_caption_ooms(self) -> None:
        from plugins.mouse_control import _omniparser_util as util

        old_predict = util.predict_yolo
        old_caption = util.get_parsed_content_icon

        class FakeConfig:
            model_type = "florence2"

        class FakeModel:
            config = FakeConfig()

        try:
            util.predict_yolo = lambda **kwargs: (
                util.torch.tensor([[50.0, 50.0, 80.0, 80.0]]),
                util.torch.tensor([0.9]),
                ["0"],
            )

            def raise_oom(*args, **kwargs):
                raise RuntimeError("DefaultCPUAllocator: not enough memory")

            util.get_parsed_content_icon = raise_oom
            _image, _coords, parsed = util.get_som_labeled_img(
                Image.new("RGB", (100, 100), "white"),
                model=object(),
                ocr_bbox=[[10, 10, 40, 30]],
                ocr_text=["登录"],
                caption_model_processor={"model": FakeModel(), "processor": object()},
                draw_bbox_config={},
                use_local_semantics=True,
                batch_size=4,
            )

            self.assertEqual(parsed[0]["content"], "登录")
            self.assertEqual(parsed[1]["type"], "icon")
            self.assertEqual(parsed[1]["content"], "")
        finally:
            util.predict_yolo = old_predict
            util.get_parsed_content_icon = old_caption


if __name__ == "__main__":
    unittest.main()
