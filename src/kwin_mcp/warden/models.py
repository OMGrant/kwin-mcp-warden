"""Results of the warden's own tools (access_request, access_list)."""

from __future__ import annotations

from pydantic import BaseModel


class AccessResult(BaseModel):
    """The person's answer to an access request."""

    allowed: bool
    text: str

    def render(self) -> str:
        return self.text


class AccessList(BaseModel):
    """What this session holds now."""

    grants: list[str]

    def render(self) -> str:
        if not self.grants:
            return "This session holds no access. Ask with access_request."
        return "This session holds:\n" + "\n".join(f"- {g}" for g in self.grants)
