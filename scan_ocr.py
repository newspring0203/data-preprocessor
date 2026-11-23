"""Run pretrained PP-OCRv5 text detection and Korean recognition offline."""
from pathlib import Path

DETECTION = "ch_PP-OCRv5_det_mobile.onnx"
RECOGNITION = "korean_PP-OCRv5_rec_mobile.onnx"


class ScanOCR:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.reader = None

    def words(self, image, width, height):
        if self.reader is None:
            try:
                import onnxruntime
                from rapidocr import LangRec, ModelType, OCRVersion, RapidOCR
            except ImportError as error:
                raise RuntimeError("한국어 OCR 환경이 없습니다. README의 설치 명령을 실행하세요.") from error
            onnxruntime.disable_telemetry_events()
            det = self.directory / DETECTION
            rec = self.directory / RECOGNITION
            if not det.is_file() or not rec.is_file():
                raise RuntimeError(f"PP-OCRv5 모델 파일이 없습니다: {self.directory}")
            self.reader = RapidOCR(params={
                "Global.use_cls": False,
                "Global.log_level": "error",
                "Global.text_score": 0.55,
                "Det.ocr_version": OCRVersion.PPOCRV5,
                "Det.model_type": ModelType.MOBILE,
                "Det.model_path": str(det),
                "Rec.ocr_version": OCRVersion.PPOCRV5,
                "Rec.model_type": ModelType.MOBILE,
                "Rec.lang_type": LangRec.KOREAN,
                "Rec.model_path": str(rec),
                "EngineConfig.onnxruntime.intra_op_num_threads": 4,
                "EngineConfig.onnxruntime.inter_op_num_threads": 1,
            })
        page = self.reader(image)
        if page.boxes is None:
            return []
        sx, sy = width / image.width, height / image.height
        result = []
        for box, text in zip(page.boxes, page.txts, strict=True):
            xs, ys = zip(*box)
            result.append(dict(text=text, x0=min(xs)*sx, x1=max(xs)*sx,
                               top=min(ys)*sy, bottom=max(ys)*sy))
        return result
