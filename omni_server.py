"""OmniParser 独立 HTTP 服务 — 内嵌于 mouse_control 插件内。

不需要独立安装 OmniParser 仓库，权重放在同目录 omniparser_weights/ 下。
接口: POST /process  body=PNG bytes  → 返回 JSON 元素列表
"""

from __future__ import annotations

import io
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from PIL import Image
from _omniparser_util import (
    check_ocr_box,
    get_caption_model_processor,
    get_som_labeled_img,
    get_yolo_model,
)

# 权重放在插件目录下
_WEIGHTS = _ROOT / "omniparser_weights"

# ── 加载模型（启动时一次性完成）───────────────────────────────
print("Loading YOLO...", flush=True)
yolo_model = get_yolo_model(str(_WEIGHTS / "icon_detect" / "model.pt"))
print("Loading Florence caption...", flush=True)
caption_processor = get_caption_model_processor(
    model_name="florence2",
    model_name_or_path=str(_WEIGHTS / "icon_caption_florence"),
)
print(f"Models loaded. Ready on http://127.0.0.1:7862", flush=True)


def process_image(image_bytes: bytes, box_threshold: float = 0.03, iou_threshold: float = 0.1):
    """处理一张截图，返回结构化 UI 元素列表。"""
    image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    (text_list, ocr_bboxes), _ = check_ocr_box(
        image,
        display_img=True,
        output_bb_format="xyxy",
        easyocr_args={"paragraph": False, "text_threshold": 0.8},
    )
    _, label_coords, parsed = get_som_labeled_img(
        image,
        yolo_model,
        BOX_TRESHOLD=box_threshold,
        output_coord_in_ratio=True,
        ocr_bbox=ocr_bboxes,
        draw_bbox_config={},
        caption_model_processor=caption_processor,
        ocr_text=text_list,
        iou_threshold=iou_threshold,
        imgsz=640,
    )
    elements = []
    for item in parsed:
        if isinstance(item, dict) and "bbox" in item:
            bbox = item["bbox"]
            x1, y1, x2, y2 = bbox[0], bbox[1], bbox[2], bbox[3]
            elements.append({
                "type": item.get("type", "icon"),
                "text": str(item.get("content", ""))[:100],
                "x_pct": round((x1 + x2) / 2, 5),
                "y_pct": round((y1 + y2) / 2, 5),
                "bbox": [round(v, 5) for v in bbox],
            })
    return elements


class OmniHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/process":
            self.send_error(404)
            return
        try:
            content_len = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_len)
            t0 = time.time()
            elements = process_image(body)
            elapsed = time.time() - t0
            resp = {"ok": True, "count": len(elements), "elapsed": round(elapsed, 2), "elements": elements[:50]}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(resp, ensure_ascii=False).encode())
        except Exception as e:
            import traceback as _tb
            _tb.print_exc()
            self.send_response(500)
            self.end_headers()
            self.wfile.write(json.dumps({"ok": False, "error": str(e), "traceback": _tb.format_exc()}).encode())

    def do_GET(self):
        if self.path == "/":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OmniParser server running")
        elif self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
        else:
            self.send_error(404)

    def log_message(self, fmt, *args):
        pass  # suppress logs


if __name__ == "__main__":
    port = 7862
    server = HTTPServer(("127.0.0.1", port), OmniHandler)
    print(f"Listening on http://127.0.0.1:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Shutting down...")
        server.shutdown()
