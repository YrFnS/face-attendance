# PAD face binding and provider pinning

Production PAD is accepted only when evidence is tied to the exact detected face that recognition consumes.

## Face binding

The canonical watcher performs one InsightFace detection pass. Faces are ordered deterministically and each PAD request receives:

- the immutable camera-event ID and source SHA-256;
- the camera ID and candidate log type;
- the one-based face index and total face count;
- the exact detected bounding box;
- the SHA-256 of the JPEG crop sent to the PAD provider;
- a derived `face_binding_id` covering all of those values.

The provider must echo `face_binding_id`. A missing or different value fails closed. Recognition then receives the already detected face objects through a one-use bound adapter, so it cannot silently run a second detection pass and associate the PAD evidence with another face.

## Face-count modes

`pad_require_single_face: true` rejects any image that does not contain exactly one detected face before a PAD request is sent. This remains the required strict-production profile.

When the setting is false outside that strict profile, every detected face is evaluated separately. The event proceeds to recognition only when every face receives a passing, non-skipped PAD result. A provider error or failed face rejects the entire capture.

`pad_max_faces_per_event` bounds provider work and defaults to 8, with a hard maximum of 32.

## Provider response contract

A production response must include:

```json
{
  "live": true,
  "score": 0.94,
  "provider": "approved-provider",
  "model": "liveness-v3",
  "evidence_id": "provider-evidence-id",
  "face_binding_id": "64-lowercase-hex-characters"
}
```

`provider` must exactly match `pad_expected_provider`. `model` must appear in `pad_allowed_models`. `evidence_id` is required, and the binding echo must match the request.

Before enabling production, configure and verify:

```json
{
  "pad_expected_provider": "approved-provider",
  "pad_allowed_models": ["liveness-v3"],
  "pad_require_binding_echo": true,
  "pad_require_evidence_id": true,
  "pad_require_single_face": true,
  "pad_max_faces_per_event": 8
}
```

Changing the provider or model requires an explicit configuration change, readiness validation, and controlled acceptance testing.
