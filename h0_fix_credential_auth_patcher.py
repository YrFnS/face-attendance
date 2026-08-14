from pathlib import Path


def replace_exact(path, old, new, *, count=1):
    target = Path(path)
    source = target.read_text(encoding="utf-8")
    actual = source.count(old)
    if actual != count:
        raise SystemExit(
            f"expected {count} match(es) in {path}, found {actual}: {old[:120]!r}"
        )
    target.write_text(source.replace(old, new), encoding="utf-8")


patcher = Path("h0_apply_credential_auth.py")
source = patcher.read_text(encoding="utf-8")
old = """    replace_once(
        \"secure_sync.py\",
        '''                    result[\"gallery_age_seconds\"] = freshness[\"age_seconds\"]
''',
        '''                    result[\"credential_id\"] = credential.credential_id
                    result[\"credential_fingerprint\"] = credential.fingerprint
                    result[\"gallery_age_seconds\"] = freshness[\"age_seconds\"]
''',
    )
    replace_once(
        \"secure_sync.py\",
        '''                result[\"gallery_age_seconds\"] = freshness[\"age_seconds\"]
''',
        '''                result[\"credential_id\"] = credential.credential_id
                result[\"credential_fingerprint\"] = credential.fingerprint
                result[\"gallery_age_seconds\"] = freshness[\"age_seconds\"]
''',
    )
"""
new = """    sync_source = read(\"secure_sync.py\")
    first_old = '\\n                    result[\"gallery_age_seconds\"] = freshness[\"age_seconds\"]\\n'
    first_new = (
        '\\n                    result[\"credential_id\"] = credential.credential_id\\n'
        '                    result[\"credential_fingerprint\"] = credential.fingerprint\\n'
        '                    result[\"gallery_age_seconds\"] = freshness[\"age_seconds\"]\\n'
    )
    if sync_source.count(first_old) != 1:
        raise SystemExit(\"expected one 304 result marker in secure_sync.py\")
    sync_source = sync_source.replace(first_old, first_new, 1)
    second_old = '\\n                result[\"gallery_age_seconds\"] = freshness[\"age_seconds\"]\\n'
    second_new = (
        '\\n                result[\"credential_id\"] = credential.credential_id\\n'
        '                result[\"credential_fingerprint\"] = credential.fingerprint\\n'
        '                result[\"gallery_age_seconds\"] = freshness[\"age_seconds\"]\\n'
    )
    if sync_source.count(second_old) != 1:
        raise SystemExit(\"expected one download result marker in secure_sync.py\")
    sync_source = sync_source.replace(second_old, second_new, 1)
    write(\"secure_sync.py\", sync_source)
"""
if source.count(old) != 1:
    raise SystemExit("secure sync patch block was not found exactly once")
patcher.write_text(source.replace(old, new, 1), encoding="utf-8")

replace_exact(
    "gallery_credentials.py",
    '        raise GalleryCredentialError(f"{field} must be configured")\n',
    '        raise GalleryCredentialError(\n'
    '            f"{field} must be a non-placeholder value"\n'
    '        )\n',
)

replace_exact(
    "runtime_policy.py",
    '''        if is_placeholder(cfg.get("central_api_token")):
            issues.append(
                (
                    "central_api_token_missing",
                    "central_api_token must be a non-placeholder value in production",
                )
            )
''',
    '''        structured_credentials = cfg.get("central_api_credentials")
        selected_credential = _text(cfg.get("central_api_credential_id"))
        has_structured_credential = (
            isinstance(structured_credentials, dict)
            and bool(selected_credential)
            and selected_credential in structured_credentials
        )
        if not has_structured_credential and is_placeholder(
            cfg.get("central_api_token")
        ):
            issues.append(
                (
                    "central_api_token_missing",
                    "a scoped central gallery credential is required in production",
                )
            )
''',
)

replace_exact(
    "test_web_admin.py",
    '            "embedding_export_token": "secret",\n',
    '            "embedding_export_token": "secret-token-value",\n',
)
replace_exact(
    "test_web_admin.py",
    '"Authorization": "Bearer secret"',
    '"Authorization": "Bearer secret-token-value"',
    count=2,
)
