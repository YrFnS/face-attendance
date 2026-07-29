# Embedding Gallery API Contract

This contract allows attendance servers to receive employee identity vectors without receiving employee enrollment images.

## Request

```http
GET /api/faces/embeddings?branch=<branch-name>
Accept: application/json
Authorization: Bearer <token>
```

Requirements:

- Serve the endpoint over HTTPS, a trusted VPN, or an isolated LAN.
- Authenticate every request with a long random token or an equivalent stronger mechanism.
- Return only the requested branch.
- Do not log the authorization token or complete embedding vectors.
- Do not return Python pickle data.

## Response

```json
{
  "schema_version": 1,
  "gallery_version": "2026-07-29T10:30:00Z",
  "generated_at": "2026-07-29T10:30:00Z",
  "model": "buffalo_l",
  "model_version": "optional-model-build-id",
  "dimension": 512,
  "normalized": true,
  "branch": "بغداد - الحارثية",
  "employees": [
    {
      "employee": "HR-EMP-00001",
      "employee_name": "Optional display name",
      "embeddings": [
        [0.0123, -0.0412, 0.0841]
      ]
    }
  ]
}
```

The vector above is shortened only for documentation. Each vector must contain exactly `dimension` finite numeric values.

## Validation performed by the client

The attendance client rejects the complete update when any of these checks fail:

- unsupported `schema_version`;
- empty gallery unless explicitly allowed;
- branch mismatch;
- model mismatch when `require_model_match` is enabled;
- duplicate or missing employee IDs;
- missing embeddings;
- zero-length, non-finite, or incorrectly sized vectors;
- configured employee or per-employee embedding limits.

Every vector is normalized again locally. The gallery is written to a temporary file, flushed to disk, and atomically moved into place. Invalid downloads never overwrite the previous working gallery.

## Multiple embeddings per employee

Send multiple high-quality embeddings for each employee instead of only one average vector. The matcher uses the strongest similarity for each employee and still applies the global threshold and second-best employee margin. Three to ten diverse, clean embeddings per employee is a practical target.

## Model compatibility

The central server and attendance servers must use the same recognition model and preprocessing behavior. The default repository configuration uses InsightFace `buffalo_l`. Do not mix vectors produced by unrelated recognition models in one gallery.

## Built-in exporter

When `web_admin.py` is configured with:

```json
{
  "embedding_export_enabled": true,
  "embedding_export_token": "LONG_RANDOM_TOKEN"
}
```

it serves this contract at `/api/faces/embeddings`. It exposes only the validated local gallery and requires the exact Bearer token. The endpoint is intended for a trusted single-branch enrollment server.
