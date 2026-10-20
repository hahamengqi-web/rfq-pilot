"""Local-only PaddleOCR adapter and deterministic image extraction contracts."""

from __future__ import annotations

import hashlib
import io
import os
import base64
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import OcrConfig, load_ocr_config
from .contracts import ImageArtifact, ImageSourceKind


SUPPORTED_IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def configure_paddle_cache() -> Path:
    """Keep PaddleX downloads in this checkout unless explicitly overridden."""
    configured = os.environ.get("PADDLE_PDX_CACHE_HOME", "").strip()
    cache_home = Path(configured) if configured else _PROJECT_ROOT / ".paddlex"
    if not cache_home.is_absolute():
        cache_home = _PROJECT_ROOT / cache_home
    cache_home = cache_home.resolve()
    os.environ["PADDLE_PDX_CACHE_HOME"] = str(cache_home)
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
    os.environ.setdefault("HF_HOME", str(cache_home / "huggingface"))
    os.environ.setdefault("MODELSCOPE_HOME", str(cache_home / "modelscope"))
    os.environ.setdefault("MODELSCOPE_CACHE", str(cache_home / "modelscope" / "hub"))
    return cache_home


class OcrProcessingError(RuntimeError):
    pass


@dataclass(frozen=True)
class OcrOutput:
    text: str
    confidence: float | None
    status: str


class PaddleOcrProvider:
    """Lazy PaddleOCR wrapper supporting both v2 ``ocr`` and v3 ``predict`` APIs."""

    detection_model_name = "PP-OCRv5_mobile_det"
    recognition_model_name = "PP-OCRv5_mobile_rec"
    model_id = "paddleocr-ppocrv5-mobile"

    def __init__(self, config: OcrConfig | None = None, *, engine=None):
        self.config = config or load_ocr_config()
        self._engine = engine

    def _get_engine(self):
        if self._engine is not None:
            return self._engine
        # PaddleX reads cache settings during import.
        configure_paddle_cache().mkdir(parents=True, exist_ok=True)
        try:
            from paddleocr import PaddleOCR
        except ImportError as exc:
            raise OcrProcessingError("ocr_dependency_missing") from exc
        try:
            self._engine = PaddleOCR(
                device=self.config.device,
                text_detection_model_name=self.detection_model_name,
                text_recognition_model_name=self.recognition_model_name,
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=True,
            )
        except TypeError:
            self._engine = PaddleOCR(
                lang=self.config.language,
                use_angle_cls=True,
                use_gpu=self.config.device == "gpu",
            )
        return self._engine

    @staticmethod
    def _old_api_rows(value: Any) -> list[tuple[str, float]]:
        rows: list[tuple[str, float]] = []
        for page in value or ():
            for row in page or ():
                if (isinstance(row, (list, tuple)) and len(row) >= 2
                        and isinstance(row[1], (list, tuple)) and len(row[1]) >= 2):
                    text, score = row[1][0], row[1][1]
                    if str(text).strip():
                        rows.append((str(text).strip(), float(score)))
        return rows

    @staticmethod
    def _new_api_rows(value: Any) -> list[tuple[str, float]]:
        rows: list[tuple[str, float]] = []
        for result in value or ():
            payload = getattr(result, "json", None)
            if callable(payload):
                payload = payload()
            payload = payload or getattr(result, "res", None) or result
            if isinstance(payload, dict) and isinstance(payload.get("res"), dict):
                payload = payload["res"]
            if not isinstance(payload, dict):
                continue
            texts = payload.get("rec_texts") or payload.get("texts") or ()
            scores = payload.get("rec_scores") or payload.get("scores") or ()
            for index, text in enumerate(texts):
                if str(text).strip():
                    score = float(scores[index]) if index < len(scores) else 0.0
                    rows.append((str(text).strip(), score))
        return rows

    def recognize(self, image_bytes: bytes) -> OcrOutput:
        if not self.config.enabled:
            raise OcrProcessingError("ocr_disabled")
        try:
            from PIL import Image
            import numpy as np
            with Image.open(io.BytesIO(image_bytes)) as image:
                image.load()
                if image.width * image.height > self.config.max_image_pixels:
                    raise OcrProcessingError("ocr_image_pixel_limit_exceeded")
                array = np.asarray(image.convert("RGB"))
        except OcrProcessingError:
            raise
        except Exception as exc:
            raise OcrProcessingError("invalid_image") from exc
        engine = self._get_engine()
        try:
            if hasattr(engine, "predict"):
                rows = self._new_api_rows(engine.predict(array))
            else:
                rows = self._old_api_rows(engine.ocr(array, cls=True))
        except Exception as exc:
            raise OcrProcessingError("ocr_recognition_failed") from exc
        text = "\n".join(row[0] for row in rows).strip()
        confidence = (sum(row[1] for row in rows) / len(rows)) if rows else None
        if len(text) > self.config.max_ocr_chars_per_image:
            raise OcrProcessingError("ocr_text_limit_exceeded")
        if not text:
            return OcrOutput("", confidence, "no_text")
        status = "ready" if confidence is not None and confidence >= self.config.min_confidence else "low_confidence"
        return OcrOutput(text, confidence, status)


class QwenVisionProvider:
    """Turn product/document images into grounded, searchable Chinese text."""

    model_id = "qwen-vision"

    def __init__(self, config: OcrConfig | None = None, *, client=None):
        self.config = config or load_ocr_config()
        self._client = client
        self.model_id = self.config.vision_model or self.model_id

    def recognize(self, image_bytes: bytes) -> OcrOutput:
        prompt = (
            "你在为产品客服知识库解析产品手册图片。请只输出图片中可见且有依据的信息，"
            "用于后续检索和客服答疑。先逐项准确抄录能辨认的文字、型号、接口名、数字和单位；"
            "再简要说明图中标注、部件、箭头、步骤、指示灯状态之间明确展示的关系。"
            "如果图片含表格，先将可辨认的表头和数据行转换为 Markdown 表格，保留行列关系；"
            "保留原有单位和编号，不要补充图片之外的产品知识，不要猜测模糊内容；"
            "看不清的字段写“无法辨认”。纯装饰或没有可用文字/技术信息的图片只输出“未发现可检索的产品信息”。"
        )
        return self._recognize(image_bytes, prompt)

    def recognize_table(self, image_bytes: bytes) -> OcrOutput:
        return self._recognize(image_bytes, (
            "仅根据图片中的产品表格输出 Markdown 表格。保留原始表头、型号、数字、单位与行列对应关系。"
            "合并单元格可重复填写明确可见的值；无法辨认的单元格写“无法辨认”。"
            "不要猜测、解释或添加表外信息，不要使用代码围栏。"
        ))

    def _recognize(self, image_bytes: bytes, prompt: str) -> OcrOutput:
        if not self.config.enabled:
            raise OcrProcessingError("ocr_disabled")
        if not self.config.vision_remote_approved:
            raise OcrProcessingError("ocr_remote_transfer_not_approved")
        if not self.config.vision_api_key or not self.config.vision_base_url or not self.config.vision_model:
            raise OcrProcessingError("ocr_vision_api_not_configured")
        try:
            from PIL import Image
            with Image.open(io.BytesIO(image_bytes)) as image:
                image.load()
                if image.width * image.height > self.config.max_image_pixels:
                    raise OcrProcessingError("ocr_image_pixel_limit_exceeded")
                mime_type = Image.MIME.get(image.format, "image/png")
        except OcrProcessingError:
            raise
        except Exception as exc:
            raise OcrProcessingError("invalid_image") from exc

        encoded = base64.b64encode(image_bytes).decode("ascii")
        payload = {
            "model": self.config.vision_model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {
                        "url": f"data:{mime_type};base64,{encoded}", "detail": "high",
                    }},
                    {"type": "text", "text": prompt},
                ],
            }],
            "temperature": 0,
            "max_tokens": self.config.vision_max_tokens,
        }
        last_error: Exception | None = None
        text = ""
        for attempt in range(self.config.vision_max_attempts):
            try:
                if self._client is None:
                    import httpx
                    response = httpx.post(
                        f"{self.config.vision_base_url}/chat/completions",
                        headers={"Authorization": f"Bearer {self.config.vision_api_key}"},
                        json=payload,
                        timeout=self.config.vision_timeout_seconds,
                    )
                else:
                    response = self._client.post(
                        f"{self.config.vision_base_url}/chat/completions",
                        headers={"Authorization": f"Bearer {self.config.vision_api_key}"},
                        json=payload,
                        timeout=self.config.vision_timeout_seconds,
                    )
                response.raise_for_status()
                data = response.json()
                text = str(data["choices"][0]["message"].get("content") or "").strip()
                break
            except Exception as exc:
                last_error = exc
                if attempt + 1 < self.config.vision_max_attempts:
                    time.sleep(0.75)
        if not text and last_error is not None:
            raise OcrProcessingError("ocr_vision_recognition_failed") from last_error
        if len(text) > self.config.max_ocr_chars_per_image:
            raise OcrProcessingError("ocr_text_limit_exceeded")
        if not text or text == "未发现可检索的产品信息":
            return OcrOutput("", None, "no_text")
        # The user accepts the model output as-is; do not block indexing on review.
        return OcrOutput(text, None, "ready")


def create_ocr_provider(config: OcrConfig):
    if config.provider == "qwen_vl":
        return QwenVisionProvider(config)
    return PaddleOcrProvider(config)


def validate_image_bytes(payload: bytes, mime_type: str, config: OcrConfig) -> tuple[int, int, bytes, str]:
    if not payload:
        raise ValueError("empty_file")
    if len(payload) > config.max_image_bytes:
        raise ValueError("file_too_large")
    try:
        from PIL import Image
        with Image.open(io.BytesIO(payload)) as image:
            image.verify()
        with Image.open(io.BytesIO(payload)) as image:
            width, height = image.size
            detected = Image.MIME.get(image.format, "")
    except Exception as exc:
        raise ValueError("invalid_image") from exc
    if width * height > config.max_image_pixels:
        raise ValueError("image_pixel_limit_exceeded")
    if detected not in set(SUPPORTED_IMAGE_TYPES.values()) or detected != mime_type:
        raise ValueError("image_signature_mismatch")
    return width, height, payload, detected


def make_image_artifact(*, document_source_hash: str, image_index: int,
                        source_kind: ImageSourceKind, page_number: int | None,
                        page_image_index: int, payload: bytes, mime_type: str,
                        width: int, height: int, output: OcrOutput) -> ImageArtifact:
    image_hash = hashlib.sha256(payload).hexdigest()
    stable = "|".join((document_source_hash, source_kind.value, str(page_number or 0),
                       str(page_image_index), image_hash))
    image_id = "img-" + hashlib.sha256(stable.encode("utf-8")).hexdigest()[:32]
    return ImageArtifact(
        image_id=image_id, image_index=image_index, source_kind=source_kind,
        page_number=page_number, page_image_index=page_image_index,
        source_hash=image_hash, mime_type=mime_type, width=width, height=height,
        ocr_text=output.text, ocr_confidence=output.confidence, ocr_status=output.status,
    )


def extract_pdf_images(payload: bytes, *, scanned_pages: set[int], config: OcrConfig) -> list[dict]:
    """Render scanned pages and crop meaningful embedded images in deterministic order."""
    try:
        import pdfplumber
    except ImportError as exc:
        raise OcrProcessingError("ocr_pdf_renderer_missing") from exc
    rows: list[dict] = []
    try:
        with pdfplumber.open(io.BytesIO(payload)) as pdf:
            for page_number, page in enumerate(pdf.pages, 1):
                if page_number in scanned_pages:
                    rendered = page.to_image(resolution=config.pdf_dpi, antialias=True).original
                    buffer = io.BytesIO(); rendered.save(buffer, format="PNG")
                    rows.append({"source_kind": ImageSourceKind.PDF_PAGE,
                                 "page_number": page_number, "page_image_index": 1,
                                 "payload": buffer.getvalue(), "mime_type": "image/png",
                                 "width": rendered.width, "height": rendered.height})
                    continue
                kept = 0
                for raw in page.images:
                    width = int(abs(float(raw.get("x1", 0)) - float(raw.get("x0", 0))))
                    height = int(abs(float(raw.get("bottom", 0)) - float(raw.get("top", 0))))
                    if width < config.min_embedded_width or height < config.min_embedded_height:
                        continue
                    bbox = (float(raw["x0"]), float(raw["top"]),
                            float(raw["x1"]), float(raw["bottom"]))
                    cropped = page.crop(bbox, strict=False).to_image(
                        resolution=config.pdf_dpi, antialias=True).original
                    buffer = io.BytesIO(); cropped.save(buffer, format="PNG")
                    kept += 1
                    rows.append({"source_kind": ImageSourceKind.PDF_EMBEDDED,
                                 "page_number": page_number, "page_image_index": kept,
                                 "payload": buffer.getvalue(), "mime_type": "image/png",
                                 "width": cropped.width, "height": cropped.height})
                    if len(rows) > config.max_images_per_document:
                        raise OcrProcessingError("ocr_image_count_limit_exceeded")
    except OcrProcessingError:
        raise
    except Exception as exc:
        raise OcrProcessingError("ocr_pdf_image_extraction_failed") from exc
    return rows
