"""Mod settings router — Config/Mod_*.xml listing, raw editing and backups.

Failure contract: missing files/directories raise ``ModNotFoundError`` (404),
invalid names/content/acks raise ``ValidationError`` (400). Successful writes
still answer 200 with data only; the running-game state is a warning field,
never an error.
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from rwmod.config import Config
from rwmod.deps import get_config
from rwmod.mod_settings import (
    backup_all,
    list_backups,
    list_mod_settings,
    read_mod_setting,
    restore_backup,
    write_mod_setting,
)

router = APIRouter(prefix="/api/mod-settings", tags=["mod-settings"])


class ModSettingWriteRequest(BaseModel):
    """PUT body — raw XML plus the required "game is closed" acknowledgement."""

    xml: str = ""
    ack_game_closed: bool = False


class ModSettingRestoreRequest(BaseModel):
    """POST restore body — empty ``backup`` means "the newest one"."""

    backup: str = ""
    ack_game_closed: bool = False


# Literal paths are declared before "/{filename}" so "backups" is never
# mistaken for a settings file name.


@router.get("")
def api_list_mod_settings(
    cfg: Config = Depends(get_config),
):
    return list_mod_settings(cfg.config_dir, cfg.mods_dir)


@router.get("/backups")
def api_list_mod_setting_backups():
    return list_backups()


@router.post("/backup-all")
def api_backup_all_mod_settings(
    cfg: Config = Depends(get_config),
):
    return backup_all(cfg.config_dir)


@router.get("/{filename}")
def api_get_mod_setting(
    filename: str,
    cfg: Config = Depends(get_config),
):
    return read_mod_setting(filename, cfg.config_dir)


@router.put("/{filename}")
def api_put_mod_setting(
    filename: str,
    payload: ModSettingWriteRequest,
    cfg: Config = Depends(get_config),
):
    return write_mod_setting(
        filename,
        payload.xml,
        config_dir=cfg.config_dir,
        ack_game_closed=payload.ack_game_closed,
    )


@router.post("/{filename}/restore")
def api_restore_mod_setting(
    filename: str,
    payload: ModSettingRestoreRequest,
    cfg: Config = Depends(get_config),
):
    return restore_backup(
        filename,
        payload.backup,
        config_dir=cfg.config_dir,
        ack_game_closed=payload.ack_game_closed,
    )
