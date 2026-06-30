from __future__ import annotations

from sportspredict.rates import MatchRates, RateModel

class AnchoredRateModel(RateModel):

    def build(self, ctx) -> MatchRates:
        from .learned import apply_ctx_rate_mult  # noqa: PLC0415

        max_ratio = float(self.s.raw.get("rates", {}).get("learned", {}).get("max_ratio", 3.0))
        return apply_ctx_rate_mult(super().build(ctx), ctx, max_ratio)

def make_rate_model(settings) -> RateModel:
    cfg = settings.raw.get("rates", {}) if hasattr(settings, "raw") else {}
    if cfg.get("model") == "learned":
        try:
            from .learned import LearnedRateModel  # noqa: PLC0415

            model = LearnedRateModel.from_settings(settings)
            if model is not None:
                return model
        except Exception:
            pass
    return AnchoredRateModel(settings)
