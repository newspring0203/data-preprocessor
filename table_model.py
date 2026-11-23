"""Offline Microsoft Table Transformer inference for PDF table layout."""
import os
from pathlib import Path


class TableModel:
    def __init__(self, directory):
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        try:
            import torch
            from transformers import DetrImageProcessor, TableTransformerForObjectDetection
        except ImportError as error:
            raise RuntimeError("딥러닝 환경이 없습니다. README의 설치 명령을 실행하세요.") from error
        self.torch = torch
        torch.set_num_threads(min(4, torch.get_num_threads()))
        self.stages = {}
        for stage in ("detection", "structure-recognition-v1.1-all"):
            path = Path(directory).resolve() / stage
            if not (path / "config.json").is_file():
                raise RuntimeError(f"Table Transformer 모델 폴더가 없습니다: {path}")
            processor = DetrImageProcessor(do_resize=False)
            model = TableTransformerForObjectDetection.from_pretrained(
                path, local_files_only=True, use_pretrained_backbone=False, use_safetensors=True
            )
            self.stages[stage] = processor, model.eval()

    def predict(self, stage, image, threshold):
        processor, model = self.stages[stage]
        from PIL import Image

        prepared = image.copy()
        prepared.thumbnail((800, 800), Image.Resampling.BILINEAR)
        inputs = processor(images=prepared, return_tensors="pt")
        with self.torch.inference_mode():
            output = model(**inputs)
        result = processor.post_process_object_detection(
            output, target_sizes=self.torch.tensor([[image.height, image.width]]),
            threshold=threshold,
        )[0]
        return [(model.config.id2label[label], box) for label, box in
                zip(result["labels"].tolist(), result["boxes"].tolist())]

    def layouts(self, image, width, height):
        scale_x, scale_y = width / image.width, height / image.height
        for label, box in self.predict("detection", image, 0.65):
            if label != "table":
                continue
            x0, y0 = max(0, int(box[0])-12), max(0, int(box[1])-12)
            x1, y1 = min(image.width, int(box[2])+12), min(image.height, int(box[3])+12)
            if x1 <= x0 or y1 <= y0:
                continue
            crop = image.crop((x0, y0, x1, y1))
            structure = self.predict("structure-recognition-v1.1-all", crop, 0.65)
            rows = sorted((box for label, box in structure if label == "table row"),
                          key=lambda box: box[1])
            cols = sorted((box for label, box in structure if label == "table column"),
                          key=lambda box: box[0])
            if len(rows) < 2 or len(cols) < 2:
                continue
            xs = [(x + x0) * scale_x for x in self.boundaries(cols, 0)]
            ys = [(y + y0) * scale_y for y in self.boundaries(rows, 1)]
            if min(b-a for a, b in zip(xs, xs[1:])) < 4 or min(
                b-a for a, b in zip(ys, ys[1:])
            ) < 4:
                continue
            yield xs, ys

    @staticmethod
    def boundaries(boxes, axis):
        return [boxes[0][axis], *[(a[axis+2]+b[axis])/2 for a, b in
                                  zip(boxes, boxes[1:])], boxes[-1][axis+2]]
