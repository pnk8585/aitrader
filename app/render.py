"""HTMX partial-render helper."""

from __future__ import annotations

from fastapi import Request
from fastapi.templating import Jinja2Templates

_templates = Jinja2Templates("app/templates")


def partial(request: Request, name: str, **ctx) -> str:
    """Return an HTML fragment for HTMX responses."""
    return _templates.get_template(name).render({"request": request, **ctx})
