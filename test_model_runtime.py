import tempfile
import unittest
from pathlib import Path

from model_runtime import ModelRuntimeError, create_face_analysis


class FakeApp:
    def __init__(self, model_dir):
        self.model_dir = str(model_dir)
        self.prepared = None

    def prepare(self, *, ctx_id, det_size):
        self.prepared = (ctx_id, det_size)


class FakeFactory:
    def __init__(self, actual_model_dir=None):
        self.actual_model_dir = actual_model_dir
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        actual = (
            self.actual_model_dir
            or Path(kwargs["root"]) / "models" / kwargs["name"]
        )
        return FakeApp(actual)


class ModelRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.insightface_root = self.root / "runtime"
        self.model_dir = (
            self.insightface_root / "models" / "buffalo_l"
        )
        self.model_dir.mkdir(parents=True)
        (self.model_dir / "recognition.onnx").write_bytes(b"model")
        self.cfg = {
            "model": "buffalo_l",
            "model_version": "v1",
            "model_directory": str(self.model_dir),
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_verified_directory_is_passed_as_insightface_root(self):
        factory = FakeFactory()
        app = create_face_analysis(
            factory,
            self.cfg,
            self.root,
            det_size=640,
            verified_model_directory=self.model_dir,
        )
        self.assertEqual(
            Path(factory.calls[0]["root"]), self.insightface_root
        )
        self.assertEqual(factory.calls[0]["name"], "buffalo_l")
        self.assertEqual(app.prepared, (-1, (640, 640)))

    def test_mismatched_verified_directory_is_rejected(self):
        other = self.root / "other" / "models" / "buffalo_l"
        other.mkdir(parents=True)
        with self.assertRaisesRegex(
            ModelRuntimeError, "verified model directory"
        ):
            create_face_analysis(
                FakeFactory(),
                self.cfg,
                self.root,
                det_size=640,
                verified_model_directory=other,
            )

    def test_factory_loading_different_directory_is_rejected(self):
        other = self.root / "other" / "models" / "buffalo_l"
        other.mkdir(parents=True)
        with self.assertRaisesRegex(
            ModelRuntimeError, "unexpected model directory"
        ):
            create_face_analysis(
                FakeFactory(actual_model_dir=other),
                self.cfg,
                self.root,
                det_size=640,
                verified_model_directory=self.model_dir,
            )

    def test_non_native_directory_layout_is_rejected(self):
        cfg = dict(
            self.cfg,
            model_directory=str(self.root / "models-elsewhere"),
        )
        with self.assertRaisesRegex(ValueError, "root/models/<model>"):
            create_face_analysis(
                FakeFactory(), cfg, self.root, det_size=640
            )


if __name__ == "__main__":
    unittest.main()
