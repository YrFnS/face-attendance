from pathlib import Path


path = Path("h0_apply_credential_auth.py")
source = path.read_text(encoding="utf-8")
first = '''    replace_once(
        "secure_sync.py",
        ''' + "'''" + '''                    result[\"gallery_age_seconds\"] = freshness[\"age_seconds\"]
''' + "'''" + ''',
        ''' + "'''" + '''                    result[\"credential_id\"] = credential.credential_id
                    result[\"credential_fingerprint\"] = credential.fingerprint
                    result[\"gallery_age_seconds\"] = freshness[\"age_seconds\"]
''' + "'''" + ''',
    )
    replace_once(
        "secure_sync.py",
        ''' + "'''" + '''                result[\"gallery_age_seconds\"] = freshness[\"age_seconds\"]
''' + "'''" + ''',
        ''' + "'''" + '''                result[\"credential_id\"] = credential.credential_id
                result[\"credential_fingerprint\"] = credential.fingerprint
                result[\"gallery_age_seconds\"] = freshness[\"age_seconds\"]
''' + "'''" + ''',
    )
'''
second = '''    replace_once(
        "secure_sync.py",
        ''' + "'''" + '''                result[\"gallery_age_seconds\"] = freshness[\"age_seconds\"]
''' + "'''" + ''',
        ''' + "'''" + '''                result[\"credential_id\"] = credential.credential_id
                result[\"credential_fingerprint\"] = credential.fingerprint
                result[\"gallery_age_seconds\"] = freshness[\"age_seconds\"]
''' + "'''" + ''',
    )
    replace_once(
        "secure_sync.py",
        ''' + "'''" + '''                    result[\"gallery_age_seconds\"] = freshness[\"age_seconds\"]
''' + "'''" + ''',
        ''' + "'''" + '''                    result[\"credential_id\"] = credential.credential_id
                    result[\"credential_fingerprint\"] = credential.fingerprint
                    result[\"gallery_age_seconds\"] = freshness[\"age_seconds\"]
''' + "'''" + ''',
    )
'''
if source.count(first) != 1:
    raise SystemExit("secure sync patch block was not found exactly once")
path.write_text(source.replace(first, second, 1), encoding="utf-8")
