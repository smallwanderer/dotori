"""Versioned option ranges explicitly substantiated by an evaluation record."""
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, model_validator


class OptionConstraint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allowed_values: list | None = None
    minimum: FiniteFloat | None = None
    maximum: FiniteFloat | None = None

    @model_validator(mode="after")
    def validate_constraint(self):
        if self.allowed_values is not None:
            if not self.allowed_values or self.minimum is not None or self.maximum is not None:
                raise ValueError("Use either an explicit value set or a numeric range")
        elif self.minimum is None or self.maximum is None or self.minimum > self.maximum:
            raise ValueError("Numeric ranges require ordered minimum and maximum")
        return self

    def accepts(self, value):
        if self.allowed_values is not None:
            return any(type(value) is type(item) and value == item for item in self.allowed_values)
        return type(value) in (int, float) and self.minimum <= value <= self.maximum


class EvidenceApplicabilityProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile_id: str = Field(min_length=1)
    revision: str = Field(min_length=1)
    option_constraints: dict[str, OptionConstraint] = Field(default_factory=dict)


