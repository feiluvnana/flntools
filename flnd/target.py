"""
Target parser for flnd.

Parses target strings in the format: <source code>:<want to download>.
The source codes are the keys of `flnd.sources.SOURCES`.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Target:
    """Represents a validated download target."""
    source: str
    value: str
    raw: str

    def __str__(self) -> str:
        return f"{self.source}:{self.value}"


def parse_target(raw_target: str) -> Target:
    """
    Parse and validate a single target string.

    Examples of valid targets:
      - ins:example_user
      - tik:example_creator
      - twt:example_handle
      - fbk:example.page

    Raises:
      ValueError: if the format is invalid or the source is not in SOURCES.
    """
    # flnd.sources.base imports Target, so importing the registry at the top would be circular.
    from flnd.sources import SOURCES, unknown_source

    cleaned = raw_target.strip()
    if ":" not in cleaned:
        raise ValueError(
            f"Invalid target format: '{raw_target}'. Expected format: <source>:<want to download>\n"
            f"Example: ins:example_user (sources: {', '.join(SOURCES)})"
        )

    source_code, _, value = cleaned.partition(":")
    source_code = source_code.strip().lower()
    value = value.strip()

    if source_code not in SOURCES:
        raise unknown_source(source_code)

    if not value:
        raise ValueError(f"Empty target value for source '{source_code}'. Example: {source_code}:username")

    # Clean username if provided with leading '@' (e.g., 'tik:@user' -> 'tik:user')
    if value.startswith("@"):
        value = value[1:]

    return Target(source=source_code, value=value, raw=raw_target)
