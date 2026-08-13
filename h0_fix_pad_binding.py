from pathlib import Path


def replace_once(path, old, new):
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    if text.count(old) != 1:
        raise SystemExit(f"expected exactly one match in {path}: {old!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


def insert_before_once(path, marker, addition):
    replace_once(path, marker, addition + marker)


def main():
    replace_once(
        "watch_service.py",
        '''        if pad_results and not all(result.passed and not result.skipped for result in pad_results):\n            reasons = ",".join(\n                f"{result.face_index}:{result.reason or 'rejected'}"\n                for result in pad_results\n                if not result.passed or result.skipped\n            )\n''',
        '''        strict_pad_evidence = bool(cfg.get("production_mode", False)) or pad_gate.required\n        rejected_pad_results = [\n            result\n            for result in pad_results\n            if not result.passed or (strict_pad_evidence and result.skipped)\n        ]\n        if rejected_pad_results:\n            reasons = ",".join(\n                f"{result.face_index}:{result.reason or 'rejected'}"\n                for result in rejected_pad_results\n            )\n''',
    )

    replace_once(
        "runtime_policy.py",
        '''        (\n            "pad_face_binding_unsafe",\n            "pad_require_single_face",\n            True,\n            "pad_require_single_face must be true until PAD is bound independently to every accepted face",\n        ),\n''',
        "",
    )

    insert_before_once(
        "test_runtime_policy.py",
        '''    def test_production_cannot_weaken_gallery_controls(self):\n''',
        '''    def test_production_allows_multi_face_only_through_bound_pad_runtime(self):\n        cfg = dict(self.cfg, pad_require_single_face=False)\n        codes = {code for code, _ in strict_profile_issues(cfg)}\n        self.assertNotIn("pad_face_binding_unsafe", codes)\n\n''',
    )

    insert_before_once(
        "test_watch_service.py",
        '''    def test_per_face_mode_requires_every_face_to_pass(self):\n''',
        '''    def test_production_per_face_mode_binds_every_face_before_recognition(self):\n        self.cfg.update(production_mode=True, pad_require_single_face=False)\n        faces = [FakeFace(1), FakeFace(40)]\n        pad = PassingPAD()\n        captured = {}\n\n        def process_image(_image, _source, bound_app, _known, _cfg, _dry_run, **kwargs):\n            captured["faces"] = bound_app.get(np.zeros((1, 1, 3), dtype=np.uint8))\n            return True\n\n        attendance.process_image = process_image\n        result = watch_service.process_path(\n            self.image_path,\n            FakeApp(faces),\n            [],\n            StaticGallery([]),\n            self.cfg,\n            self.state,\n            pad,\n        )\n        self.assertTrue(result)\n        self.assertEqual(len(pad.calls), 2)\n        self.assertEqual(captured["faces"], faces)\n        event = self.state.get_event(self.event_id())\n        self.assertEqual(event["status"], "checkin_created")\n\n    def test_nonproduction_optional_fail_open_remains_diagnostic_only(self):\n        class SkippedPAD(PassingPAD):\n            required = False\n\n            def evaluate(self, crop, context):\n                result = super().evaluate(crop, context)\n                return PADResult(\n                    True,\n                    None,\n                    result.provider,\n                    reason="fail_open:provider_offline",\n                    binding_id=result.binding_id,\n                    crop_sha256=result.crop_sha256,\n                    face_index=result.face_index,\n                    face_count=result.face_count,\n                    skipped=True,\n                )\n\n        called = []\n\n        def process_image(_image, _source, bound_app, _known, _cfg, _dry_run, **kwargs):\n            bound_app.get(np.zeros((1, 1, 3), dtype=np.uint8))\n            called.append(True)\n            return True\n\n        attendance.process_image = process_image\n        result = watch_service.process_path(\n            self.image_path,\n            FakeApp([FakeFace()]),\n            [],\n            StaticGallery([]),\n            self.cfg,\n            self.state,\n            SkippedPAD(),\n        )\n        self.assertTrue(result)\n        self.assertEqual(called, [True])\n\n''',
    )

    replace_once(
        "docs/pad-face-binding.md",
        '''`pad_require_single_face: true` rejects any image that does not contain exactly one detected face before a PAD request is sent. This remains the required strict-production profile.\n\nWhen the setting is false outside that strict profile, every detected face is evaluated separately. The event proceeds to recognition only when every face receives a passing, non-skipped PAD result. A provider error or failed face rejects the entire capture.\n''',
        '''`pad_require_single_face: true` rejects any image that does not contain exactly one detected face before a PAD request is sent. It remains the safer default.\n\nWhen the setting is false, every detected face is evaluated separately. Production permits this mode only because provider identity, model identity, evidence IDs, and face-binding echoes are mandatory. The event proceeds to recognition only when every face receives a passing, non-skipped PAD result. A provider error or failed face rejects the entire capture.\n''',
    )


if __name__ == "__main__":
    main()
