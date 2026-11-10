"""
Target parser for flnd.

Parses target strings in the format: <source 3-letter code>:<want to download>
Supported sources:
  - tik: TikTok
  - fbk: Facebook
  - ins: Instagram
  - twt: Twitter / X
"""

from dataclasses import dataclass
from typing import Literal

# The 4 strictly supported sources
SUPPORTED_SOURCES = {"tik", "fbk", "ins", "twt"}
SourceCode = Literal["tik", "fbk", "ins", "twt"]


@dataclass(frozen=True)
class Target:
    """Represents a validated download target."""
    source: SourceCode
    value: str
    raw: str

    def __str__(self) -> str:
        return f"{self.source}:{self.value}"


def parse_target(raw_target: str) -> Target:
    """
    Parse and validate a single target string.

    Examples of valid targets:
      - ins:iamsan_198
      - tik:example_creator
      - twt:example_handle
      - fbk:example.page

    Raises:
      ValueError: if format is invalid or source is not one of the 4 supported.
    """
    cleaned = raw_target.strip()
    if ":" not in cleaned:
        raise ValueError(
            f"Invalid target format: '{raw_target}'. Expected format: <source>:<want to download>\n"
            f"Example: ins:iamsan_198 (supported sources: {', '.join(sorted(SUPPORTED_SOURCES))})"
        )

    source_code, _, value = cleaned.partition(":")
    source_code = source_code.strip().lower()
    value = value.strip()

    # Reject unsupported sources
    if source_code not in SUPPORTED_SOURCES:
        raise ValueError(
            f"Unsupported source '{source_code}'. flnd only supports: {', '.join(sorted(SUPPORTED_SOURCES))}"
        )

    if not value:
        raise ValueError(f"Empty target value for source '{source_code}'. Example: {source_code}:username")

    # Clean username if provided with leading '@' (e.g., 'tik:@user' -> 'tik:user')
    if value.startswith("@"):
        value = value[1:]

    return Target(source=source_code, value=value, raw=raw_target)
