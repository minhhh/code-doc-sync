"""Option aliases shared by every command."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer

FormatOption = Annotated[
    str, typer.Option("--format", help="Output format: json (default) or text.")
]
PrettyOption = Annotated[bool, typer.Option("--pretty", help="Indent JSON output.")]
NoColorOption = Annotated[
    bool, typer.Option("--no-color", help="Disable ANSI color in text output.")
]
RootOption = Annotated[Optional[Path], typer.Option(help="Repository root.")]
