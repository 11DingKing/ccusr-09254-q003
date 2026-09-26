"""服务端业务模块。"""

from __future__ import annotations

from fastapi import FastAPI

from .eligibility_routers import router as eligibility_router
from .routers import router

app = FastAPI(
    title="Practice Hours Guard",
    version="0.1.0",
    description=(
        "Event-sourced practice-hours compliance service. Check-ins, mentor "
        "confirmations and leave corrections are append-only; compliance is "
        "derived by replay and can be frozen into an immutable snapshot. "
        "Internship check-ins additionally require an in-validity contract "
        "prerequisite snapshot (insurance, confidentiality agreement, safety "
        "training); deficient check-ins stay pending until retroactive review."
    ),
)

app.include_router(router)
app.include_router(eligibility_router)


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}
