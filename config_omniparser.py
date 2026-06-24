"""OmniParser 配置模型 — 独立于 Moondream，可无缝切换。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class OmniParserConfig:
    """OmniParser 桥接配置。"""

    enabled: bool = False
    auto_start: bool = True           # 启动时自动拉起 Gradio 服务
    server_url: str = "http://127.0.0.1:7862"
    box_threshold: float = 0.03
    iou_threshold: float = 0.1
    infer_max_side: int = 1024
    # 自动启动所需路径
    conda_python: str = "python"  # 默认用 Shinsekai runtime Python
    omniparser_dir: str = ""  # 留空则自动推断为 <plugin_root>/../../../OmniParser

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> OmniParserConfig:
        return cls(
            enabled=bool(d.get("enabled", False)),
            auto_start=bool(d.get("auto_start", True)),
            server_url=str(d.get("server_url", "http://127.0.0.1:7861")),
            box_threshold=float(d.get("box_threshold", 0.03)),
            iou_threshold=float(d.get("iou_threshold", 0.1)),
            infer_max_side=int(d.get("infer_max_side", 1024)),
            conda_python=str(d.get("conda_python", "F:/minicond/envs/omni/Scripts/python.exe")),
            omniparser_dir=str(d.get("omniparser_dir", "")),
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
