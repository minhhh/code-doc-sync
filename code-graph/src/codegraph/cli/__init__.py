"""code-graph command line interface.

One module per verb group. Importing a group registers its commands on ``app``;
sub-apps are attached here so the top-level surface stays in one place.
"""

from __future__ import annotations

from . import lifecycle, lookup  # noqa: F401
from . import arch, change, flows, health, refactor  # noqa: F401
from .app import app
from .arch import arch_app
from .change import change_app
from .flows import flows_app
from .health import health_app
from .refactor import refactor_app

app.add_typer(change_app, name="change")
app.add_typer(health_app, name="health")
app.add_typer(flows_app, name="flows")
app.add_typer(arch_app, name="arch")
app.add_typer(refactor_app, name="refactor")

__all__ = ["app", "main"]


def main() -> None:
    app()
