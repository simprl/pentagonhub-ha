"""Config-entry mode compatibility helpers."""

from __future__ import annotations


VALID_INSTALLATION_MODES = ("live", "dev")


def resolve_installation_mode(stored_mode: object, build_profile: str) -> str:
    """Resolve a legacy entry mode without weakening explicit mode checks."""

    if build_profile not in VALID_INSTALLATION_MODES:
        raise ValueError(f"Unsupported PentagonHub HA build profile: {build_profile}")
    if stored_mode is None:
        return build_profile
    if not isinstance(stored_mode, str) or stored_mode not in VALID_INSTALLATION_MODES:
        raise ValueError(f"Unsupported PentagonHub HA installation mode: {stored_mode}")
    return stored_mode
