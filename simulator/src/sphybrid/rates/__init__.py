from __future__ import annotations

__all__ = ["make_rate_model"]

def make_rate_model(settings):
    from .factory import make_rate_model as _impl

    return _impl(settings)
