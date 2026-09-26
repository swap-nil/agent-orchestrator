import time
import unittest

import helpers  # noqa: F401  (sets sys.path)
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from orchestrator.api.security import (
    AuthError, Authenticator, JwtValidator, end_user_from_claims, parse_xfcc, resolve_acr, spiffe_from_xfcc,
)
from orchestrator.config import AuthConfig, JwtConfig

LEVELS = ["low", "standard", "stepup"]


def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key, key.public_key()


class XfccTests(unittest.TestCase):
    def test_parse_multiple_elements_uses_last(self):
        header = ('By=spiffe://bank/ns/a/sa/gw;Hash=abc;URI=spiffe://bank/ns/x/sa/outer,'
                  'By=spiffe://bank/ns/o/sa/orch;Hash=def;Subject="CN=a,O=b";URI=spiffe://bank/ns/voice/sa/master-agent')
        elements = parse_xfcc(header)
        self.assertEqual(len(elements), 2)
        self.assertEqual(elements[1]["Subject"], "CN=a,O=b")
        self.assertEqual(spiffe_from_xfcc(header), "spiffe://bank/ns/voice/sa/master-agent")

    def test_non_spiffe_rejected(self):
        self.assertIsNone(spiffe_from_xfcc("URI=https://evil"))
        self.assertIsNone(spiffe_from_xfcc(None))


class JwtTests(unittest.TestCase):
    def setUp(self):
        self.priv, self.pub = keypair()
        self.cfg = JwtConfig(issuer="https://idp", audience="api://orch", jwks_url="unused", algorithms=["RS256"])
        self.validator = JwtValidator(self.cfg, key_resolver=lambda token: self.pub)

    def token(self, **overrides):
        now = int(time.time())
        claims = {"iss": "https://idp", "aud": "api://orch", "sub": "u1", "iat": now, "exp": now + 300,
                  "tid": "t1", "acr": "stepup", **overrides}
        return jwt.encode(claims, self.priv, algorithm="RS256")

    def test_valid(self):
        self.assertEqual(self.validator.validate(self.token())["sub"], "u1")

    def test_rejections(self):
        for bad in (self.token(aud="other"), self.token(iss="https://evil"), self.token(exp=int(time.time()) - 600)):
            with self.assertRaises(AuthError):
                self.validator.validate(bad)
        other_priv, _ = keypair()
        forged = jwt.encode({"iss": "https://idp", "aud": "api://orch", "sub": "u", "iat": 1, "exp": int(time.time()) + 60}, other_priv, algorithm="RS256")
        with self.assertRaises(AuthError):
            self.validator.validate(forged)

    def test_hs256_confusion_rejected(self):
        # Attacker signs with HS256 using the public key bytes as the secret.
        with self.assertRaises(AuthError):
            self.validator.validate(jwt.encode({"sub": "x"}, "secret", algorithm="HS256"))

    def test_end_user_mapping(self):
        cfg = AuthConfig(user_acr_claim="acrs", user_subject_claim="oid")
        user = end_user_from_claims({"oid": "o1", "tid": "t", "acrs": ["low", "stepup"], "exp": 5}, cfg)
        self.assertEqual((user.subject, user.acr), ("o1", "stepup"))
        with self.assertRaises(AuthError):
            end_user_from_claims({"oid": "o1", "tid": "t", "acrs": ["c9"]}, cfg)
        self.assertEqual(resolve_acr("standard", LEVELS), "standard")
        self.assertEqual(resolve_acr(None, LEVELS), "")


class AuthenticatorTests(unittest.TestCase):
    def test_route_allowlist(self):
        cfg = AuthConfig(mode="mesh_xfcc", route_callers={"turns": ["spiffe://b/ns/voice/sa/master-agent"]})
        auth = Authenticator(cfg)
        ok = auth.caller({"x-forwarded-client-cert": "URI=spiffe://b/ns/voice/sa/master-agent"}, "turns")
        self.assertEqual(ok.kind, "spiffe")
        with self.assertRaises(AuthError) as ctx:
            auth.caller({"x-forwarded-client-cert": "URI=spiffe://b/ns/voice/sa/master-agent"}, "admin")
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(AuthError):
            auth.caller({}, "turns")

    def test_dev_fallback_only_in_none_mode(self):
        dev = Authenticator(AuthConfig(mode="none"))
        self.assertEqual(dev.end_user(None, fallback={"subject": "d", "acr": "stepup"}).acr, "stepup")
        prod_like = Authenticator(AuthConfig(mode="mesh_xfcc"))
        with self.assertRaises(AuthError):
            prod_like.end_user(None, fallback={"subject": "d", "acr": "stepup"})


if __name__ == "__main__":
    unittest.main()


class TlsTests(unittest.TestCase):
    def test_contexts(self):
        import ssl
        from orchestrator.tls import client_ssl_context
        self.assertIs(client_ssl_context(verify=False), False)
        ctx = client_ssl_context()
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)
        self.assertGreaterEqual(ctx.minimum_version, ssl.TLSVersion.TLSv1_2)
        with self.assertRaises(OSError):
            client_ssl_context(ca_bundle="/nonexistent/ca.pem")
