import unittest

from test_h0_credential_auth import (
    ExternalAuthAdapterH11Tests,
    GalleryCredentialH11Tests,
    RuntimeRateLimitH11Tests,
    SecretStoreH11Tests,
    SystemdCredentialExampleH11Tests,
    TrustedProxyH11Tests,
    WebAdminH11Tests,
)

try:
    import flask  # noqa: F401
except ModuleNotFoundError:
    flask = None

if flask is not None:
    from flask import Flask

    from web_security import (
        auth_configured,
        configure_app,
        hash_password,
        password_hash_issues,
        verify_password,
    )


@unittest.skipIf(flask is None, "Flask dependency is not installed")
class WebSecurityTests(unittest.TestCase):
    def test_password_hash_round_trip(self):
        encoded = hash_password(
            "correct horse battery staple"
        )
        self.assertFalse(password_hash_issues(encoded))
        self.assertTrue(
            verify_password(
                "correct horse battery staple", encoded
            )
        )
        self.assertFalse(
            verify_password("wrong password", encoded)
        )

    def test_short_password_is_rejected(self):
        with self.assertRaises(ValueError):
            hash_password("too-short")

    def test_prefix_only_hash_is_rejected(self):
        malformed = "scrypt$16384$8$1$salt$hash"
        self.assertTrue(password_hash_issues(malformed))
        self.assertFalse(
            auth_configured(
                {
                    "web_admin_username": "admin",
                    "web_admin_password_hash": malformed,
                    "web_session_secret": "x" * 48,
                }
            )
        )

    def test_unsafe_scrypt_cost_is_rejected_before_derivation(self):
        encoded = "scrypt$1073741824$8$1$AAAAAAAAAAAAAAAAAAAAAA$" + (
            "A" * 43
        )
        self.assertTrue(password_hash_issues(encoded))
        self.assertFalse(verify_password("anything", encoded))

    def test_auth_requires_persistent_secret_and_hash(self):
        cfg = {
            "web_admin_username": "admin",
            "web_admin_password_hash": hash_password(
                "correct horse battery staple"
            ),
            "web_session_secret": "x" * 48,
            "web_cookie_secure": False,
        }
        self.assertTrue(auth_configured(cfg))
        app = Flask(__name__)
        self.assertTrue(configure_app(app, cfg))
        self.assertEqual(
            app.config["SESSION_COOKIE_SAMESITE"], "Lax"
        )
        self.assertTrue(
            app.config["SESSION_COOKIE_HTTPONLY"]
        )


if __name__ == "__main__":
    unittest.main()
