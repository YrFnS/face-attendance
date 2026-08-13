from pathlib import Path

from model_manifest import runtime_model_binding


class ModelRuntimeError(RuntimeError):
    """Raised when InsightFace is not bound to the verified model directory."""


def create_face_analysis(
    factory,
    cfg,
    app_root,
    *,
    det_size,
    verified_model_directory=None,
):
    binding = runtime_model_binding(cfg, app_root)
    expected_directory = Path(binding["model_directory"]).resolve()

    if verified_model_directory:
        verified_directory = Path(verified_model_directory).expanduser().resolve()
        if verified_directory != expected_directory:
            raise ModelRuntimeError(
                "verified model directory does not match configured runtime directory: "
                f"{verified_directory} != {expected_directory}"
            )

    if not expected_directory.is_dir():
        raise ModelRuntimeError(
            f"configured model directory is unavailable: {expected_directory}"
        )

    app = factory(
        name=binding["model"],
        root=binding["insightface_root"],
        allowed_modules=cfg.get(
            "allowed_modules", ["detection", "recognition"]
        ),
        providers=["CPUExecutionProvider"],
    )
    actual_value = getattr(app, "model_dir", "")
    if not actual_value:
        raise ModelRuntimeError(
            "InsightFace did not expose the model directory it loaded"
        )
    actual_directory = Path(actual_value).expanduser().resolve()
    if actual_directory != expected_directory:
        raise ModelRuntimeError(
            "InsightFace loaded an unexpected model directory: "
            f"{actual_directory} != {expected_directory}"
        )

    size = int(det_size)
    app.prepare(ctx_id=-1, det_size=(size, size))
    return app
