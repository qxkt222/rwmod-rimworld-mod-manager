"""Profiles router.

Failure contract: missing profiles raise ``ModNotFoundError`` (404), bad input
raises ``ValidationError`` (400) and a write failure raises ``RwmodError``
(500). The 200 bodies below carry data only.
"""

from fastapi import APIRouter, Depends

from rwmod.config import Config
from rwmod.deps import get_config
from rwmod.errors import ModNotFoundError, RwmodError, ValidationError
from rwmod.models.schemas import (
    OkResponse,
    ProfileListResponse,
    ProfileSaveRequest,
    ProfileSaveResponse,
)

router = APIRouter(prefix="/api", tags=["profiles"])


def _profile_error(msg: str) -> Exception:
    """Map a profile module result message onto the right status code.

    ``profile.*`` keeps its ``{ok, msg}`` result contract for the CLI; the HTTP
    layer is where a failure must become a status code (CLAUDE.md).
    """
    if "不存在" in msg:
        return ModNotFoundError(msg)
    if "XML" in msg:
        return ValidationError(msg)
    return RwmodError(msg)


def _raise_on_failure(result: dict) -> dict:
    if not result.get("ok"):
        raise _profile_error(str(result.get("msg") or "操作失败"))
    return result


@router.get("/profiles", response_model=ProfileListResponse)
def list_profiles(
    cfg: Config = Depends(get_config),
):
    from rwmod.profile import list_profiles as _list
    from rwmod.profile import resolve_modsconfig_path

    return {
        "profiles": _list(),
        "modsconfig_path": str(path)
        if (path := resolve_modsconfig_path(cfg.rimworld_dir, config_dir=cfg.config_dir))
        else None,
    }


@router.post("/profiles/save", response_model=ProfileSaveResponse)
def save_profile(
    payload: ProfileSaveRequest,
    cfg: Config = Depends(get_config),
):
    name: str = payload.name.strip()
    if not name:
        raise ValidationError("需要提供 profile 名称")
    from rwmod.profile import resolve_modsconfig_path
    from rwmod.profile import save_profile as _save

    source = resolve_modsconfig_path(cfg.rimworld_dir, config_dir=cfg.config_dir)
    if not source:
        from rwmod.rimsort import generate_modsconfig

        return _raise_on_failure(_save(name, generate_modsconfig(cfg.mods_dir)))
    return _raise_on_failure(_save(name, source))


@router.post("/profiles/{name}/restore", response_model=ProfileSaveResponse)
def restore_profile(
    name: str,
    cfg: Config = Depends(get_config),
):
    from rwmod.profile import modsconfig_target
    from rwmod.profile import restore_profile as _restore
    from rwmod.undo import snapshot_modsconfig

    # Sandbox-checked; cfg.config_dir (when set) wins over LocalLow discovery.
    target = modsconfig_target(cfg.rimworld_dir, cfg.config_dir)
    if target.exists():
        snapshot_modsconfig(target, label="restore")
    return _raise_on_failure(_restore(name, target))


@router.delete("/profiles/{name}", response_model=OkResponse)
def delete_profile(
    name: str,
):
    from rwmod.profile import delete_profile as _delete

    if not _delete(name):
        raise ModNotFoundError(f"Profile 不存在: {name}")
    return {"ok": True}
