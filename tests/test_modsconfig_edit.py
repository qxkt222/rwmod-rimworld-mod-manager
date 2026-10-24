"""ModsConfig.xml batch enable/disable — planning, rewriting, atomic writes.

The writer is the second write path for ModsConfig.xml (after rimsort/apply and
profile restore), so the safety rules from ``test_config_path_safety.py`` apply
here too:

* every write test declares ``RWMOD_TEST_SANDBOX_HOME`` and asserts the target
  lives under pytest's tmp tree *before* writing;
* only the ``<activeMods>`` child is rewritten — version, knownExpansions and
  any other node must survive byte-for-byte, CRLF and BOM style included;
* a ``.bak_<ts>`` copy and an undo snapshot are taken before the atomic
  ``os.replace``.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from rwmod.errors import ValidationError
from rwmod.modsconfig_edit import (
    active_order_needs_sort,
    plan_active_changes,
    read_active_mods,
    read_modsconfig_text,
    rewrite_active_mods,
    write_modsconfig_target,
)
from rwmod.profile import SANDBOX_ENV_VAR, SandboxViolationError

_HEADER = '<?xml version="1.0" encoding="utf-8"?>'


def _xml(
    active_ids: list[str],
    *,
    crlf: bool = True,
    bom: bool = False,
    extra: str = "",
) -> str:
    nl = "\r\n" if crlf else "\n"
    items = "".join(f"{nl}    <li>{pid}</li>" for pid in active_ids)
    body = (
        f"{_HEADER}{nl}"
        f"<ModsConfigData>{nl}"
        f"  <version>1.6.4850 rev645</version>{nl}"
        f"  <activeMods>{items}{nl}  </activeMods>{nl}"
        f"  <knownExpansions>{nl}"
        f"    <li>ludeon.rimworld.royalty</li>{nl}"
        f"  </knownExpansions>{nl}"
        f"{extra}"
        f"</ModsConfigData>{nl}"
    )
    return ("﻿" + body) if bom else body


def _write(path: Path, active_ids: list[str], **kwargs) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_xml(active_ids, **kwargs), encoding="utf-8", newline="")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ── read ───────────────────────────────────────────────────────────


class TestReadActiveMods:
    def test_returns_ids_in_file_order_with_case_preserved(self, tmp_path: Path):
        target = tmp_path / "ModsConfig.xml"
        _write(target, ["Brrainz.Harmony", "ludeon.rimworld", "pkg.a"])

        assert read_active_mods(target) == ["Brrainz.Harmony", "ludeon.rimworld", "pkg.a"]

    def test_missing_active_mods_is_validation_error(self, tmp_path: Path):
        target = tmp_path / "ModsConfig.xml"
        target.write_text(
            "<ModsConfigData><version>1.6</version></ModsConfigData>", encoding="utf-8"
        )

        with pytest.raises(ValidationError):
            read_active_mods(target)

    def test_malformed_xml_is_validation_error(self, tmp_path: Path):
        target = tmp_path / "ModsConfig.xml"
        target.write_text("<ModsConfigData><activeMods>", encoding="utf-8")

        with pytest.raises(ValidationError):
            read_active_mods(target)

    def test_text_reader_keeps_bom_marker(self, tmp_path: Path):
        target = tmp_path / "ModsConfig.xml"
        _write(target, ["a.b"], bom=True)

        text = read_modsconfig_text(target)
        assert text.startswith("﻿")
        assert "\r\n" in text


# ── planning ───────────────────────────────────────────────────────


class TestPlanActiveChanges:
    def test_enable_appends_at_end_without_reordering(self):
        plan = plan_active_changes(
            ["brrainz.harmony", "core.mod"],
            enable=["new.mod"],
            installed=["brrainz.harmony", "core.mod", "new.mod"],
        )

        assert plan.active == ["brrainz.harmony", "core.mod", "new.mod"]
        assert plan.added == ["new.mod"]
        assert plan.removed == []
        assert plan.skipped == []
        assert plan.needs_sort is False

    def test_enable_matches_installed_case_insensitively(self):
        plan = plan_active_changes(
            ["brrainz.harmony"],
            enable=["Author.NewMod"],
            installed=["author.newmod"],
        )

        assert plan.active == ["brrainz.harmony", "Author.NewMod"]
        assert plan.added == ["Author.NewMod"]

    def test_enable_unknown_id_is_skipped(self):
        plan = plan_active_changes(
            ["brrainz.harmony"],
            enable=["ghost.mod"],
            installed=["brrainz.harmony"],
        )

        assert plan.active == ["brrainz.harmony"]
        assert plan.added == []
        assert plan.skipped == ["ghost.mod"]

    def test_enable_official_core_and_dlc_is_allowed(self):
        plan = plan_active_changes(
            [],
            enable=["ludeon.rimworld", "ludeon.rimworld.royalty", "Core"],
        )

        assert plan.active == ["ludeon.rimworld", "ludeon.rimworld.royalty", "Core"]
        assert plan.skipped == []

    def test_enable_already_active_is_skipped_case_insensitively(self):
        plan = plan_active_changes(
            ["brrainz.harmony", "author.mod"],
            enable=["Author.Mod"],
            installed=["author.mod"],
        )

        assert plan.active == ["brrainz.harmony", "author.mod"]
        assert plan.added == []
        assert plan.skipped == ["Author.Mod"]

    def test_disable_removes_case_insensitively(self):
        plan = plan_active_changes(
            ["brrainz.harmony", "Author.Mod", "other.mod"],
            disable=["author.MOD"],
        )

        assert plan.active == ["brrainz.harmony", "other.mod"]
        assert plan.removed == ["Author.Mod"]
        assert plan.skipped == []

    def test_disable_not_active_is_skipped(self):
        plan = plan_active_changes(
            ["brrainz.harmony"],
            disable=["not.active"],
        )

        assert plan.active == ["brrainz.harmony"]
        assert plan.removed == []
        assert plan.skipped == ["not.active"]

    def test_same_id_in_enable_and_disable_is_skipped(self):
        plan = plan_active_changes(
            ["brrainz.harmony"],
            enable=["author.mod"],
            disable=["author.mod"],
            installed=["author.mod"],
        )

        assert plan.active == ["brrainz.harmony"]
        assert plan.added == []
        assert plan.removed == []
        assert plan.skipped == ["author.mod", "author.mod"]

    def test_duplicate_enable_ids_are_added_once(self):
        plan = plan_active_changes(
            [],
            enable=["author.mod", "Author.Mod"],
            installed=["author.mod"],
        )

        assert plan.active == ["author.mod"]
        assert plan.added == ["author.mod"]
        assert plan.skipped == ["Author.Mod"]

    def test_needs_sort_true_when_appending_harmony_after_community_mods(self):
        plan = plan_active_changes(
            ["community.mod"],
            enable=["brrainz.harmony"],
            installed=["brrainz.harmony", "community.mod"],
        )

        assert plan.active == ["community.mod", "brrainz.harmony"]
        assert plan.needs_sort is True

    def test_no_changes_keeps_list_untouched(self):
        plan = plan_active_changes(["b.mod", "a.mod"])

        assert plan.active == ["b.mod", "a.mod"]
        assert plan.needs_sort is False

    def test_active_order_needs_sort_accepts_its_own_output(self):
        assert active_order_needs_sort(["brrainz.harmony", "ludeon.rimworld", "a.b"]) is False
        assert active_order_needs_sort(["a.b", "ludeon.rimworld"]) is True


# ── rewrite ────────────────────────────────────────────────────────


class TestRewriteActiveMods:
    def test_preserves_everything_outside_active_mods(self):
        original = _xml(["old.mod"], crlf=True)
        rewritten = rewrite_active_mods(original, ["old.mod", "new.mod"])

        # version / knownExpansions / declaration survive verbatim.
        assert "<version>1.6.4850 rev645</version>" in rewritten
        assert "<knownExpansions>" in rewritten
        assert rewritten.startswith(_HEADER)
        assert "<li>old.mod</li>" in rewritten
        assert "<li>old.mod</li>\r\n    <li>new.mod</li>\r\n  </activeMods>\r\n" in rewritten
        assert "<li>new.mod</li>\r\n  </activeMods>\r\n  <knownExpansions>" in rewritten

    def test_preserves_crlf_line_endings(self):
        rewritten = rewrite_active_mods(_xml(["a.b"], crlf=True), ["a.b", "c.d"])

        assert "\r\n" in rewritten
        assert "\n" not in rewritten.replace("\r\n", "")

    def test_preserves_lf_line_endings(self):
        rewritten = rewrite_active_mods(_xml(["a.b"], crlf=False), ["a.b", "c.d"])

        assert "\r\n" not in rewritten

    def test_preserves_absence_of_bom(self, tmp_path: Path):
        target = tmp_path / "ModsConfig.xml"
        _write(target, ["a.b"], bom=False)
        rewritten = rewrite_active_mods(read_modsconfig_text(target), ["a.b", "c.d"])

        assert not rewritten.startswith("﻿")
        assert not rewritten.encode("utf-8").startswith(b"\xef\xbb\xbf")

    def test_preserves_bom_when_present(self):
        rewritten = rewrite_active_mods(_xml(["a.b"], bom=True), ["a.b", "c.d"])

        assert rewritten.startswith("﻿")

    def test_removing_all_ids_leaves_empty_element(self):
        rewritten = rewrite_active_mods(_xml(["a.b"]), [])

        assert "<activeMods></activeMods>" in rewritten

    def test_escapes_xml_special_characters(self):
        rewritten = rewrite_active_mods(_xml(["a.b"]), ["a&b"])

        assert "<li>a&amp;b</li>" in rewritten

    def test_expands_self_closing_active_mods(self):
        text = "<ModsConfigData><activeMods/><version>1.6</version></ModsConfigData>"

        rewritten = rewrite_active_mods(text, ["a.b"])

        assert "<activeMods>" in rewritten
        assert "<li>a.b</li>" in rewritten
        assert "</activeMods>" in rewritten

    def test_self_closing_active_mods_stays_explicit_when_emptied(self):
        text = "<ModsConfigData><activeMods /></ModsConfigData>"

        rewritten = rewrite_active_mods(text, [])

        assert rewritten == "<ModsConfigData><activeMods></activeMods></ModsConfigData>"

    def test_missing_active_mods_is_an_error(self):
        with pytest.raises(ValidationError):
            rewrite_active_mods("<ModsConfigData><version>1</version></ModsConfigData>", ["a.b"])

    def test_duplicate_active_mods_is_an_error(self):
        text = "<ModsConfigData><activeMods></activeMods><activeMods></activeMods></ModsConfigData>"
        with pytest.raises(ValidationError):
            rewrite_active_mods(text, ["a.b"])


# ── atomic writer ──────────────────────────────────────────────────


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A tmp sandbox root; every write test asserts targets live under it."""
    import rwmod.undo as undo_mod

    root = tmp_path / "sandbox"
    (root / "Config").mkdir(parents=True)
    monkeypatch.setenv(SANDBOX_ENV_VAR, str(root))
    monkeypatch.setattr(undo_mod, "UNDO_DIR", root / "undo")
    return root


class TestWriteModsconfigTarget:
    def test_backs_up_snapshots_and_replaces_atomically(self, sandbox: Path):
        target = sandbox / "Config" / "ModsConfig.xml"
        _write(target, ["old.mod"])
        before = _sha(target)
        assert target.is_relative_to(sandbox)

        backup = write_modsconfig_target(
            target, rewrite_active_mods(read_modsconfig_text(target), ["old.mod", "new.mod"])
        )

        assert backup is not None
        assert backup.is_relative_to(sandbox)
        assert backup.name.startswith("ModsConfig.xml.bak_")
        assert _sha(backup) == before
        assert "<li>new.mod</li>" in target.read_text(encoding="utf-8")
        # Undo snapshot landed in the patched undo dir (per-instance subdir),
        # not the real ~/.rwmod.
        snapshots = list((sandbox / "undo").rglob("*.xml"))
        assert len(snapshots) == 1
        assert _sha(snapshots[0]) == before
        # No temp file is left behind.
        assert not list(target.parent.glob("*.rwmod.tmp"))

    def test_refuses_to_write_when_target_escapes_sandbox(self, sandbox: Path, tmp_path: Path):
        outside = tmp_path / "outside" / "ModsConfig.xml"
        _write(outside, ["old.mod"])
        assert not outside.is_relative_to(sandbox)

        with pytest.raises(SandboxViolationError):
            write_modsconfig_target(outside, "<ModsConfigData/>")

        assert outside.read_text(encoding="utf-8").startswith(_HEADER)

    def test_refuses_a_non_modsconfig_filename(self, sandbox: Path):
        target = sandbox / "Config" / "Other.xml"

        with pytest.raises(ValidationError):
            write_modsconfig_target(target, "<ModsConfigData/>")

    def test_writes_new_file_without_backup_when_absent(self, sandbox: Path):
        target = sandbox / "Config" / "ModsConfig.xml"
        assert not target.exists()

        backup = write_modsconfig_target(target, _xml(["a.b"]))

        assert backup is None
        assert target.read_text(encoding="utf-8").startswith(_HEADER)
        assert not os.listdir(sandbox / "undo") if (sandbox / "undo").exists() else True
