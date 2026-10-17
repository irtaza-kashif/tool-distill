"""Library settings. The ``tooldistill.toml`` loader is added with the SDK facade."""

from dataclasses import dataclass

DEFAULT_DISTILLER_MODEL = "claude-sonnet-5-5"


@dataclass(slots=True)
class Settings:
    """Tunable behaviour; defaults and their consequences are documented in CLAUDE.md."""

    distiller_model: str = DEFAULT_DISTILLER_MODEL
    fallback_distiller: bool = True
    auto_promote: bool = False
    min_calls_to_distill: int = 8
    min_call_reduction: float = 0.3
    max_exposed_tools: int = 20
    allow_live_verification: bool = False
    check_schemas_on_start: bool = True

    def __post_init__(self) -> None:
        if self.min_calls_to_distill < 1:
            raise ValueError("min_calls_to_distill must be at least 1")
        if not 0.0 <= self.min_call_reduction < 1.0:
            raise ValueError("min_call_reduction must be in [0, 1)")
        if self.max_exposed_tools < 0:
            raise ValueError("max_exposed_tools must not be negative")
