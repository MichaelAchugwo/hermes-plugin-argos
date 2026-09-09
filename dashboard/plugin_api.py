from __future__ import annotations

import asyncio

from fastapi import APIRouter, Body, HTTPException

from hermes_argos.authstore import AuthStore, AuthStoreError, SelectorError
from hermes_argos.config import load_config, save_config
from hermes_argos.pool import status

router = APIRouter()


@router.get("/status")
async def get_status(force: bool = False):
    return await asyncio.to_thread(status, force=force, apply_policy=True)


@router.post("/use")
async def use_account(payload: dict = Body(...)):
    selector = str(payload.get("selector") or "").strip()
    if not selector:
        raise HTTPException(status_code=400, detail="selector is required")
    store = AuthStore()
    try:
        selected = await asyncio.to_thread(store.activate, selector)
    except (AuthStoreError, SelectorError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    public = next(item for item in store.public_entries() if item["id"] == selected["id"])
    return {"ok": True, "active": public}


@router.post("/refresh")
async def refresh_usage():
    return await asyncio.to_thread(status, force=True, apply_policy=True)


@router.post("/auto")
async def set_auto(payload: dict = Body(...)):
    if not isinstance(payload.get("enabled"), bool):
        raise HTTPException(status_code=400, detail="enabled must be a boolean")
    config = load_config()
    config["auto_rotate"] = payload["enabled"]
    await asyncio.to_thread(save_config, config)
    return {"ok": True, "auto_rotate": config["auto_rotate"]}


@router.get("/health")
async def health():
    store = AuthStore()
    try:
        problems = await asyncio.to_thread(store.validate)
        count = len(store.public_entries())
        return {"ok": not problems, "account_count": count, "problems": problems}
    except AuthStoreError as exc:
        return {"ok": False, "account_count": 0, "problems": [str(exc)]}
