"""OmniParser 设置页 —— 配置本地 OmniParser 服务连接参数。

参照 moondream_vision/settings_tab 的布局风格。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from plugins.mouse_control.config_omniparser import (
    OmniParserConfig,
    default_config_path,
    load_config,
    save_config,
)
from sdk.plugin_host_context import PluginSettingsUIContext


class OmniParserSettingsTab(QWidget):
    """设置 → 鼠标控制 → OmniParser 识屏"""

    def __init__(
        self, plg: PluginSettingsUIContext, plugin_root: Path, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._plg = plg
        self._root = plugin_root
        self._path = default_config_path(plugin_root)
        self._cfg = load_config(plugin_root)
        self._setup_ui()
        self._load_to_ui()

    # ── UI 构建 ──────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(12)

        # ── 启用 ──
        self._enabled_cb = QCheckBox("启用 OmniParser 视觉定位（替代 Moondream point/detect）")
        self._enabled_cb.setToolTip(
            "开启后，mouse_omniparser_* 系列工具可用。\n"
            "需先在外部启动 OmniParser Gradio 服务。"
        )
        layout.addWidget(self._enabled_cb)

        # ── 服务连接 ──
        grp_conn = QGroupBox("服务连接")
        fl_conn = QFormLayout(grp_conn)
        self._server_url_le = QLineEdit()
        self._server_url_le.setPlaceholderText("http://127.0.0.1:7861")
        fl_conn.addRow("服务器地址:", self._server_url_le)

        btn_row = QHBoxLayout()
        self._test_btn = QPushButton("测试连接")
        self._test_btn.clicked.connect(self._on_test_connection)
        btn_row.addWidget(self._test_btn)
        self._status_label = QLabel("")
        self._status_label.setWordWrap(True)
        btn_row.addWidget(self._status_label)
        btn_row.addStretch()
        fl_conn.addRow("", btn_row)
        layout.addWidget(grp_conn)

        # ── 检测参数 ──
        grp_detect = QGroupBox("检测参数")
        fl_detect = QFormLayout(grp_detect)

        self._box_threshold_sb = QDoubleSpinBox()
        self._box_threshold_sb.setRange(0.001, 0.5)
        self._box_threshold_sb.setDecimals(4)
        self._box_threshold_sb.setSingleStep(0.005)
        self._box_threshold_sb.setToolTip(
            "YOLO 图标检测置信度阈值。越低检出越多（但可能有误检），\n"
            "越高检出越少（更精准）。默认 0.03。"
        )
        fl_detect.addRow("检测阈值:", self._box_threshold_sb)

        self._iou_threshold_sb = QDoubleSpinBox()
        self._iou_threshold_sb.setRange(0.01, 1.0)
        self._iou_threshold_sb.setDecimals(2)
        self._iou_threshold_sb.setSingleStep(0.05)
        self._iou_threshold_sb.setToolTip(
            "NMS 去重 IOU 阈值。越高保留越多重叠框，越低去重越激进。默认 0.1。"
        )
        fl_detect.addRow("IOU 阈值:", self._iou_threshold_sb)

        self._infer_max_side_sb = QSpinBox()
        self._infer_max_side_sb.setRange(0, 4096)
        self._infer_max_side_sb.setSingleStep(128)
        self._infer_max_side_sb.setToolTip(
            "推理前图片最长边缩放。0 = 不缩放（最清晰但慢），"
            "1024 = 平衡速度与精度。"
        )
        fl_detect.addRow("推理最长边:", self._infer_max_side_sb)

        layout.addWidget(grp_detect)

        # ── 启动指引 ──
        grp_help = QGroupBox("启动 OmniParser 服务")
        help_layout = QVBoxLayout(grp_help)
        help_text = QLabel(
            "在外部终端执行以下命令启动 OmniParser Gradio 服务：\n\n"
            '<span style="background:#1e1e1e;color:#9cdcfe;padding:4px 8px;">'
            "cd OmniParser<br>"
            "F:\\minicond\\envs\\omni\\Scripts\\python.exe gradio_demo.py"
            "</span>\n\n"
            "看到 <i>Running on local URL: http://127.0.0.1:7861</i> 即启动成功。"
        )
        help_text.setWordWrap(True)
        help_text.setTextFormat(Qt.TextFormat.RichText)
        help_layout.addWidget(help_text)
        layout.addWidget(grp_help)

        # ── 保存/重置 ──
        btn_row2 = QHBoxLayout()
        self._save_btn = QPushButton("保存设置")
        self._save_btn.clicked.connect(self._on_save)
        btn_row2.addWidget(self._save_btn)
        self._reset_btn = QPushButton("恢复默认")
        self._reset_btn.clicked.connect(self._on_reset)
        btn_row2.addWidget(self._reset_btn)
        btn_row2.addStretch()
        layout.addLayout(btn_row2)

        layout.addStretch()

    # ── 数据绑定 ──────────────────────────────────────────────────

    def _load_to_ui(self) -> None:
        cfg = self._cfg
        self._enabled_cb.setChecked(cfg.enabled)
        self._server_url_le.setText(cfg.server_url)
        self._box_threshold_sb.setValue(cfg.box_threshold)
        self._iou_threshold_sb.setValue(cfg.iou_threshold)
        self._infer_max_side_sb.setValue(cfg.infer_max_side)

    def _ui_to_cfg(self) -> None:
        self._cfg.enabled = self._enabled_cb.isChecked()
        self._cfg.server_url = self._server_url_le.text().strip() or "http://127.0.0.1:7861"
        self._cfg.box_threshold = self._box_threshold_sb.value()
        self._cfg.iou_threshold = self._iou_threshold_sb.value()
        self._cfg.infer_max_side = self._infer_max_side_sb.value()

    # ── 事件处理 ──────────────────────────────────────────────────

    def _on_save(self) -> None:
        self._ui_to_cfg()
        save_config(self._cfg, self._root)
        QMessageBox.information(self, "OmniParser", "设置已保存。")

    def _on_reset(self) -> None:
        self._cfg = OmniParserConfig()
        self._load_to_ui()
        save_config(self._cfg, self._root)
        QMessageBox.information(self, "OmniParser", "已恢复默认设置。")

    def _on_test_connection(self) -> None:
        self._ui_to_cfg()
        url = self._cfg.server_url.rstrip("/")
        try:
            from urllib.request import urlopen
            resp = urlopen(f"{url}/", timeout=5)
            self._status_label.setText("✓ 服务在线")
            self._status_label.setStyleSheet("color: green; font-weight: bold;")
        except Exception as e:
            self._status_label.setText(f"✗ 无法连接: {e}")
            self._status_label.setStyleSheet("color: red;")
