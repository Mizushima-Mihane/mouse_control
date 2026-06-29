"""OmniParser 配置模型 — 独立于 Moondream，可无缝切换。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_SERVER_URL = "http://127.0.0.1:7862"
DEFAULT_CONDA_PYTHON = "python"
DEFAULT_OMNIPARSER_DIR = ""


@dataclass
class OmniParserConfig:
    """OmniParser 桥接配置。"""

    enabled: bool = False
    auto_start: bool = True  # 启动时自动拉起插件内置 HTTP 服务
    server_url: str = DEFAULT_SERVER_URL
    box_threshold: float = 0.03
    iou_threshold: float = 0.1
    infer_max_side: int = 1024
    # 自动启动所需路径
    conda_python: str = DEFAULT_CONDA_PYTHON  # 默认用 Shinsekai runtime Python
    omniparser_dir: str = DEFAULT_OMNIPARSER_DIR  # 留空则使用插件源码目录
    offset_x: int = 0
    offset_y: int = 0
    offset_cloud_x: int = 0
    offset_cloud_y: int = 0
    btn_close_offset: int = 20
    btn_max_offset: int = 40
    btn_min_offset: int = 80

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> OmniParserConfig:
        return cls(
            enabled=bool(d.get("enabled", False)),
            auto_start=bool(d.get("auto_start", True)),
            server_url=str(d.get("server_url", DEFAULT_SERVER_URL)),
            box_threshold=float(d.get("box_threshold", 0.03)),
            iou_threshold=float(d.get("iou_threshold", 0.1)),
            infer_max_side=int(d.get("infer_max_side", 1024)),
            conda_python=str(d.get("conda_python", DEFAULT_CONDA_PYTHON)),
            omniparser_dir=str(d.get("omniparser_dir", DEFAULT_OMNIPARSER_DIR)),
            offset_x=int(d.get("offset_x", 0)),
            offset_y=int(d.get("offset_y", 0)),
            offset_cloud_x=int(d.get("offset_cloud_x", 0)),
            offset_cloud_y=int(d.get("offset_cloud_y", 0)),
            btn_close_offset=int(d.get("btn_close_offset", 20)),
            btn_max_offset=int(d.get("btn_max_offset", 60)),
            btn_min_offset=int(d.get("btn_min_offset", 80)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "auto_start": self.auto_start,
            "server_url": self.server_url,
            "box_threshold": self.box_threshold,
            "iou_threshold": self.iou_threshold,
            "infer_max_side": self.infer_max_side,
            "conda_python": self.conda_python,
            "omniparser_dir": self.omniparser_dir,
            "offset_x": self.offset_x,
            "offset_y": self.offset_y,
            "offset_cloud_x": self.offset_cloud_x,
            "offset_cloud_y": self.offset_cloud_y,
            "btn_close_offset": self.btn_close_offset,
            "btn_max_offset": self.btn_max_offset,
            "btn_min_offset": self.btn_min_offset,
        }


def default_config_path(plugin_root: Path) -> Path:
    return plugin_root / "omniparser_config.json"


def load_config(plugin_root: Path) -> OmniParserConfig:
    path = default_config_path(plugin_root)
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return OmniParserConfig.from_dict(json.load(f))
        except (json.JSONDecodeError, KeyError, ValueError):
            pass
    cfg = OmniParserConfig()
    save_config(cfg, plugin_root)
    return cfg


def save_config(cfg: OmniParserConfig, plugin_root: Path) -> None:
    path = default_config_path(plugin_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg.to_dict(), f, ensure_ascii=False, indent=2)
