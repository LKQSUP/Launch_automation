"""Local OCR and template-matching vision engine (CPU-only, no cloud APIs)."""

from __future__ import annotations

import logging
import re
import threading
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

Point = Tuple[int, int]
Bounds = Tuple[int, int, int, int]  # x1, y1, x2, y2


class LocalVisionEngine:
    """EasyOCR + OpenCV icon matcher for on-device UI perception.

    All processing runs locally on CPU. EasyOCR is loaded lazily and shared
    across instances via a process-wide singleton reader.
    """

    _reader = None
    _reader_lock = threading.Lock()
    _reader_failed = False

    def __init__(self, languages: Optional[List[str]] = None, gpu: bool = False) -> None:
        """Initialize the vision engine.

        Args:
            languages: EasyOCR language codes (default: English).
            gpu: Whether to attempt GPU acceleration (default False = CPU).
        """
        self.languages = languages or ["en"]
        self.gpu = gpu

    @classmethod
    def _get_reader(cls, languages: List[str], gpu: bool):
        """Lazily construct a shared EasyOCR reader (expensive to load)."""
        if cls._reader_failed:
            return None
        if cls._reader is not None:
            return cls._reader
        with cls._reader_lock:
            if cls._reader is not None:
                return cls._reader
            try:
                import easyocr  # type: ignore

                cls._reader = easyocr.Reader(languages, gpu=gpu)
                logger.info("EasyOCR reader initialized (gpu=%s, langs=%s)", gpu, languages)
            except Exception as exc:
                cls._reader_failed = True
                logger.warning("EasyOCR unavailable: %s", exc)
                cls._reader = None
        return cls._reader

    @staticmethod
    def _load_image(path: str | Path):
        """Load an image with OpenCV (BGR)."""
        import cv2

        image = cv2.imread(str(path))
        if image is None:
            raise FileNotFoundError(f"Unable to load image: {path}")
        return image

    @staticmethod
    def _canonical(text: str) -> str:
        return re.sub(r"\s+", " ", (text or "")).strip().lower()

    def find_text_bounds(
        self,
        screenshot_path: str | Path,
        target_text: str,
        confidence: float = 0.4,
    ) -> Optional[Point]:
        """Locate ``target_text`` on a screenshot and return its center ``(X, Y)``.

        Args:
            screenshot_path: Path to a PNG/JPEG screenshot.
            target_text: Text to search for (case-insensitive substring match).
            confidence: Minimum EasyOCR confidence (0–1).

        Returns:
            Center coordinates of the best matching text box, or ``None``.
        """
        reader = self._get_reader(self.languages, self.gpu)
        if reader is None:
            return None

        try:
            results = reader.readtext(str(screenshot_path))
        except Exception as exc:
            logger.warning("OCR read failed for %s: %s", screenshot_path, exc)
            return None

        target = self._canonical(target_text)
        best: Optional[Tuple[float, Point]] = None

        for bbox, text, score in results:
            if score < confidence:
                continue
            if target not in self._canonical(text):
                continue
            xs = [int(p[0]) for p in bbox]
            ys = [int(p[1]) for p in bbox]
            center: Point = ((min(xs) + max(xs)) // 2, (min(ys) + max(ys)) // 2)
            if best is None or score > best[0]:
                best = (float(score), center)

        return best[1] if best else None

    def find_text_bounds_rect(
        self,
        screenshot_path: str | Path,
        target_text: str,
        confidence: float = 0.4,
    ) -> Optional[Bounds]:
        """Same as :meth:`find_text_bounds` but returns the full bounding box."""
        reader = self._get_reader(self.languages, self.gpu)
        if reader is None:
            return None

        try:
            results = reader.readtext(str(screenshot_path))
        except Exception as exc:
            logger.warning("OCR read failed for %s: %s", screenshot_path, exc)
            return None

        target = self._canonical(target_text)
        best: Optional[Tuple[float, Bounds]] = None

        for bbox, text, score in results:
            if score < confidence:
                continue
            if target not in self._canonical(text):
                continue
            xs = [int(p[0]) for p in bbox]
            ys = [int(p[1]) for p in bbox]
            rect: Bounds = (min(xs), min(ys), max(xs), max(ys))
            if best is None or score > best[0]:
                best = (float(score), rect)

        return best[1] if best else None

    def find_icon_bounds(
        self,
        screenshot_path: str | Path,
        template_path: str | Path,
        threshold: float = 0.72,
    ) -> Optional[Point]:
        """Locate an icon via multi-scale OpenCV template matching; return center ``(X, Y)``.

        Args:
            screenshot_path: Full-screen capture path.
            template_path: Small template image of the icon to find.
            threshold: Minimum normalized correlation score (0–1).

        Returns:
            Center coordinates of the best match above ``threshold``, or ``None``.
        """
        try:
            import cv2
            import numpy as np
        except ImportError as exc:
            logger.warning("OpenCV unavailable for icon match: %s", exc)
            return None

        try:
            haystack = self._load_image(screenshot_path)
            needle = self._load_image(template_path)
        except FileNotFoundError as exc:
            logger.warning("%s", exc)
            return None

        best_val = -1.0
        best_center: Optional[Point] = None
        # Scale template to match different tablet DPI / crop sizes.
        for scale in (0.45, 0.55, 0.65, 0.75, 0.85, 1.0, 1.15, 1.3, 1.5):
            try:
                resized = cv2.resize(needle, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
            except Exception:
                continue
            if resized.shape[0] < 8 or resized.shape[1] < 8:
                continue
            if resized.shape[0] >= haystack.shape[0] or resized.shape[1] >= haystack.shape[1]:
                continue
            try:
                result = cv2.matchTemplate(haystack, resized, cv2.TM_CCOEFF_NORMED)
                _, max_val, _, max_loc = cv2.minMaxLoc(result)
            except Exception:
                continue
            if float(max_val) > best_val:
                best_val = float(max_val)
                h, w = resized.shape[:2]
                best_center = (int(max_loc[0] + w / 2), int(max_loc[1] + h / 2))

        if best_center is None or best_val < threshold:
            logger.info("Icon match best=%.3f threshold=%.3f", best_val, threshold)
            return None
        logger.info("Icon match hit score=%.3f at %s", best_val, best_center)
        return best_center

    def find_intelligent_diagnose(
        self,
        screenshot_path: str | Path,
        template_path: str | Path | None = None,
        text_confidence: float = 0.35,
        icon_threshold: float = 0.70,
    ) -> Optional[Tuple[Point, str]]:
        """Find the Intelligent Diagnose control by text first, then icon template.

        Returns:
            ``((x, y), method)`` where method is ``text`` or ``icon``, or ``None``.
        """
        for label in ("Intelligent Diagnose", "Intelligent Diagnosis", "Inttelligent Diagnose"):
            point = self.find_text_bounds(screenshot_path, label, confidence=text_confidence)
            if point:
                return point, "text"

        tpl = Path(template_path) if template_path else (
            Path(__file__).resolve().parent.parent / "assets" / "templates" / "intelligent_diagnose.png"
        )
        if tpl.exists():
            point = self.find_icon_bounds(screenshot_path, tpl, threshold=icon_threshold)
            if point:
                return point, "icon"
        return None

    def extract_dtc_codes(
        self,
        screenshot_path: str | Path,
        confidence: float = 0.35,
    ) -> List[Tuple[str, str]]:
        """Extract OBD-style DTC codes (P/B/C/U + 4 digits) from a screenshot.

        Returns:
            List of ``(code, nearby_description)`` tuples.
        """
        reader = self._get_reader(self.languages, self.gpu)
        if reader is None:
            return []

        try:
            results = reader.readtext(str(screenshot_path))
        except Exception as exc:
            logger.warning("OCR DTC extract failed: %s", exc)
            return []

        dtc_pattern = re.compile(r"\b([PBCU][0-9A-F]{4})\b", re.IGNORECASE)
        found: List[Tuple[str, str]] = []
        seen: set[str] = set()

        for _, text, score in results:
            if score < confidence:
                continue
            for match in dtc_pattern.finditer(text):
                code = match.group(1).upper()
                if code in seen:
                    continue
                seen.add(code)
                desc = text.strip()
                found.append((code, desc))

        return found

    def read_all_text(
        self,
        screenshot_path: str | Path,
        confidence: float = 0.3,
    ) -> List[Tuple[str, float, Point]]:
        """Return all OCR hits as ``(text, score, center)``."""
        reader = self._get_reader(self.languages, self.gpu)
        if reader is None:
            return []

        try:
            results = reader.readtext(str(screenshot_path))
        except Exception as exc:
            logger.warning("OCR read_all failed: %s", exc)
            return []

        out: List[Tuple[str, float, Point]] = []
        for bbox, text, score in results:
            if score < confidence:
                continue
            xs = [int(p[0]) for p in bbox]
            ys = [int(p[1]) for p in bbox]
            center = ((min(xs) + max(xs)) // 2, (min(ys) + max(ys)) // 2)
            out.append((text, float(score), center))
        return out
