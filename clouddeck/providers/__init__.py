"""Provider registry.  Third parties can ship a provider as a plain package:

    [project.entry-points."clouddeck.providers"]
    mycloud = "mycloud_pkg:MyProvider"      # a Provider subclass with a no-argument constructor
"""
from __future__ import annotations

from importlib import metadata

from .base import Provider, ProviderError, Unsupported  # noqa: F401

_REGISTRY: dict[str, type[Provider]] = {}


def register(cls: type[Provider]) -> type[Provider]:
    _REGISTRY[cls.id] = cls
    return cls


def _load() -> None:
    if _REGISTRY:
        return
    from .akash import AkashProvider
    from .demo import DemoProvider
    register(AkashProvider)
    register(DemoProvider)
    for ep in metadata.entry_points(group="clouddeck.providers"):
        try:
            register(ep.load())
        except Exception:  # a broken third-party plugin must not stop CloudDeck from starting
            continue


def available() -> dict[str, type[Provider]]:
    _load()
    return dict(_REGISTRY)


def get(name: str) -> Provider:
    _load()
    try:
        return _REGISTRY[name]()
    except KeyError:
        raise ProviderError(f"unknown provider '{name}' (available: {', '.join(sorted(_REGISTRY))})") from None
