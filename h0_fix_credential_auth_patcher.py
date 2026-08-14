from pathlib import Path


path = Path("h0_apply_credential_auth.py")
source = path.read_text(encoding="utf-8")
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
path.write_text(source.replace(old, new, 1), encoding="utf-8")
