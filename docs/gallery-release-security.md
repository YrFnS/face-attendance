# Authenticated Gallery Releases and Anti-Rollback

This control prevents an old but structurally valid embedding gallery from being silently downloaded or reactivated.

## Release envelope

A published `embedding_gallery.json` may contain a signed release object:

```json
{
  "schema_version": 1,
  "gallery_version": "baghdad-2026-08-13.42",
  "generated_at": "2026-08-13T12:00:00Z",
  "model": "buffalo_l",
  "model_version": "approved-model-v1",
  "dimension": 512,
  "normalized": true,
  "branch": "Baghdad",
  "employees": [],
  "release": {
    "sequence": 42,
    "publisher": "central-enrollment",
    "key_id": "release-key-2026-q3",
    "algorithm": "ed25519",
    "signature": "BASE64URL_SIGNATURE"
  },
  "checksum": "CALCULATED_BY_THE_APPLICATION"
}
```

The Ed25519 signature covers the canonical JSON representation of every gallery field except `checksum` and `release.signature`. It therefore authenticates the employee embeddings, branch, model, model version, generated time, release sequence, publisher, and key identity.

## Attendance-node configuration

Production mode always requires an authenticated release. Configure the expected publisher and trusted public keys:

```json
{
  "production_mode": true,
  "embedding_release_required": true,
  "embedding_release_publisher": "central-enrollment",
  "embedding_release_trusted_keys": {
    "release-key-2026-q3": {
      "publisher": "central-enrollment",
      "public_key": "UNPADDED_BASE64URL_32_BYTE_ED25519_PUBLIC_KEY"
    }
  },
  "embedding_release_future_tolerance_seconds": 300,
  "embedding_release_history_limit": 32
}
```

A release is rejected when:

- its signature does not verify under the configured key;
- its publisher or key assignment is unexpected;
- `generated_at` is invalid, lacks a timezone, or is too far in the future;
- its sequence is lower than the last accepted sequence for the same source scope;
- the same sequence is reused for different content or a different generation time;
- a higher sequence claims a generation time older than the accepted release;
- the installed gallery does not match the accepted release state.

## Scoped synchronization state

Conditional request state is bound to:

```text
central source URL + branch + model + model version
```

Each scope has an independent ETag, checksum, release sequence, publisher, key ID, and bounded acceptance history under `release_scopes` in `embedding_sync_status.json`.

An ETag learned for one branch or model is never sent for another. A `304 Not Modified` response is accepted only when matching scoped state and a matching installed gallery already exist.

## Publisher key setup

Generate a dedicated offline Ed25519 signing key:

```bash
python gallery_release.py generate-key \
  --private-key release-private.pem \
  --public-key release-public.txt
```

Keep `release-private.pem` off attendance nodes and restrict it to the trusted publisher. Add the one-line value from `release-public.txt` to `embedding_release_trusted_keys` on attendance nodes.

After rebuilding the central gallery, sign it with the next monotonic sequence:

```bash
python gallery_release.py sign \
  --gallery embedding_gallery.json \
  --private-key release-private.pem \
  --publisher central-enrollment \
  --key-id release-key-2026-q3 \
  --sequence 42
```

Never reuse a sequence for changed content. Record sequence allocation in the publisher's release process rather than deriving it from a local clock.

## Key rotation

Add the new public key to attendance nodes before publishing with it. Use a new `key_id`, keep the same publisher identity, and advance the release sequence. After every node has accepted releases under the new key, remove the retired key according to the approved rollback window.

Changing the publisher identity intentionally creates a different trust relationship and should be handled as a controlled migration, not an ordinary key rotation.

## Recovery

The last valid local gallery remains active when a new release fails validation. Investigate the central publisher, key inventory, release sequence record, and `embedding_sync_status.json` before any manual state change.

Do not delete or lower the accepted sequence merely to make an older gallery load. A rollback requires a newly signed release with a higher sequence and an explicit operational record explaining why older embedding content was republished.
