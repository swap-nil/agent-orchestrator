"""Encrypts Temporal payloads so workflow inputs, signals and results are
encrypted at rest in Temporal's database and in its UI/CLI output.

Uses Fernet (AES-128-CBC + HMAC-SHA256) with a key from Key Vault
(``workflows.payload_key_env``). Rotate by deploying a MultiFernet with the new
key first. The Temporal UI shows ciphertext unless a codec server is deployed.
"""

from __future__ import annotations

import dataclasses
from typing import Sequence

import temporalio.converter
from cryptography.fernet import Fernet
from temporalio.api.common.v1 import Payload
from temporalio.converter import PayloadCodec

ENCODING = b"binary/encrypted-fernet"


class FernetPayloadCodec(PayloadCodec):
    def __init__(self, key: str) -> None:
        self._fernet = Fernet(key.encode())

    async def encode(self, payloads: Sequence[Payload]) -> list[Payload]:
        return [
            Payload(metadata={"encoding": ENCODING}, data=self._fernet.encrypt(p.SerializeToString()))
            for p in payloads
        ]

    async def decode(self, payloads: Sequence[Payload]) -> list[Payload]:
        out: list[Payload] = []
        for p in payloads:
            if p.metadata.get("encoding", b"") != ENCODING:
                out.append(p)
                continue
            out.append(Payload.FromString(self._fernet.decrypt(p.data)))
        return out


def data_converter(key: str | None) -> temporalio.converter.DataConverter:
    if not key:
        return temporalio.converter.default()
    return dataclasses.replace(temporalio.converter.default(), payload_codec=FernetPayloadCodec(key))
