# Gallery and employee data contract

H0-07 defines one validation boundary before gallery data or employee identifiers reach filesystem paths, HTTP requests, logs, or ERPNext.

## Employee IDs

Employee IDs are normalized to Unicode NFC. They must be 1–128 characters, no more than 180 UTF-8 bytes, start with a letter or digit, and contain only letters, digits, combining marks, `.`, `_`, `@`, or `-`. Whitespace, slashes, traversal segments, control characters, and directional formatting controls are rejected.

Safe ASCII IDs such as `HR-EMP-0001` retain their current directory name. Other valid IDs use a reversible unpadded Base64URL directory component prefixed with `e~`. The original canonical ID remains the value stored in the gallery and sent to ERPNext. Employee directories and enrollment images must not be symbolic links.

## Gallery limits

Schema version 1 rejects unknown fields, duplicate JSON keys, non-finite constants, and silent type coercion. The default and hard limits are:

| Field | Default | Hard maximum |
| --- | ---: | ---: |
| Employees | 10,000 | 100,000 |
| Templates per employee | 50 | 1,000 |
| Total templates | 500,000 | 500,000 |
| Embedding dimension | 4,096 | 4,096 |

Integer fields must be JSON integers rather than strings, floats, or booleans. Vector entries must be finite JSON numbers with the exact declared dimension. Strings, timestamps, signatures, and checksums have explicit canonical formats and length limits. A supplied checksum must equal the canonical sanitized payload checksum.

## Boundary rules

Synchronization validates gallery identity, limits, and endpoint path before network access. Runtime logs escape line breaks and formatting controls. Local enrollment and migration use the same employee-directory encoding. ERPNext transports revalidate the employee ID and IN/OUT value before creating a check-in.

Existing safe ASCII enrollment folders remain compatible. Invalid legacy folder names require an audited rename before gallery rebuild.
