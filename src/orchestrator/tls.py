"""TLS context construction (standard library only, unit-tested).

Two trust domains are kept apart on purpose:

* mesh: the internal A2A gateway, verified against the mesh CA, with the
  workload's SPIFFE certificate presented for mTLS;
* public: the identity provider and other public endpoints, verified against
  the system trust store.
"""

from __future__ import annotations

import ssl


def client_ssl_context(
    *, verify: bool = True, ca_bundle: str = "", cert_file: str = "", key_file: str = "", min_tls13: bool = False
) -> ssl.SSLContext | bool:
    """Return an SSLContext for httpx, or False when verification is disabled (dev only)."""
    if not verify:
        return False
    context = ssl.create_default_context(cafile=ca_bundle or None)
    context.minimum_version = ssl.TLSVersion.TLSv1_3 if min_tls13 else ssl.TLSVersion.TLSv1_2
    if cert_file:
        context.load_cert_chain(cert_file, key_file or None)
    return context
