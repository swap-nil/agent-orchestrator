"""STT, TTS and VAD provider factories.

Providers are chosen in configuration; options are passed straight to the
LiveKit plugin constructor, so a model, language or self-hosted ``base_url``
change needs no code change. Add a provider by adding one line here.
Plugin option names differ per plugin version: check the plugin you pin.
"""

from __future__ import annotations

import importlib
from typing import Any, Callable


def _deepgram_stt(options: dict[str, Any]) -> Any:
    from livekit.plugins import deepgram
    return deepgram.STT(**options)


def _azure_stt(options: dict[str, Any]) -> Any:
    from livekit.plugins import azure
    return azure.STT(**options)


def _cartesia_tts(options: dict[str, Any]) -> Any:
    from livekit.plugins import cartesia
    return cartesia.TTS(**options)


def _azure_tts(options: dict[str, Any]) -> Any:
    from livekit.plugins import azure
    return azure.TTS(**options)


def _elevenlabs_tts(options: dict[str, Any]) -> Any:
    from livekit.plugins import elevenlabs
    return elevenlabs.TTS(**options)


def _silero_vad(options: dict[str, Any]) -> Any:
    from livekit.plugins import silero
    return silero.VAD.load(**options)


STT_FACTORIES: dict[str, Callable[[dict[str, Any]], Any]] = {"deepgram": _deepgram_stt, "azure": _azure_stt}
TTS_FACTORIES: dict[str, Callable[[dict[str, Any]], Any]] = {
    "cartesia": _cartesia_tts, "azure": _azure_tts, "elevenlabs": _elevenlabs_tts,
}
VAD_FACTORIES: dict[str, Callable[[dict[str, Any]], Any]] = {"silero": _silero_vad}


def import_plugins(*modules: str) -> None:
    """Import LiveKit plugins up front: they register themselves and must do so on the main
    thread. Jobs run in a thread on some platforms (Windows), where a lazy import fails."""
    for module in modules:
        importlib.import_module(f"livekit.plugins.{module}")


def build(kind: str, factories: dict[str, Callable[[dict[str, Any]], Any]], provider: str, options: dict[str, Any]) -> Any:
    try:
        factory = factories[provider]
    except KeyError as exc:
        raise ValueError(f"unknown {kind} provider {provider!r}; known: {sorted(factories)}") from exc
    return factory(dict(options))
