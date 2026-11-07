"""Probe tests for parsers, metadata, sorting, i18n, RimPy DB and the CLI.

Scope: parser.py / metadata.py / save_parser.py / xmlutil.py / rimsort.py /
load_order.py / mod_conflicts.py / version_support.py / rimpy_db.py / i18n.py /
cli.py.  These tests target branches the existing suite does not cover:
malformed input, encoding edge cases, graph edge cases and CLI exit codes.

Everything runs offline: the two places that could reach the network
(RimPyDB._download, SteamCMD) are monkeypatched, and `download_one` is faked
for CLI success paths.  All file I/O stays under tmp_path; Path.home is
redirected so the developer's real ModsConfig.xml / profiles / DB are never
read or written.
"""

from __future__ import annotations

import json
import os
import platform
import struct
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rwmod.errors import ModNotFoundError, ValidationError
from rwmod.load_order import check_load_order
from rwmod.metadata import (
    installed_package_ids,
    package_id_to_workshop_id,
    read_mod_metadata,
)
from rwmod.mod_conflicts import find_mod_conflicts, scan_mod_metadata
from rwmod.parser import (
    _parse_legacy_collection,
    parse_collection_dir,
    parse_modlist_file,
    parse_mods_config,
    resolve_workshop_ids,
)
from rwmod.rimsort import (
    _collect_load_rules,
    compare_modsconfig,
    generate_modsconfig,
    parse_modsconfig,
    resolve_missing_workshop_ids,
    sort_mods,
)
from rwmod.save_parser import _extract_game_version, analyze_save, parse_save_mods
from rwmod.version_support import _normalize_version, check_version_support, detect_rimworld_version

runner = CliRunner()


# ── helpers ────────────────────────────────────────────────────────


def _make_mod(
    mods_dir: Path,
    folder: str,
    package_id: str,
    *,
    name: str | None = None,
    extra: str = "",
    workshop_id: str | None = None,
) -> Path:
    about = mods_dir / folder / "About"
    about.mkdir(parents=True, exist_ok=True)
    display = name if name is not None else folder
    (about / "About.xml").write_text(
        f"<ModMetaData><name>{display}</name><packageId>{package_id}</packageId>{extra}</ModMetaData>",
        encoding="utf-8",
    )
    if workshop_id is not None:
        (about / "PublishedFileId.txt").write_text(workshop_id, encoding="utf-8")
    return mods_dir / folder


def _modsconfig_xml(active: list[str], expansions: list[str] | None = None) -> str:
    lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        "<ModsConfigData>",
        "  <version>1.5.4297 rev1117</version>",
        "  <activeMods>",
    ]
    lines += [f"    <li>{pid}</li>" for pid in active]
    lines += ["  </activeMods>"]
    if expansions:
        lines += ["  <knownExpansions>"]
        lines += [f"    <li>{pid}</li>" for pid in expansions]
        lines += ["  </knownExpansions>"]
    lines += ["</ModsConfigData>"]
    return "\n".join(lines)


def _output(result) -> str:
    return result.output + getattr(result, "stderr", "")


def _patch_download_one(monkeypatch: pytest.MonkeyPatch, calls: list[str], ok: bool = True):
    import rwmod.cli as cli_mod

    def fake(cfg, mod_id: str, force: bool = False) -> bool:
        calls.append(mod_id)
        return ok

    monkeypatch.setattr(cli_mod, "download_one", fake)


# ── parser: modlist robustness ─────────────────────────────────────


class TestModlistRobustness:
    def test_utf8_bom_drops_first_id(self, tmp_path: Path):
        """A UTF-8 BOM is valid UTF-8; the first line must not be lost."""
        f = tmp_path / "bom.txt"
        f.write_bytes("﻿2009463077\n1234567890\n".encode())
        ids = parse_modlist_file(f)
        assert "2009463077" in ids  # currently dropped

    def test_gbk_modlist_does_not_crash(self, tmp_path: Path):
        """Chinese comments in a cp936 file are common on Windows."""
        f = tmp_path / "gbk.txt"
        f.write_bytes("# 中文注释\n2009463077\n".encode("gbk"))
        ids = parse_modlist_file(f)
        assert "2009463077" in ids

    def test_first_line_comment_swallows_bom(self, tmp_path: Path):
        """BOM in front of a comment is harmless — documents why the bug hides."""
        f = tmp_path / "bom-comment.txt"
        f.write_bytes("﻿# comment\n2009463077\n".encode())
        assert parse_modlist_file(f) == ["2009463077"]

    def test_duplicates_preserved_in_order(self, tmp_path: Path):
        f = tmp_path / "dup.txt"
        f.write_text("111\n222\n111\n")
        assert parse_modlist_file(f) == ["111", "222", "111"]

    def test_url_with_extra_params(self, tmp_path: Path):
        f = tmp_path / "url.txt"
        f.write_text("https://steamcommunity.com/sharedfiles/filedetails/?id=2009463077&x=1\n")
        assert parse_modlist_file(f) == ["2009463077"]

    def test_embedded_digits_are_not_ids(self, tmp_path: Path):
        f = tmp_path / "junk.txt"
        f.write_text("mod123\n12a\n\t42\n")
        assert parse_modlist_file(f) == ["42"]

    def test_crlf(self, tmp_path: Path):
        f = tmp_path / "crlf.txt"
        f.write_bytes(b"111\r\n222\r\n")
        assert parse_modlist_file(f) == ["111", "222"]


# ── parser: collection dirs ────────────────────────────────────────


class TestCollectionDirEdges:
    def test_invalid_utf8_json_does_not_crash(self, tmp_path: Path):
        d = tmp_path / "coll"
        d.mkdir()
        (d / "collection.json").write_bytes(
            '{"children": [{"publishedfileid": "中文"}]}'.encode("gbk")
        )
        ids = parse_collection_dir(d)
        assert isinstance(ids, list)

    def test_flat_sequence_with_small_first_dword(self):
        # first 4 bytes look like a count but are > 10000 -> header path skipped
        data = struct.pack("<Q", 2009463077) + struct.pack("<Q", 1234567890)
        assert _parse_legacy_collection(data) == ["2009463077", "1234567890"]

    def test_truncated_or_misaligned_bin_yields_nothing(self, tmp_path: Path):
        """A 4-byte count shifts the ids, and the flat fallback cannot
        re-align them — a truncated .bin turns into zero ids, not an error."""
        d = tmp_path / "coll"
        d.mkdir()
        data = struct.pack("<I", 3) + struct.pack("<Q", 1111111111) + struct.pack("<Q", 2222222222)
        (d / "x_legacy.bin").write_bytes(data)
        assert parse_collection_dir(d) == []

    def test_negative_sentinel_filtered(self):
        data = struct.pack("<Q", 0) + struct.pack("<Q", 2009463077)
        assert _parse_legacy_collection(data) == ["2009463077"]

    def test_json_null_scalar_is_ignored(self, tmp_path: Path):
        d = tmp_path / "coll"
        d.mkdir()
        (d / "c.json").write_text(json.dumps(None))
        assert parse_collection_dir(d) == []

    def test_json_dedup_keeps_first_occurrence(self, tmp_path: Path):
        d = tmp_path / "coll"
        d.mkdir()
        data = {"children": [{"publishedfileid": "111"}, "222", {"id": "111"}]}
        (d / "c.json").write_text(json.dumps(data))
        assert parse_collection_dir(d) == ["111", "222"]

    def test_bin_and_json_combined_order(self, tmp_path: Path):
        d = tmp_path / "coll"
        d.mkdir()
        (d / "a.bin").write_bytes(struct.pack("<Q", 111))
        (d / "b.json").write_text(json.dumps({"mods": [{"id": "222"}]}))
        assert parse_collection_dir(d) == ["111", "222"]

    def test_missing_dir_returns_empty(self, tmp_path: Path):
        assert parse_collection_dir(tmp_path / "nope") == []

    def test_top_level_dict_and_plain_string_list(self, tmp_path: Path):
        d = tmp_path / "coll"
        d.mkdir()
        (d / "a.json").write_text(json.dumps({"items": ["333"]}))
        # a bare list item that is itself a mod object is NOT scanned for ids
        # (only wrapper keys children/items/mods are), so "222" is dropped
        (d / "b.json").write_text(json.dumps(["111", {"publishedfileid": "222"}]))
        assert parse_collection_dir(d) == ["333", "111"]

    def test_files_created_in_reverse_name_order_still_sort_by_name(self, tmp_path: Path):
        """iterdir() yields creation order on NTFS but not on ext4, so the
        result must be ordered by file name on every platform."""
        d = tmp_path / "coll"
        d.mkdir()
        (d / "z.json").write_text(json.dumps({"items": ["333"]}))
        (d / "a.json").write_text(json.dumps(["111"]))
        assert parse_collection_dir(d) == ["111", "333"]

    def test_json_syntax_error_is_skipped(self, tmp_path: Path):
        d = tmp_path / "coll"
        d.mkdir()
        (d / "bad.json").write_text("{not json")
        assert parse_collection_dir(d) == []

    def test_tiny_bin_file_returns_nothing(self, tmp_path: Path):
        assert _parse_legacy_collection(b"\x01\x02\x03") == []

    def test_subdirectory_without_suffix_is_skipped(self, tmp_path: Path):
        d = tmp_path / "coll"
        d.mkdir()
        (d / "content").mkdir()  # suffix == "" -> read_bytes raises, must be swallowed
        assert parse_collection_dir(d) == []


# ── parser: ModsConfig.xml ─────────────────────────────────────────


class TestModsConfigParsing:
    def test_known_expansions_are_not_mods(self, tmp_path: Path):
        """ModsConfig.xml separates activeMods from knownExpansions."""
        f = tmp_path / "ModsConfig.xml"
        f.write_text(_modsconfig_xml(["brrainz.harmony"], expansions=["ludeon.rimworld.royalty"]))
        assert parse_mods_config(f) == ["brrainz.harmony"]

    def test_scoped_parse_agrees_with_rimsort(self, tmp_path: Path):
        f = tmp_path / "ModsConfig.xml"
        f.write_text(_modsconfig_xml(["a.mod", "b.mod"], expansions=["ludeon.rimworld.ideology"]))
        assert parse_mods_config(f) == parse_modsconfig(f)["active_mods"]

    def test_empty_li_text_skipped(self, tmp_path: Path):
        f = tmp_path / "ModsConfig.xml"
        f.write_text(
            "<ModsConfigData><activeMods><li></li><li>  </li><li>a.mod</li></activeMods></ModsConfigData>"
        )
        assert parse_mods_config(f) == ["", "a.mod"]

    def test_malformed_xml_raises_validation_error(self, tmp_path: Path):
        """REV-003: a corrupt upload is a 400, never "the list is empty"."""
        f = tmp_path / "ModsConfig.xml"
        f.write_text("<<<not xml>>>")
        with pytest.raises(ValidationError):
            parse_mods_config(f)

    def test_entity_expansion_rejected(self, tmp_path: Path):
        f = tmp_path / "ModsConfig.xml"
        f.write_text(
            '<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY a "aaaa">'
            '<!ENTITY b "&a;&a;&a;&a;">]><ModsConfigData><activeMods>'
            "<li>&b;</li></activeMods></ModsConfigData>"
        )
        with pytest.raises(ValidationError):
            parse_mods_config(f)

    def test_missing_file_raises_validation_error(self, tmp_path: Path):
        with pytest.raises(ValidationError):
            parse_mods_config(tmp_path / "nope.xml")

    def test_bom_xml_parses(self, tmp_path: Path):
        f = tmp_path / "ModsConfig.xml"
        f.write_bytes(
            "﻿<ModsConfigData><activeMods><li>a.mod</li></activeMods></ModsConfigData>".encode()
        )
        assert parse_mods_config(f) == ["a.mod"]


class TestResolveWorkshopIds:
    def test_case_mismatch_resolves(self, tmp_path: Path):
        """ModsConfig/saves lowercase ids; About.xml keeps the author's case.

        A case-only difference used to be reported as an unknown mod (it was
        then re-downloaded even though it was installed). packageIds are now
        compared through the normalised key.
        """
        _make_mod(tmp_path, "A", "author.mod", workshop_id="111")
        known, unknown = resolve_workshop_ids(["Author.Mod"], tmp_path)
        assert known == ["111"]
        assert unknown == []

    def test_duplicates_are_preserved(self, tmp_path: Path):
        _make_mod(tmp_path, "A", "author.mod", workshop_id="111")
        known, unknown = resolve_workshop_ids(["author.mod", "author.mod"], tmp_path)
        assert known == ["111", "111"]

    def test_missing_mods_dir_is_unknown(self, tmp_path: Path):
        known, unknown = resolve_workshop_ids(["a.mod"], tmp_path / "nope")
        assert known == [] and unknown == ["a.mod"]


# ── xmlutil safety ─────────────────────────────────────────────────


class TestXmlUtilSafety:
    def test_billion_laughs_rejected_by_defusedxml(self, tmp_path: Path):
        from defusedxml.common import DefusedXmlException

        from rwmod.xmlutil import parse_xml_root

        f = tmp_path / "bomb.xml"
        f.write_text(
            '<?xml version="1.0"?><!DOCTYPE lolz ['
            '<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;">'
            '<!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;">]>'
            "<root>&lol3;</root>"
        )
        with pytest.raises(DefusedXmlException):
            parse_xml_root(f)

    def test_external_entity_rejected(self, tmp_path: Path):
        from defusedxml.common import DefusedXmlException

        from rwmod.xmlutil import parse_xml_root

        f = tmp_path / "xxe.xml"
        f.write_text(
            '<?xml version="1.0"?><!DOCTYPE r [<!ENTITY e SYSTEM "file:///etc/passwd">]><r>&e;</r>'
        )
        with pytest.raises(DefusedXmlException):
            parse_xml_root(f)

    def test_missing_file_raises_oserror(self, tmp_path: Path):
        from rwmod.xmlutil import parse_xml_root

        with pytest.raises(OSError):
            parse_xml_root(tmp_path / "nope.xml")

    def test_parse_about_field_strips_and_defaults(self, tmp_path: Path):
        from rwmod.xmlutil import parse_about_field

        f = tmp_path / "About.xml"
        f.write_text("<ModMetaData><name>  Spaced Name  </name></ModMetaData>")
        assert parse_about_field(f, "name") == "Spaced Name"
        assert parse_about_field(f, "missing") == ""

    def test_parse_about_field_bomb_returns_empty(self, tmp_path: Path):
        from rwmod.xmlutil import parse_about_field

        f = tmp_path / "About.xml"
        f.write_text('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e "e">]><x><name>&e;</name></x>')
        assert parse_about_field(f, "name") == ""


# ── metadata edges ─────────────────────────────────────────────────


class TestMetadataEdges:
    def test_bomb_about_degrades_without_crash(self, tmp_path: Path):
        d = tmp_path / "bomb"
        about = d / "About"
        about.mkdir(parents=True)
        (about / "About.xml").write_text(
            '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e "&e;&e;">]><ModMetaData>&e;</ModMetaData>'
        )
        meta = read_mod_metadata(d)
        assert meta is not None
        assert meta.package_id == ""
        assert meta.name == "?"

    def test_supported_versions_collected(self, tmp_path: Path):
        d = tmp_path / "sv"
        about = d / "About"
        about.mkdir(parents=True)
        (about / "About.xml").write_text(
            "<ModMetaData><packageId>a.b</packageId><supportedVersions>"
            "<li>1.4</li><li>1.5</li></supportedVersions></ModMetaData>"
        )
        meta = read_mod_metadata(d)
        assert meta is not None
        assert meta.supported_versions == ["1.4", "1.5"]

    def test_package_id_whitespace_is_not_stripped(self, tmp_path: Path):
        d = tmp_path / "ws"
        about = d / "About"
        about.mkdir(parents=True)
        (about / "About.xml").write_text(
            "<ModMetaData><packageId>  author.mod  </packageId></ModMetaData>"
        )
        meta = read_mod_metadata(d)
        assert meta is not None
        assert meta.package_id == "  author.mod  "  # no strip, unlike rimsort

    def test_workshop_id_whitespace_and_non_numeric(self, tmp_path: Path):
        d = tmp_path / "wid"
        about = d / "About"
        about.mkdir(parents=True)
        (about / "About.xml").write_text("<ModMetaData><packageId>a.b</packageId></ModMetaData>")
        pf = about / "PublishedFileId.txt"
        pf.write_text("  2009463077  \n")
        meta = read_mod_metadata(d)
        assert meta is not None and meta.workshop_id == "2009463077"
        pf.write_text("12 34")
        meta = read_mod_metadata(d)
        assert meta is not None and meta.workshop_id == ""

    def test_installed_metas_skip_missing_package_id(self, tmp_path: Path):
        _make_mod(tmp_path, "nopkg", "", name="No Pkg")
        _make_mod(tmp_path, "ok", "a.b")
        assert installed_package_ids(tmp_path) == {"a.b"}
        assert package_id_to_workshop_id(tmp_path) == {}

    def test_duplicate_package_ids_collapse(self, tmp_path: Path):
        _make_mod(tmp_path, "A1", "dup.mod", workshop_id="111")
        _make_mod(tmp_path, "A2", "dup.mod", workshop_id="222")
        assert installed_package_ids(tmp_path) == {"dup.mod"}
        # last directory in sorted order wins
        assert package_id_to_workshop_id(tmp_path) == {"dup.mod": "222"}

    def test_installed_metas_missing_dir(self, tmp_path: Path):
        assert installed_package_ids(tmp_path / "nope") == set()

    def test_published_file_id_unreadable_or_invalid_utf8(self, tmp_path: Path):
        d = tmp_path / "wid"
        about = d / "About"
        about.mkdir(parents=True)
        (about / "About.xml").write_text("<ModMetaData><packageId>a.b</packageId></ModMetaData>")
        pf = about / "PublishedFileId.txt"
        pf.mkdir()  # OSError on read
        meta = read_mod_metadata(d)
        assert meta is not None and meta.workshop_id == ""
        pf.rmdir()
        pf.write_bytes(b"\xff\xfe123")  # UnicodeDecodeError
        meta = read_mod_metadata(d)
        assert meta is not None and meta.workshop_id == ""


# ── save parser edges ──────────────────────────────────────────────


class TestSaveParserEdges:
    @pytest.mark.parametrize("pad", [1_999_970, 2_100_000])
    def test_modids_beyond_read_window_should_be_found(self, tmp_path: Path, pad: int):
        """Only the first 2 MB is read; a modIds section past the window
        yields [] instead of an error — and analyze_save then calls the save
        loadable with completeness 1.0."""
        f = tmp_path / "big.rws"
        f.write_text(
            "<savegame>" + "x" * pad + "<modIds><li>a.mod</li></modIds></savegame>",
            encoding="utf-8",
        )
        assert parse_save_mods(f) == ["a.mod"]

    def test_modids_straddling_the_window_is_lost(self, tmp_path: Path):
        f = tmp_path / "straddle.rws"
        prefix = "<savegame>" + "y" * 1_999_970
        assert len(prefix) + len("<modIds>") < 2_000_000
        f.write_text(prefix + "<modIds><li>a.mod</li></modIds></savegame>", encoding="utf-8")
        assert parse_save_mods(f) == ["a.mod"]

    def test_gbk_comment_before_modids_still_parses(self, tmp_path: Path):
        f = tmp_path / "gbk.rws"
        raw = "<?xml version='1.0'?>\n<!-- 中文注释 -->\n<savegame><modIds><li>a.mod</li></modIds></savegame>"
        f.write_bytes(raw.encode("gbk"))
        assert parse_save_mods(f) == ["a.mod"]

    def test_utf16_save_yields_nothing_without_crash(self, tmp_path: Path):
        f = tmp_path / "u16.rws"
        f.write_bytes("<savegame><modIds><li>a.mod</li></modIds></savegame>".encode("utf-16"))
        assert parse_save_mods(f) == []

    def test_unclosed_modids_returns_empty(self, tmp_path: Path):
        assert parse_save_mods("<savegame><modIds><li>a.mod</li>") == []

    def test_empty_modids_section(self, tmp_path: Path):
        assert parse_save_mods("<savegame><modIds></modIds></savegame>") == []

    def test_non_utf8_path_no_crash(self, tmp_path: Path):
        f = tmp_path / "bad.rws"
        f.write_bytes(b"\xff\xfe\x00\x01<modIds><li>a.mod</li></modIds>")
        assert isinstance(parse_save_mods(f), list)

    def test_analyze_save_all_missing_completeness(self, tmp_path: Path):
        f = tmp_path / "s.rws"
        f.write_text("<savegame><modIds><li>a.mod</li><li>b.mod</li></modIds></savegame>")
        result = analyze_save(f, set())
        assert result["missing_mods"] == ["a.mod", "b.mod"]
        assert result["completeness"] == 0.5
        assert result["loadable"] is False

    def test_analyze_save_no_mods_is_complete(self, tmp_path: Path):
        f = tmp_path / "s.rws"
        f.write_text("<savegame><gameVersion>1.5</gameVersion></savegame>")
        result = analyze_save(f, {"installed.mod"})
        assert result["total_mods"] == 0
        assert result["completeness"] == 1.0


class TestFindSaveFiles:
    @pytest.mark.skipif(platform.system() != "Windows", reason="Windows layout only")
    def test_finds_both_known_windows_locations(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        low = (
            tmp_path
            / "AppData"
            / "LocalLow"
            / "Ludeon Studios"
            / "RimWorld by Ludeon Studios"
            / "Saves"
        )
        low.mkdir(parents=True)
        (low / "A.rws").write_text("<savegame/>")
        rw = tmp_path / "RimWorld"
        (rw / "Saves").mkdir(parents=True)
        (rw / "Saves" / "B.rws").write_text("<savegame/>")
        from rwmod.save_parser import find_save_files

        found = find_save_files(rw)
        assert {p.name for p in found} == {"A.rws", "B.rws"}

    def test_extract_game_version_binary_garbage(self, tmp_path: Path):
        f = tmp_path / "g.rws"
        f.write_bytes(b"\x00\x01\x02")
        assert _extract_game_version(f) is None


# ── sort_mods graph edges ──────────────────────────────────────────


class TestSortModsGraph:
    def test_official_active_id_is_anchored_not_unresolved(self, tmp_path: Path):
        """S-P0-3: Core/DLC live outside mods_dir but are anchored content,
        not unresolved mods."""
        _make_mod(tmp_path, "A", "a.mod")
        result = sort_mods(tmp_path, ["a.mod", "ludeon.rimworld"])
        assert result["sorted"] == ["ludeon.rimworld", "a.mod"]
        assert result["unresolved"] == []

    def test_active_ids_match_case_insensitively(self, tmp_path: Path):
        """S-P0-2: real ModsConfig uses lowercase ids; About.xml is mixed case."""
        _make_mod(tmp_path, "A", "Author.Mod", extra="<loadAfter><li>OTHER.mod</li></loadAfter>")
        _make_mod(tmp_path, "B", "Other.Mod")
        result = sort_mods(tmp_path, ["AUTHOR.MOD", "other.mod"])
        assert result["unresolved"] == []
        assert set(result["sorted"]) == {"AUTHOR.MOD", "other.mod"}
        assert result["sorted"].index("other.mod") < result["sorted"].index("AUTHOR.MOD")

    def test_missing_mods_dir_returns_empty(self, tmp_path: Path):
        result = sort_mods(tmp_path / "nope")
        assert result == {
            "sorted": [],
            "unresolved": [],
            "cycles": [],
            "violations": [],
            "rimworld_version": "",
        }

    def test_active_subset_ignores_others(self, tmp_path: Path):
        _make_mod(tmp_path, "A", "a.mod", extra="<loadAfter><li>b.mod</li></loadAfter>")
        _make_mod(tmp_path, "B", "b.mod")
        result = sort_mods(tmp_path, ["a.mod"])
        assert result["sorted"] == ["a.mod"]
        rules = _collect_load_rules(tmp_path)
        assert rules["a.mod"]["loadAfter"] == {"b.mod"}

    def test_self_cycle_detected(self, tmp_path: Path):
        _make_mod(tmp_path, "A", "a.mod", extra="<loadAfter><li>a.mod</li></loadAfter>")
        result = sort_mods(tmp_path)
        assert result["sorted"] == []
        assert result["unresolved"] == ["a.mod"]
        assert ["a.mod"] in result["cycles"]

    def test_two_node_cycle(self, tmp_path: Path):
        _make_mod(tmp_path, "A", "a.mod", extra="<loadAfter><li>b.mod</li></loadAfter>")
        _make_mod(tmp_path, "B", "b.mod", extra="<loadAfter><li>a.mod</li></loadAfter>")
        result = sort_mods(tmp_path)
        assert set(result["unresolved"]) == {"a.mod", "b.mod"}

    def test_dependent_of_cycle_is_unresolved(self, tmp_path: Path):
        _make_mod(tmp_path, "A", "a.mod", extra="<loadAfter><li>b.mod</li></loadAfter>")
        _make_mod(tmp_path, "B", "b.mod", extra="<loadAfter><li>c.mod</li></loadAfter>")
        _make_mod(tmp_path, "C", "c.mod", extra="<loadAfter><li>b.mod</li></loadAfter>")
        result = sort_mods(tmp_path)
        assert result["sorted"] == []
        assert set(result["unresolved"]) == {"a.mod", "b.mod", "c.mod"}

    def test_acyclic_loadafter_and_loadbefore(self, tmp_path: Path):
        _make_mod(tmp_path, "C", "c.mod")
        _make_mod(tmp_path, "A", "a.mod", extra="<loadAfter><li>b.mod</li></loadAfter>")
        _make_mod(tmp_path, "B", "b.mod", extra="<loadBefore><li>c.mod</li></loadBefore>")
        result = sort_mods(tmp_path)
        assert result["unresolved"] == []
        assert result["sorted"].index("b.mod") < result["sorted"].index("a.mod")
        assert result["sorted"].index("b.mod") < result["sorted"].index("c.mod")

    def test_loadtop_wins_over_loadafter(self, tmp_path: Path):
        _make_mod(
            tmp_path,
            "A",
            "a.mod",
            extra="<loadTop>true</loadTop><loadAfter><li>b.mod</li></loadAfter>",
        )
        _make_mod(tmp_path, "B", "b.mod")
        result = sort_mods(tmp_path)
        assert result["unresolved"] == []
        assert result["sorted"][0] == "a.mod"

    def test_loadbottom_wins_over_loadbefore(self, tmp_path: Path):
        _make_mod(
            tmp_path,
            "A",
            "a.mod",
            extra="<loadBottom>true</loadBottom><loadBefore><li>b.mod</li></loadBefore>",
        )
        _make_mod(tmp_path, "B", "b.mod")
        result = sort_mods(tmp_path)
        assert result["unresolved"] == []
        assert result["sorted"][-1] == "a.mod"

    def test_unknown_dependency_is_ignored_not_unresolved(self, tmp_path: Path):
        _make_mod(tmp_path, "A", "a.mod", extra="<loadAfter><li>ghost.mod</li></loadAfter>")
        result = sort_mods(tmp_path)
        # docstring calls `unresolved` "mods with cyclic/unknown deps",
        # but an unknown dep neither blocks nor is reported
        assert result["sorted"] == ["a.mod"]
        assert result["unresolved"] == []

    def test_multiple_loadafter_elements_union(self, tmp_path: Path):
        _make_mod(
            tmp_path,
            "A",
            "a.mod",
            extra="<loadAfter><li>b.mod</li></loadAfter><loadAfter><li>c.mod</li></loadAfter>",
        )
        _make_mod(tmp_path, "B", "b.mod")
        _make_mod(tmp_path, "C", "c.mod")
        rules = _collect_load_rules(tmp_path)
        assert rules["a.mod"]["loadAfter"] == {"b.mod", "c.mod"}

    def test_rules_require_package_id(self, tmp_path: Path):
        _make_mod(tmp_path, "A", "")
        assert _collect_load_rules(tmp_path) == {}

    def test_rules_skip_files_and_malformed_about(self, tmp_path: Path):
        (tmp_path / "stray.txt").write_text("x")
        bad = tmp_path / "bad" / "About"
        bad.mkdir(parents=True)
        (bad / "About.xml").write_text("<<<bad")
        _make_mod(tmp_path, "A", "a.mod")
        assert set(_collect_load_rules(tmp_path)) == {"a.mod"}

    def test_rules_missing_dir_no_iterdir_crash(self, tmp_path: Path):
        """_collect_load_rules mirrors scan_mod_metadata: missing dir -> {}."""
        assert _collect_load_rules(tmp_path / "nope") == {}


class TestSortModsScale:
    def test_1200_mod_chain_sorts(self, tmp_path: Path):
        n = 1200
        for i in range(n):
            after = f"<loadAfter><li>mod.{i - 1:04d}</li></loadAfter>" if i else ""
            _make_mod(tmp_path, f"mod{i:04d}", f"mod.{i:04d}", extra=after)
        start = time.monotonic()
        result = sort_mods(tmp_path)
        elapsed = time.monotonic() - start
        assert len(result["sorted"]) == n
        assert result["unresolved"] == []
        assert result["sorted"][0] == "mod.0000"
        assert result["sorted"][-1] == f"mod.{n - 1:04d}"
        assert elapsed < 20, f"topological sort degraded: {elapsed:.1f}s"


# ── generate / compare ─────────────────────────────────────────────


class TestGenerateAndCompare:
    def test_generate_empty_dir_round_trips(self, tmp_path: Path):
        mods = tmp_path / "Mods"
        mods.mkdir()
        xml = generate_modsconfig(mods)
        out = tmp_path / "ModsConfig.xml"
        out.write_text(xml, encoding="utf-8")
        data = parse_modsconfig(out)
        assert data["active_mods"] == []
        assert data["total"] == 0

    def test_generate_skips_mods_without_package_id(self, tmp_path: Path):
        mods = tmp_path / "Mods"
        mods.mkdir()
        _make_mod(mods, "ok", "a.b")
        _make_mod(mods, "bad", "")
        xml = generate_modsconfig(mods)
        assert "a.b" in xml
        assert "<li></li>" not in xml

    def test_compare_literal_core_is_exempt(self, tmp_path: Path):
        """The literal folder name "Core" is exempted — the case the code handles."""
        mods = tmp_path / "Mods"
        mods.mkdir()
        cfg = tmp_path / "ModsConfig.xml"
        cfg.write_text(_modsconfig_xml(["Core"]), encoding="utf-8")
        data = compare_modsconfig(cfg, mods)
        assert data["missing"] == []
        assert data["extra"] == []

    def test_compare_core_package_id_is_never_missing(self, tmp_path: Path):
        """RimWorld's Core mod declares packageId ludeon.rimworld (docstring:
        'must never be reported as missing')."""
        mods = tmp_path / "Mods"
        mods.mkdir()
        _make_mod(mods, "A", "a.mod")
        cfg = tmp_path / "ModsConfig.xml"
        cfg.write_text(_modsconfig_xml(["ludeon.rimworld", "a.mod"]), encoding="utf-8")
        data = compare_modsconfig(cfg, mods)
        assert data["missing"] == []

    def test_compare_malformed_xml_returns_error(self, tmp_path: Path):
        mods = tmp_path / "Mods"
        mods.mkdir()
        cfg = tmp_path / "ModsConfig.xml"
        cfg.write_text("<<<bad", encoding="utf-8")
        data = compare_modsconfig(cfg, mods)
        assert "error" in data

    def test_parse_modsconfig_root_without_activemods(self, tmp_path: Path):
        f = tmp_path / "ModsConfig.xml"
        f.write_text("<ModsConfigData><version>1.5</version></ModsConfigData>", encoding="utf-8")
        data = parse_modsconfig(f)
        assert data["active_mods"] == []
        assert data["version"] == "1.5"

    def test_resolve_missing_workshop_ids_positive_and_negative(self, tmp_path: Path):
        _make_mod(tmp_path, "A", "author.mod", workshop_id="111")
        results = resolve_missing_workshop_ids(["author.mod", "ghost.mod"], tmp_path)
        assert results[0] == {"package_id": "author.mod", "workshop_id": "111", "resolved": True}
        assert results[1] == {"package_id": "ghost.mod", "workshop_id": "", "resolved": False}

    def test_resolve_missing_workshop_ids_non_numeric_id_not_returned(self, tmp_path: Path):
        _make_mod(tmp_path, "A", "author.mod", workshop_id="../evil")
        results = resolve_missing_workshop_ids(["author.mod"], tmp_path)
        assert results[0]["resolved"] is False


# ── load_order ─────────────────────────────────────────────────────


class TestLoadOrderEdges:
    def test_empty_active_mods_raises(self, tmp_path: Path):
        cfg = tmp_path / "ModsConfig.xml"
        cfg.write_text("<ModsConfigData><activeMods></activeMods></ModsConfigData>")
        with pytest.raises(ValidationError):
            check_load_order(cfg, tmp_path / "Mods")

    def test_missing_active_mods_raises(self, tmp_path: Path):
        cfg = tmp_path / "ModsConfig.xml"
        cfg.write_text("<ModsConfigData><version>1.5</version></ModsConfigData>")
        with pytest.raises(ValidationError):
            check_load_order(cfg, tmp_path / "Mods")

    def test_malformed_xml_raises_validation(self, tmp_path: Path):
        cfg = tmp_path / "ModsConfig.xml"
        cfg.write_text("<<<bad")
        with pytest.raises(ValidationError):
            check_load_order(cfg, tmp_path / "Mods")

    def test_missing_file_raises_mod_not_found(self, tmp_path: Path):
        with pytest.raises(ModNotFoundError):
            check_load_order(tmp_path / "nope.xml", tmp_path / "Mods")

    def test_community_mod_before_official_is_flagged(self, tmp_path: Path):
        cfg = tmp_path / "ModsConfig.xml"
        cfg.write_text(
            "<ModsConfigData><activeMods><li>brrainz.harmony</li><li>some.mod</li>"
            "<li>ludeon.rimworld</li></activeMods></ModsConfigData>"
        )
        result = check_load_order(cfg, tmp_path / "Mods")
        assert any(
            "官方内容" in i["message"] or "官方 DLC" in i["message"] for i in result["issues"]
        )

    def test_core_after_dlc_is_flagged(self, tmp_path: Path):
        cfg = tmp_path / "ModsConfig.xml"
        cfg.write_text(
            "<ModsConfigData><activeMods><li>brrainz.harmony</li>"
            "<li>ludeon.rimworld.royalty</li><li>ludeon.rimworld</li></activeMods></ModsConfigData>"
        )
        result = check_load_order(cfg, tmp_path / "Mods")
        assert any("官方内容顺序建议" in i["message"] for i in result["issues"])

    def test_harmony_anchor_is_not_flagged_as_community_before_official(self, tmp_path: Path):
        """S-P0-3: Harmony is anchored before Core by convention, so it must
        not be reported as a community mod placed before official content."""
        cfg = tmp_path / "ModsConfig.xml"
        cfg.write_text(
            "<ModsConfigData><activeMods><li>brrainz.harmony</li><li>ludeon.rimworld</li>"
            "<li>ludeon.rimworld.royalty</li><li>some.mod</li></activeMods></ModsConfigData>"
        )
        result = check_load_order(cfg, tmp_path / "Mods")
        assert not any("社区 Mod" in i["message"] for i in result["issues"])
        assert result["ok"] is True

    def test_triple_duplicate_reports_two_issues(self, tmp_path: Path):
        cfg = tmp_path / "ModsConfig.xml"
        cfg.write_text(
            "<ModsConfigData><activeMods><li>brrainz.harmony</li><li>a.mod</li>"
            "<li>a.mod</li><li>a.mod</li></activeMods></ModsConfigData>"
        )
        result = check_load_order(cfg, tmp_path / "Mods")
        dupes = [i for i in result["issues"] if "重复" in i["message"]]
        assert len(dupes) == 2

    def test_missing_harmony_warns_but_stays_ok(self, tmp_path: Path):
        cfg = tmp_path / "ModsConfig.xml"
        cfg.write_text(
            "<ModsConfigData><activeMods><li>ludeon.rimworld</li>"
            "<li>some.mod</li></activeMods></ModsConfigData>"
        )
        result = check_load_order(cfg, tmp_path / "Mods")
        assert any("未检测到 Harmony" in i["message"] for i in result["issues"])
        assert result["ok"] is True

    def test_rimpy_db_exception_falls_back_to_hardcoded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from rwmod import rimpy_db as rimpy_mod

        def boom(cls, cache_dir=None):
            raise RuntimeError("db exploded")

        monkeypatch.setattr(rimpy_mod.RimPyDB, "get", classmethod(boom))
        mods = tmp_path / "Mods"
        mods.mkdir()
        _make_mod(mods, "CE", "combat_extended", workshop_id="111")
        _make_mod(mods, "Yayo", "yayo.combat", workshop_id="222")
        cfg = tmp_path / "ModsConfig.xml"
        cfg.write_text(
            "<ModsConfigData><activeMods><li>brrainz.harmony</li><li>combat_extended</li>"
            "<li>yayo.combat</li></activeMods></ModsConfigData>"
        )
        result = check_load_order(cfg, mods)
        assert any("二者择一" in i["message"] for i in result["issues"])

    def test_harmony_suffix_match(self, tmp_path: Path):
        """Any pid containing 'harmony' counts as Harmony for position check."""
        cfg = tmp_path / "ModsConfig.xml"
        cfg.write_text(
            "<ModsConfigData><activeMods><li>some.mod</li><li>author.harmony.patch</li>"
            "</activeMods></ModsConfigData>"
        )
        result = check_load_order(cfg, tmp_path / "Mods")
        assert any("Harmony" in i["message"] for i in result["issues"])

    def test_known_conflict_fallback_offline(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        from rwmod import rimpy_db as rimpy_mod

        monkeypatch.setattr(rimpy_mod.RimPyDB, "ensure_loaded", lambda self: False)
        monkeypatch.setattr(
            rimpy_mod.RimPyDB,
            "_download",
            lambda self: (_ for _ in ()).throw(AssertionError("network attempted")),
        )
        mods = tmp_path / "Mods"
        mods.mkdir()
        _make_mod(mods, "CE", "combat_extended", workshop_id="111")
        _make_mod(mods, "Yayo", "yayo.combat", workshop_id="222")
        cfg = tmp_path / "ModsConfig.xml"
        cfg.write_text(
            "<ModsConfigData><activeMods><li>brrainz.harmony</li><li>combat_extended</li>"
            "<li>yayo.combat</li></activeMods></ModsConfigData>"
        )
        result = check_load_order(cfg, mods)
        assert any("冲突" in i["message"] for i in result["issues"])

    def test_rimpy_db_conflicts_take_precedence_over_hardcoded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from rwmod import rimpy_db as rimpy_mod

        class FakeDB:
            def ensure_loaded(self) -> bool:
                return True

            def get_conflicts(self, wids: set[str]) -> list[dict]:
                assert wids == {"111", "222"}
                return [{"reason": "RimPy says no"}]

        monkeypatch.setattr(
            rimpy_mod.RimPyDB, "get", classmethod(lambda cls, cache_dir=None: FakeDB())
        )
        mods = tmp_path / "Mods"
        mods.mkdir()
        _make_mod(mods, "CE", "combat_extended", workshop_id="111")
        _make_mod(mods, "Yayo", "yayo.combat", workshop_id="222")
        cfg = tmp_path / "ModsConfig.xml"
        cfg.write_text(
            "<ModsConfigData><activeMods><li>brrainz.harmony</li><li>combat_extended</li>"
            "<li>yayo.combat</li></activeMods></ModsConfigData>"
        )
        result = check_load_order(cfg, mods)
        conflicts = [i for i in result["issues"] if "RimPy says no" in i["message"]]
        assert len(conflicts) == 1
        # hardcoded fallback is skipped when the DB answered
        assert not any("二者择一" in i["message"] for i in result["issues"])


# ── mod_conflicts ──────────────────────────────────────────────────


class TestScanModMetadata:
    def test_extracts_dependencies_and_incompatible(self, tmp_path: Path):
        _make_mod(
            tmp_path,
            "A",
            "a.mod",
            extra="<modDependencies><li>dep.one</li><li>dep.two</li></modDependencies>"
            "<incompatibleWith><li>enemy.mod</li></incompatibleWith>",
        )
        meta = scan_mod_metadata(tmp_path)
        assert meta["a.mod"]["dependencies"] == {"dep.one", "dep.two"}
        assert meta["a.mod"]["incompatible"] == {"enemy.mod"}

    def test_multiple_dependency_blocks_union(self, tmp_path: Path):
        _make_mod(
            tmp_path,
            "A",
            "a.mod",
            extra="<modDependencies><li>dep.one</li></modDependencies>"
            "<modDependencies><li>dep.two</li></modDependencies>",
        )
        assert scan_mod_metadata(tmp_path)["a.mod"]["dependencies"] == {"dep.one", "dep.two"}

    def test_rich_dependency_groups(self, tmp_path: Path):
        """S-P0-4: rich <li> entries keep their alternativePackageIds group."""
        _make_mod(
            tmp_path,
            "A",
            "a.mod",
            extra="<modDependencies><li><packageId>Ghost.Mod</packageId>"
            "<alternativePackageIds><li>ALT.Mod</li></alternativePackageIds>"
            "</li></modDependencies>",
        )
        meta = scan_mod_metadata(tmp_path)["a.mod"]
        assert meta["dependencies"] == {"ghost.mod"}
        assert meta["dependency_groups"] == [("ghost.mod", "alt.mod")]

    def test_blank_li_values_skipped(self, tmp_path: Path):
        _make_mod(
            tmp_path,
            "A",
            "a.mod",
            extra="<modDependencies><li>  </li><li>dep.one</li></modDependencies>",
        )
        assert scan_mod_metadata(tmp_path)["a.mod"]["dependencies"] == {"dep.one"}

    def test_skips_non_mods_and_bad_xml(self, tmp_path: Path):
        (tmp_path / "no_about").mkdir()
        (tmp_path / "plain_file.txt").write_text("x")
        bad = tmp_path / "bad" / "About"
        bad.mkdir(parents=True)
        (bad / "About.xml").write_text("<<<bad")
        _make_mod(tmp_path, "nopkg", "")
        assert scan_mod_metadata(tmp_path) == {}

    def test_sorted_iteration_last_duplicate_wins(self, tmp_path: Path):
        _make_mod(tmp_path, "A1", "dup.mod", name="First")
        _make_mod(tmp_path, "A2", "dup.mod", name="Second")
        meta = scan_mod_metadata(tmp_path)
        assert meta["dup.mod"]["name"] == "Second"


class TestFindModConflicts:
    def test_missing_dependency_reported(self, tmp_path: Path):
        _make_mod(
            tmp_path, "A", "a.mod", extra="<modDependencies><li>ghost.mod</li></modDependencies>"
        )
        result = find_mod_conflicts(tmp_path)
        assert result["ok"] is False
        assert result["missing_dependencies"] == [
            {"mod": "a.mod", "name": "A", "missing": ["ghost.mod"]}
        ]

    def test_installed_but_inactive_dependency_is_not_missing(self, tmp_path: Path):
        _make_mod(tmp_path, "A", "a.mod", extra="<modDependencies><li>b.mod</li></modDependencies>")
        _make_mod(tmp_path, "B", "b.mod")
        result = find_mod_conflicts(tmp_path, active_ids=["a.mod"])
        assert result["missing_dependencies"] == []

    def test_conflict_reported_once_when_declared_both_ways(self, tmp_path: Path):
        _make_mod(
            tmp_path, "A", "a.mod", extra="<incompatibleWith><li>b.mod</li></incompatibleWith>"
        )
        _make_mod(
            tmp_path, "B", "b.mod", extra="<incompatibleWith><li>a.mod</li></incompatibleWith>"
        )
        result = find_mod_conflicts(tmp_path)
        assert len(result["conflicts"]) == 1
        assert result["ok"] is False

    def test_conflict_ignored_when_other_inactive(self, tmp_path: Path):
        _make_mod(
            tmp_path, "A", "a.mod", extra="<incompatibleWith><li>b.mod</li></incompatibleWith>"
        )
        _make_mod(tmp_path, "B", "b.mod")
        result = find_mod_conflicts(tmp_path, active_ids=["a.mod"])
        assert result["conflicts"] == []
        assert result["ok"] is True

    def test_self_incompatibility_ignored(self, tmp_path: Path):
        _make_mod(
            tmp_path, "A", "a.mod", extra="<incompatibleWith><li>a.mod</li></incompatibleWith>"
        )
        result = find_mod_conflicts(tmp_path)
        assert result["conflicts"] == []
        assert result["ok"] is True

    def test_active_id_not_installed_is_skipped(self, tmp_path: Path):
        result = find_mod_conflicts(tmp_path, active_ids=["ghost.mod"])
        assert result["ok"] is True

    def test_conflict_matches_ids_case_insensitively(self, tmp_path: Path):
        _make_mod(
            tmp_path,
            "A",
            "Author.Mod",
            extra="<incompatibleWith><li>OTHER.MOD</li></incompatibleWith>",
        )
        _make_mod(tmp_path, "B", "Other.Mod")
        result = find_mod_conflicts(tmp_path, active_ids=["author.mod", "other.mod"])
        assert len(result["conflicts"]) == 1
        assert result["ok"] is False

    def test_missing_dependency_matches_installed_case_insensitively(self, tmp_path: Path):
        _make_mod(
            tmp_path, "A", "a.mod", extra="<modDependencies><li>DEP.One</li></modDependencies>"
        )
        _make_mod(tmp_path, "B", "dep.one")
        result = find_mod_conflicts(tmp_path, active_ids=["a.mod"])
        assert result["missing_dependencies"] == []

    def test_rich_format_dependency_is_parsed(self, tmp_path: Path):
        """S-P0-4: the 1.4+ <li><packageId> form was previously invisible."""
        _make_mod(
            tmp_path,
            "A",
            "a.mod",
            extra="<modDependencies><li><packageId>ghost.mod</packageId>"
            "<displayName>Ghost</displayName></li></modDependencies>",
        )
        result = find_mod_conflicts(tmp_path)
        assert result["ok"] is False
        assert result["missing_dependencies"] == [
            {"mod": "a.mod", "name": "A", "missing": ["ghost.mod"]}
        ]

    def test_alternative_package_id_satisfies_dependency(self, tmp_path: Path):
        _make_mod(
            tmp_path,
            "A",
            "a.mod",
            extra="<modDependencies><li><packageId>ghost.mod</packageId>"
            "<alternativePackageIds IgnoreIfNoMatchingField='True'>"
            "<li>b.mod</li></alternativePackageIds></li></modDependencies>",
        )
        _make_mod(tmp_path, "B", "b.mod")
        result = find_mod_conflicts(tmp_path, active_ids=["a.mod"])
        assert result["missing_dependencies"] == []

    def test_official_dlc_dependency_is_treated_as_installed(self, tmp_path: Path):
        _make_mod(
            tmp_path,
            "A",
            "a.mod",
            extra="<modDependencies><li><packageId>Ludeon.RimWorld.Ideology</packageId>"
            "</li></modDependencies>",
        )
        result = find_mod_conflicts(tmp_path)
        assert result["missing_dependencies"] == []

    def test_missing_mods_dir_returns_empty_result(self, tmp_path: Path):
        result = find_mod_conflicts(tmp_path / "nope")
        assert result == {"missing_dependencies": [], "conflicts": [], "ok": True}


# ── version_support edges ──────────────────────────────────────────


class TestVersionSupportEdges:
    def test_normalize_junk_returns_stripped(self):
        assert _normalize_version("  stable  ") == "stable"
        assert _normalize_version("") == ""

    def test_supported_version_with_whitespace_matches(self):
        metas = [read_mod_meta_stub(supported=[" 1.5 ", "v1.4"])]
        result = check_version_support(metas, "1.5.4297")
        assert len(result["compatible"]) == 1

    def test_detect_version_prefers_version_txt(self, tmp_path: Path):
        (tmp_path / "Version.txt").write_text("1.5.4297 rev1117")
        core = tmp_path / "Data" / "Core" / "About"
        core.mkdir(parents=True)
        (core / "About.xml").write_text(
            "<ModMetaData><targetVersion>1.4</targetVersion></ModMetaData>"
        )
        assert detect_rimworld_version(tmp_path) == "1.5"

    def test_detect_version_falls_back_to_core_about(self, tmp_path: Path):
        (tmp_path / "Version.txt").write_text("no digits here")
        core = tmp_path / "Data" / "Core" / "About"
        core.mkdir(parents=True)
        (core / "About.xml").write_text(
            "<ModMetaData><targetVersion>1.4</targetVersion></ModMetaData>"
        )
        assert detect_rimworld_version(tmp_path) == "1.4"

    def test_detect_version_bad_core_about_returns_none(self, tmp_path: Path):
        core = tmp_path / "Data" / "Core" / "About"
        core.mkdir(parents=True)
        (core / "About.xml").write_text("<<<bad")
        assert detect_rimworld_version(tmp_path) is None


def read_mod_meta_stub(supported):
    from rwmod.metadata import ModMeta

    return ModMeta("folder", "Name", "pkg", "", supported_versions=supported)


# ── i18n ───────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_i18n_state():
    import rwmod.i18n as i18n

    i18n._current_locale = i18n._DEFAULT_LOCALE
    i18n._translations.clear()
    i18n._loaded_locales.clear()
    yield
    i18n._current_locale = i18n._DEFAULT_LOCALE
    i18n._translations.clear()
    i18n._loaded_locales.clear()


class TestI18n:
    def test_default_locale_is_zh(self):
        from rwmod.i18n import get_locale

        assert get_locale() == "zh-CN"

    def test_key_sets_match_between_locales(self):
        from rwmod.i18n import _get_builtin

        en = set(_get_builtin("en"))
        zh = set(_get_builtin("zh-CN"))
        assert en == zh, f"en-only={en - zh}, zh-only={zh - en}"

    def test_set_locale_switches_and_format(self):
        from rwmod.i18n import get_locale, set_locale, t

        set_locale("en")
        assert get_locale() == "en"
        assert t("mods.count", count=3) == "3 mods"
        set_locale("zh-CN")
        assert t("mods.count", count=3) == "3 个 Mod"

    def test_unknown_locale_is_ignored(self):
        from rwmod.i18n import get_locale, set_locale

        set_locale("en")
        set_locale("fr")
        assert get_locale() == "en"

    def test_missing_key_falls_back_to_zh(self):
        from rwmod.i18n import _get_builtin, set_locale, t

        set_locale("en")
        import rwmod.i18n as i18n

        i18n._translations["en"].pop("ui.title")
        assert t("ui.title") == _get_builtin("zh-CN")["ui.title"]

    def test_missing_everywhere_returns_key(self):
        from rwmod.i18n import set_locale, t

        set_locale("en")
        assert t("does.not.exist") == "does.not.exist"

    def test_missing_placeholder_returns_unformatted(self):
        from rwmod.i18n import t

        assert t("mods.count") == "{count} 个 Mod"

    def test_wrong_placeholder_kwarg_returns_template(self):
        from rwmod.i18n import t

        assert t("mods.count", wrong=1) == "{count} 个 Mod"

    def test_extra_kwargs_are_tolerated(self):
        from rwmod.i18n import t

        assert t("queue.empty", n=99) == "队列为空"

    def test_available_locales(self):
        from rwmod.i18n import AVAILABLE_LOCALES

        assert set(AVAILABLE_LOCALES) == {"zh-CN", "en"}


# ── rimpy_db ───────────────────────────────────────────────────────


def _write_rimpy_cache(cache_dir: Path, data: dict) -> Path:
    """Write the *legacy* RimPy cache file (migration coverage)."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    p = cache_dir / "rimpy_database.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def _write_community_cache(cache_dir: Path, data: dict) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    p = cache_dir / "community_rules.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


class _FakeResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._payload


class TestRimPyDB:
    def test_singleton_get_and_reset(self, tmp_path: Path):
        from rwmod.rimpy_db import RimPyDB

        RimPyDB.reset()
        try:
            a = RimPyDB.get(tmp_path)
            b = RimPyDB.get()
            assert a is b
            RimPyDB.reset()
            assert RimPyDB.get(tmp_path) is not a
        finally:
            RimPyDB.reset()

    def test_loads_fresh_cache(self, tmp_path: Path):
        from rwmod.rimpy_db import RimPyDB

        _write_rimpy_cache(tmp_path, {"conflicts": [{"a": "1", "b": "2", "reason": "boom"}]})
        db = RimPyDB(cache_dir=tmp_path)
        assert db.ensure_loaded() is True
        assert db.get_conflicts({"1", "2"})[0]["reason"] == "boom"

    def test_stale_cache_is_used_when_download_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Fail-open: an expired cache still beats no rules at all."""
        from rwmod import rimpy_db as rimpy_mod

        p = _write_community_cache(
            tmp_path,
            {"rules": {"a.mod": {"loadAfter": {"b.mod": {"name": "B"}}}}},
        )
        old = time.time() - 48 * 3600
        os.utime(p, (old, old))
        monkeypatch.setattr(rimpy_mod.RimPyDB, "_download", lambda self: False)
        db = rimpy_mod.RimPyDB(cache_dir=tmp_path)
        assert db.ensure_loaded() is True
        assert db.rules["a.mod"]["loadAfter"] == {"b.mod"}

    def test_corrupt_cache_falls_back(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        from rwmod import rimpy_db as rimpy_mod

        (tmp_path / "rimpy_database.json").write_text("{not json", encoding="utf-8")
        monkeypatch.setattr(rimpy_mod.RimPyDB, "_download", lambda self: False)
        db = rimpy_mod.RimPyDB(cache_dir=tmp_path)
        assert db.ensure_loaded() is False

    def test_empty_dict_cache_not_loaded(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        from rwmod import rimpy_db as rimpy_mod

        _write_rimpy_cache(tmp_path, {})
        monkeypatch.setattr(rimpy_mod.RimPyDB, "_download", lambda self: False)
        assert rimpy_mod.RimPyDB(cache_dir=tmp_path).ensure_loaded() is False

    def test_load_attempted_only_once(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        from rwmod import rimpy_db as rimpy_mod

        calls: list[int] = []
        monkeypatch.setattr(rimpy_mod.RimPyDB, "_download", lambda self: calls.append(1) or False)
        db = rimpy_mod.RimPyDB(cache_dir=tmp_path)
        assert db.ensure_loaded() is False
        assert db.ensure_loaded() is False
        assert len(calls) == 1

    def test_download_success_writes_cache(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        from rwmod import rimpy_db as rimpy_mod

        payload = json.dumps({"mods": {"111": {"name": "X"}}}).encode()
        monkeypatch.setattr(rimpy_mod, "urlopen", lambda req, timeout=30: _FakeResponse(payload))
        db = rimpy_mod.RimPyDB(cache_dir=tmp_path / "cache")
        assert db.ensure_loaded() is True
        assert (tmp_path / "cache" / "community_rules.json").exists()
        assert not list((tmp_path / "cache").glob("*.tmp"))
        assert db.search_by_workshop_id("111") == {"name": "X"}

    def test_download_non_dict_payload_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from rwmod import rimpy_db as rimpy_mod

        monkeypatch.setattr(rimpy_mod, "urlopen", lambda req, timeout=30: _FakeResponse(b"[]"))
        db = rimpy_mod.RimPyDB(cache_dir=tmp_path)
        assert db.ensure_loaded() is False

    def test_download_exception_is_swallowed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        from rwmod import rimpy_db as rimpy_mod

        def boom(req, timeout=30):
            raise OSError("offline")

        monkeypatch.setattr(rimpy_mod, "urlopen", boom)
        assert rimpy_mod.RimPyDB(cache_dir=tmp_path).ensure_loaded() is False

    def test_get_conflicts_without_load_returns_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from rwmod import rimpy_db as rimpy_mod

        monkeypatch.setattr(rimpy_mod.RimPyDB, "ensure_loaded", lambda self: False)
        assert rimpy_mod.RimPyDB(cache_dir=tmp_path).get_conflicts({"1"}) == []
        assert rimpy_mod.RimPyDB(cache_dir=tmp_path).get_load_rules({"1"}) == []
        assert rimpy_mod.RimPyDB(cache_dir=tmp_path).search_by_workshop_id("1") is None

    def test_conflict_key_variants(self, tmp_path: Path):
        from rwmod.rimpy_db import RimPyDB

        _write_rimpy_cache(
            tmp_path,
            {
                "conflicts": [
                    {"mod_a": "1", "mod_b": "2"},
                    {"id1": "3", "id2": "4", "description": "desc"},
                    "not-a-dict",
                    {"a": "5", "b": "6", "reason": "explicit"},
                ]
            },
        )
        db = RimPyDB(cache_dir=tmp_path)
        found = {
            (c["mod_a"], c["mod_b"]): c["reason"]
            for c in db.get_conflicts({"1", "2", "3", "4", "5", "6"})
        }
        assert found[("1", "2")] == "Known conflict"
        assert found[("3", "4")] == "desc"
        assert found[("5", "6")] == "explicit"
        assert db.get_conflicts({"1"}) == []

    def test_data_not_dict_is_ignored(self, tmp_path: Path):
        from rwmod.rimpy_db import RimPyDB

        db = RimPyDB(cache_dir=tmp_path)
        db._data = []  # type: ignore[assignment]
        db._loaded = True
        assert db.get_conflicts({"1"}) == []
        assert db.get_load_rules({"1"}) == []
        assert db.search_by_workshop_id("1") is None

    def test_load_rules_filter_and_shape(self, tmp_path: Path):
        from rwmod.rimpy_db import RimPyDB

        _write_rimpy_cache(
            tmp_path,
            {
                "load_rules": [
                    {"id": "1", "before": ["2"], "after": [], "note": "n"},
                    {"mod_id": "9", "before": [], "after": ["1"]},
                    {"id": "far", "before": [], "after": []},
                    "junk",
                ]
            },
        )
        db = RimPyDB(cache_dir=tmp_path)
        rules = db.get_load_rules({"1", "2"})
        assert {r["mod_id"] for r in rules} == {"1", "9"}
        rule = next(r for r in rules if r["mod_id"] == "9")
        assert rule["after"] == ["1"]

    def test_search_by_workshop_id_missing(self, tmp_path: Path):
        from rwmod.rimpy_db import RimPyDB

        _write_rimpy_cache(tmp_path, {"mods": {"111": {"name": "X"}}})
        db = RimPyDB(cache_dir=tmp_path)
        assert db.search_by_workshop_id("111") == {"name": "X"}
        assert db.search_by_workshop_id("999") is None

    def test_search_mods_not_a_dict_returns_none(self, tmp_path: Path):
        from rwmod.rimpy_db import RimPyDB

        _write_rimpy_cache(tmp_path, {"mods": ["not", "a", "dict"]})
        db = RimPyDB(cache_dir=tmp_path)
        assert db.search_by_workshop_id("111") is None


# ── Community Rules database (Wave 3) ──────────────────────────────


class _Resp304:
    status = 304
    headers = {"ETag": '"abc"'}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return b""


class TestCommunityRulesDatabase:
    def test_parses_community_rules_schema(self, tmp_path: Path):
        from rwmod.rimpy_db import RimPyDB

        _write_community_cache(
            tmp_path,
            {
                "timestamp": 123,
                "rules": {
                    "a.mod": {
                        "loadAfter": {"b.mod": {"name": ["B"]}},
                        "loadBefore": {"c.mod": {}},
                        "loadTop": {"value": True, "comment": "first"},
                        "loadBottom": {"value": False},
                        "incompatibleWith": {"d.mod": {"comment": "no"}},
                    }
                },
            },
        )
        db = RimPyDB(cache_dir=tmp_path)
        assert db.ensure_loaded() is True
        rule = db.rules["a.mod"]
        assert rule["loadAfter"] == {"b.mod"}
        assert rule["loadBefore"] == {"c.mod"}
        assert rule["loadTop"] is True
        assert rule["loadBottom"] is False
        assert rule["incompatibleWith"] == {"d.mod"}

    def test_legacy_rimpy_schema_is_migrated(self, tmp_path: Path):
        """The dead RimPy DB format ({conflicts: [...]}) must still work."""
        from rwmod.rimpy_db import RimPyDB

        _write_rimpy_cache(
            tmp_path,
            {
                "conflicts": [{"a": "111", "b": "222", "reason": "boom"}],
                "load_rules": [{"id": "a.mod", "before": [], "after": ["b.mod"]}],
            },
        )
        db = RimPyDB(cache_dir=tmp_path)
        assert db.ensure_loaded() is True
        assert db.get_conflicts({"111", "222"})[0]["reason"] == "boom"
        assert db.rules["a.mod"]["loadAfter"] == {"b.mod"}

    def test_new_cache_file_is_preferred_over_legacy(self, tmp_path: Path):
        from rwmod.rimpy_db import RimPyDB

        _write_community_cache(tmp_path, {"rules": {"new.mod": {"loadTop": {"value": True}}}})
        _write_rimpy_cache(tmp_path, {"conflicts": [{"a": "1", "b": "2"}]})
        db = RimPyDB(cache_dir=tmp_path)
        assert db.ensure_loaded() is True
        assert "new.mod" in db.rules
        assert "1" not in db.rules

    def test_load_cached_never_downloads(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        from rwmod import rimpy_db as rimpy_mod

        def boom(*_args, **_kwargs):
            raise AssertionError("network attempted")

        monkeypatch.setattr(rimpy_mod, "urlopen", boom)
        _write_community_cache(tmp_path, {"rules": {"a.mod": {"loadTop": {"value": True}}}})
        db = rimpy_mod.RimPyDB(cache_dir=tmp_path)
        assert db.load_cached() is True
        assert db.rules["a.mod"]["loadTop"] is True

    def test_load_cached_missing_cache_is_false_without_network(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from rwmod import rimpy_db as rimpy_mod

        def boom(*_args, **_kwargs):
            raise AssertionError("network attempted")

        monkeypatch.setattr(rimpy_mod, "urlopen", boom)
        db = rimpy_mod.RimPyDB(cache_dir=tmp_path)
        assert db.load_cached() is False
        assert db.rules == {}

    def test_allow_network_false_never_downloads(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from rwmod import rimpy_db as rimpy_mod

        def boom(*_args, **_kwargs):
            raise AssertionError("network attempted")

        monkeypatch.setattr(rimpy_mod, "urlopen", boom)
        db = rimpy_mod.RimPyDB(cache_dir=tmp_path)
        assert db.ensure_loaded(allow_network=False) is False

    def test_cache_size_limit_rejects_oversized_file(self, tmp_path: Path):
        from rwmod.rimpy_db import RimPyDB

        p = _write_community_cache(tmp_path, {"rules": {"a.mod": {"loadTop": {"value": True}}}})
        p.write_bytes(b" " * (2 * 1024 * 1024 + 1))
        db = RimPyDB(cache_dir=tmp_path)
        assert db.load_cached() is False

    def test_download_size_limit_rejects_oversized_payload(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from rwmod import rimpy_db as rimpy_mod

        monkeypatch.setattr(rimpy_mod, "MAX_CACHE_BYTES", 32)
        payload = json.dumps({"rules": {"a.mod": {"loadTop": {"value": True}}}}).encode()
        monkeypatch.setattr(rimpy_mod, "urlopen", lambda req, timeout=30: _FakeResponse(payload))
        db = rimpy_mod.RimPyDB(cache_dir=tmp_path)
        assert db.ensure_loaded() is False
        assert not (tmp_path / "community_rules.json").exists()

    def test_conditional_get_sends_etag_and_304_reuses_cache(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from rwmod import rimpy_db as rimpy_mod

        p = _write_community_cache(
            tmp_path,
            {"rules": {"a.mod": {"loadAfter": {"b.mod": {}}}}},
        )
        old = time.time() - 48 * 3600
        os.utime(p, (old, old))
        (tmp_path / "community_rules.etag").write_text('"abc"', encoding="utf-8")
        seen: dict[str, str | None] = {}

        def fake(req, timeout=30):
            seen["if_none_match"] = req.get_header("If-none-match")
            return _Resp304()

        monkeypatch.setattr(rimpy_mod, "urlopen", fake)
        db = rimpy_mod.RimPyDB(cache_dir=tmp_path)

        assert db.ensure_loaded() is True
        assert seen["if_none_match"] == '"abc"'
        assert db.rules["a.mod"]["loadAfter"] == {"b.mod"}
        assert time.time() - p.stat().st_mtime < 60  # 304 refreshed the TTL

    def test_http_304_error_reuses_cache_and_refreshes_ttl(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """urllib raises HTTPError for 304, not a response object."""
        from urllib.error import HTTPError

        from rwmod import rimpy_db as rimpy_mod

        p = _write_community_cache(tmp_path, {"rules": {"a.mod": {"loadAfter": {"b.mod": {}}}}})
        old = time.time() - 48 * 3600
        os.utime(p, (old, old))
        (tmp_path / "community_rules.etag").write_text('"abc"', encoding="utf-8")

        def fake(req, timeout=30):
            raise HTTPError(req.full_url, 304, "Not Modified", {}, None)

        monkeypatch.setattr(rimpy_mod, "urlopen", fake)
        db = rimpy_mod.RimPyDB(cache_dir=tmp_path)

        assert db.ensure_loaded() is True
        assert db.rules["a.mod"]["loadAfter"] == {"b.mod"}
        assert time.time() - p.stat().st_mtime < 60

    def test_download_stores_etag_sidecar(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        from rwmod import rimpy_db as rimpy_mod

        class _Resp:
            status = 200
            headers = {"ETag": '"new"'}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps({"rules": {"a.mod": {"loadTop": {"value": True}}}}).encode()

        monkeypatch.setattr(rimpy_mod, "urlopen", lambda req, timeout=30: _Resp())
        db = rimpy_mod.RimPyDB(cache_dir=tmp_path)
        assert db.ensure_loaded() is True
        assert (tmp_path / "community_rules.etag").read_text(encoding="utf-8") == '"new"'
        assert not list(tmp_path.glob("*.tmp"))

    def test_get_incompatible_uses_package_ids(self, tmp_path: Path):
        from rwmod.rimpy_db import RimPyDB

        _write_community_cache(
            tmp_path,
            {
                "rules": {
                    "a.mod": {"incompatibleWith": {"b.mod": {}}},
                    "b.mod": {"loadAfter": {"c.mod": {}}},
                }
            },
        )
        db = RimPyDB(cache_dir=tmp_path)
        assert db.ensure_loaded() is True
        found = db.get_incompatible({"a.mod", "b.mod", "c.mod"})
        assert len(found) == 1
        assert {found[0]["a"], found[0]["b"]} == {"a.mod", "b.mod"}

    def test_parse_rule_map_from_legacy_load_rules(self, tmp_path: Path):
        from rwmod.rimpy_db import RimPyDB

        _write_rimpy_cache(
            tmp_path,
            {"load_rules": [{"id": "A.Mod", "before": ["C.Mod"], "after": ["B.Mod"]}]},
        )
        db = RimPyDB(cache_dir=tmp_path)
        assert db.ensure_loaded() is True
        rule = db.rules["a.mod"]
        assert rule["loadAfter"] == {"b.mod"}
        assert rule["loadBefore"] == {"c.mod"}

    @pytest.mark.network
    def test_real_community_rules_download(self, tmp_path: Path):
        from rwmod.rimpy_db import RimPyDB

        db = RimPyDB(cache_dir=tmp_path)
        assert db.ensure_loaded() is True
        assert db.rules, "community rules database parsed to an empty rule map"
        assert any(rule["loadAfter"] for rule in db.rules.values())


# ── CLI ────────────────────────────────────────────────────────────


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Isolated Config + home + profiles + DB for CLI invocations."""
    import rwmod.config as cfg_mod
    import rwmod.database as db_mod
    import rwmod.profile as prof_mod

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(cfg_mod.Config, "CONFIG_PATH", tmp_path / ".rwmod_probe.toml")
    monkeypatch.setattr(prof_mod, "PROFILES_DIR", tmp_path / ".rwmod" / "profiles")
    monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / ".rwmod_probe.db")
    db_mod.close_db()
    mods = tmp_path / "Mods"
    mods.mkdir()
    rimworld = tmp_path / "RimWorld"
    rimworld.mkdir()
    steamcmd = tmp_path / "steamcmd"
    steamcmd.mkdir()
    (steamcmd / "steamcmd.exe").touch()
    cfg_mod.Config(
        steamcmd_path=steamcmd / "steamcmd.exe", mods_dir=mods, rimworld_dir=rimworld
    ).save()
    try:
        yield tmp_path
    finally:
        db_mod.close_db()


ALL_CLI_COMMANDS = [
    "setup",
    "web",
    "config",
    "download",
    "import",
    "import-collection",
    "import-sort",
    "list",
    "profile-save",
    "profile-list",
    "profile-restore",
    "profile-delete",
    "backup-list",
    "backup-restore",
    "backup-cleanup",
    "compat",
    "check-order",
]


class TestCliSurface:
    @pytest.mark.parametrize("command", ALL_CLI_COMMANDS)
    def test_help_exits_zero(self, command: str):
        from rwmod.cli import app

        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0, _output(result)

    def test_no_args_shows_help_nonzero(self):
        from rwmod.cli import app

        result = runner.invoke(app, [])
        assert result.exit_code != 0
        assert "Usage" in _output(result)

    def test_missing_required_arg_is_usage_error(self):
        from rwmod.cli import app

        result = runner.invoke(app, ["download"])
        assert result.exit_code == 2

    def test_config_prints_paths(self, cli_env: Path):
        from rwmod.cli import app

        result = runner.invoke(app, ["config"])
        assert result.exit_code == 0
        out = _output(result)
        assert "Mods 目录" in out
        assert str(cli_env / "Mods") in out

    def test_setup_accepts_defaults(self, cli_env: Path, monkeypatch: pytest.MonkeyPatch):
        import rwmod.config as cfg_mod
        from rwmod.cli import app

        result = runner.invoke(app, ["setup"], input="\n\n\n")
        assert result.exit_code == 0, _output(result)
        assert "配置已保存" in _output(result)
        cfg = cfg_mod.Config.load()
        assert cfg.mods_dir == cli_env / "Mods"


class TestCliList:
    def test_missing_mods_dir_message(self, cli_env: Path):
        import shutil

        from rwmod.cli import app

        shutil.rmtree(cli_env / "Mods")
        result = runner.invoke(app, ["list"])
        assert result.exit_code == 0
        assert "Mods 目录不存在" in _output(result)

    def test_stray_file_in_mods_dir_is_ignored(self, cli_env: Path):
        from rwmod.cli import app

        (cli_env / "Mods" / "readme.txt").write_text("not a mod")
        result = runner.invoke(app, ["list"])
        assert result.exit_code == 0
        assert "已安装 Mod (0 个)" in _output(result)

    def test_non_mod_directory_shows_placeholder(self, cli_env: Path):
        from rwmod.cli import app

        (cli_env / "Mods" / "just_a_folder").mkdir()
        result = runner.invoke(app, ["list"])
        assert result.exit_code == 0
        out = _output(result)
        assert "just_a_folder" in out
        assert "(1 个)" in out

    def test_bom_about_xml_is_read(self, cli_env: Path):
        about = cli_env / "Mods" / "BomMod" / "About"
        about.mkdir(parents=True)
        (about / "About.xml").write_bytes(
            "﻿<ModMetaData><name>Bom Mod</name><packageId>bom.mod</packageId></ModMetaData>".encode()
        )
        from rwmod.cli import app

        result = runner.invoke(app, ["list"])
        assert result.exit_code == 0
        assert "bom.mod" in _output(result)


class TestCliDownloadAndImport:
    def test_download_success_path(self, cli_env: Path, monkeypatch: pytest.MonkeyPatch):
        calls: list[str] = []
        _patch_download_one(monkeypatch, calls)
        from rwmod.cli import app

        result = runner.invoke(
            app, ["download", "https://steamcommunity.com/sharedfiles/filedetails/?id=2009463077"]
        )
        assert result.exit_code == 0, _output(result)
        assert calls == ["2009463077"]
        assert "1/1 成功" in _output(result)

    def test_download_quiet_flag(self, cli_env: Path, monkeypatch: pytest.MonkeyPatch):
        calls: list[str] = []
        _patch_download_one(monkeypatch, calls, ok=False)
        from rwmod.cli import app

        result = runner.invoke(app, ["download", "--quiet", "2009463077"])
        assert result.exit_code == 0
        out = _output(result)
        assert "0/1 成功" in out

    def test_download_quiet_success_marks_check(
        self, cli_env: Path, monkeypatch: pytest.MonkeyPatch
    ):
        calls: list[str] = []
        _patch_download_one(monkeypatch, calls, ok=True)
        from rwmod.cli import app

        result = runner.invoke(app, ["download", "--quiet", "2009463077", "1234567890"])
        assert result.exit_code == 0, _output(result)
        assert "✓" in _output(result)
        assert calls == ["2009463077", "1234567890"]

    def test_import_missing_file(self, cli_env: Path):
        from rwmod.cli import app

        result = runner.invoke(app, ["import", str(cli_env / "nope.txt")])
        assert result.exit_code == 1
        assert "文件不存在" in _output(result)

    def test_import_empty_file(self, cli_env: Path):
        from rwmod.cli import app

        f = cli_env / "empty.txt"
        f.write_text("# only comments\n\n")
        result = runner.invoke(app, ["import", str(f)])
        assert result.exit_code == 1
        assert "未找到有效 Mod ID" in _output(result)

    def test_import_valid_file_downloads(self, cli_env: Path, monkeypatch: pytest.MonkeyPatch):
        calls: list[str] = []
        _patch_download_one(monkeypatch, calls)
        f = cli_env / "mods list.txt"
        f.write_text("2009463077\n# comment\n1234567890\n")
        from rwmod.cli import app

        result = runner.invoke(app, ["import", str(f)])
        assert result.exit_code == 0, _output(result)
        assert calls == ["2009463077", "1234567890"]

    def test_import_collection_failure_exits_nonzero(
        self, cli_env: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import rwmod.cli as cli_mod

        class FakeSteamCMD:
            def __init__(self, path):
                self.workshop_content_dir = cli_env / "workshop"

            def workshop_download(self, collection_id):
                class R:
                    success = False
                    output_lines = ["ERROR! something"]

                return R()

        monkeypatch.setattr(cli_mod, "SteamCMD", FakeSteamCMD)
        result = runner.invoke(cli_mod.app, ["import-collection", "123"])
        assert result.exit_code == 1
        assert "获取合集元数据失败" in _output(result)

    def test_import_collection_success_downloads(
        self, cli_env: Path, monkeypatch: pytest.MonkeyPatch
    ):
        calls: list[str] = []
        _patch_download_one(monkeypatch, calls)
        import rwmod.cli as cli_mod

        content_dir = cli_env / "workshop"

        class FakeSteamCMD:
            def __init__(self, path):
                self.workshop_content_dir = content_dir

            def workshop_download(self, collection_id):
                (content_dir / collection_id).mkdir(parents=True)
                (content_dir / collection_id / "collection.json").write_text(
                    json.dumps({"children": [{"publishedfileid": "2009463077"}]})
                )

                class R:
                    success = True
                    output_lines = ["Downloaded item 2009463077"]

                return R()

        monkeypatch.setattr(cli_mod, "SteamCMD", FakeSteamCMD)
        result = runner.invoke(cli_mod.app, ["import-collection", "123"])
        assert result.exit_code == 0, _output(result)
        out = _output(result)
        assert "合集包含 1 个 Mod" in out
        assert "1/1 成功" in out
        assert calls == ["2009463077"]

    def test_import_collection_unparseable_exits_nonzero(
        self, cli_env: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import rwmod.cli as cli_mod

        class FakeSteamCMD:
            def __init__(self, path):
                self.workshop_content_dir = cli_env / "workshop"

            def workshop_download(self, collection_id):
                class R:
                    success = True
                    output_lines = []

                return R()

        monkeypatch.setattr(cli_mod, "SteamCMD", FakeSteamCMD)
        result = runner.invoke(cli_mod.app, ["import-collection", "123"])
        assert result.exit_code == 1
        assert "未能解析合集内容" in _output(result)


class TestCliImportSort:
    def _write_config(self, cli_env: Path, active: list[str]) -> Path:
        p = cli_env / "RimWorld" / "ModsConfig.xml"
        p.write_text(_modsconfig_xml(active), encoding="utf-8")
        return p

    def test_missing_file(self, cli_env: Path):
        from rwmod.cli import app

        result = runner.invoke(app, ["import-sort", str(cli_env / "nope.xml")])
        assert result.exit_code == 1
        assert "文件不存在" in _output(result)

    def test_empty_config_exits_nonzero(self, cli_env: Path):
        from rwmod.cli import app

        p = cli_env / "empty.xml"
        p.write_text("<ModsConfigData><activeMods></activeMods></ModsConfigData>")
        result = runner.invoke(app, ["import-sort", str(p)])
        assert result.exit_code == 1
        assert "未找到 mod 列表" in _output(result)

    def test_all_installed(self, cli_env: Path):
        _make_mod(cli_env / "Mods", "A", "a.mod")
        p = self._write_config(cli_env, ["a.mod"])
        from rwmod.cli import app

        result = runner.invoke(app, ["import-sort", str(p)])
        assert result.exit_code == 0
        assert "所有 mod 已安装" in _output(result)

    def test_missing_ids_reported_and_skipped(self, cli_env: Path, monkeypatch: pytest.MonkeyPatch):
        calls: list[str] = []
        _patch_download_one(monkeypatch, calls)
        _make_mod(cli_env / "Mods", "A", "a.mod")
        p = self._write_config(cli_env, ["a.mod", "ghost.mod"])
        from rwmod.cli import app

        result = runner.invoke(app, ["import-sort", str(p)])
        assert result.exit_code == 0, _output(result)
        out = _output(result)
        assert "缺失 1 个 Mod" in out
        assert "无法解析为 Workshop ID" in out
        assert "没有可下载的 Mod" in out
        assert calls == []

    def test_known_expansions_are_not_counted_as_missing(
        self, cli_env: Path, monkeypatch: pytest.MonkeyPatch
    ):
        calls: list[str] = []
        _patch_download_one(monkeypatch, calls)
        _make_mod(cli_env / "Mods", "A", "a.mod")
        p = cli_env / "RimWorld" / "ModsConfig.xml"
        p.write_text(
            _modsconfig_xml(["a.mod"], expansions=["ludeon.rimworld.ideology"]),
            encoding="utf-8",
        )
        from rwmod.cli import app

        result = runner.invoke(app, ["import-sort", str(p)])
        assert result.exit_code == 0, _output(result)
        assert "所有 mod 已安装" in _output(result)


class TestCliOrdersAndCompat:
    def test_check_order_reports_duplicates(self, cli_env: Path):
        p = cli_env / "RimWorld" / "ModsConfig.xml"
        p.write_text(_modsconfig_xml(["brrainz.harmony", "a.mod", "a.mod"]), encoding="utf-8")
        from rwmod.cli import app

        result = runner.invoke(app, ["check-order"])
        assert result.exit_code == 0
        assert "重复条目" in _output(result)

    def test_check_order_malformed_config_reports_error(self, cli_env: Path):
        p = cli_env / "RimWorld" / "ModsConfig.xml"
        p.write_text("<<<bad", encoding="utf-8")
        from rwmod.cli import app

        result = runner.invoke(app, ["check-order"])
        assert result.exit_code == 0
        assert "解析 ModsConfig.xml 失败" in _output(result)

    def test_compat_with_version_and_mods(self, cli_env: Path):
        import rwmod.config as cfg_mod

        (cli_env / "RimWorld" / "Version.txt").write_text("1.5.4297 rev1117")
        _make_mod(
            cli_env / "Mods",
            "New",
            "new.mod",
            extra="<supportedVersions><li>1.5</li></supportedVersions>",
        )
        _make_mod(
            cli_env / "Mods",
            "Old",
            "old.mod",
            extra="<supportedVersions><li>1.4</li></supportedVersions>",
        )
        cfg_mod.Config.load()  # ensure cache reflects the fixture config
        from rwmod.cli import app

        result = runner.invoke(app, ["compat"])
        assert result.exit_code == 0, _output(result)
        out = _output(result)
        assert "兼容:  1" in out
        assert "不兼容: 1" in out


class TestCliBackupsAndProfiles:
    def test_backup_list_empty(self, cli_env: Path):
        from rwmod.cli import app

        result = runner.invoke(app, ["backup-list"])
        assert result.exit_code == 0
        assert "暂无备份" in _output(result)

    def test_backup_restore_missing(self, cli_env: Path):
        from rwmod.cli import app

        result = runner.invoke(app, ["backup-restore", "2009463077"])
        assert result.exit_code == 0
        assert "未找到" in _output(result)

    def test_backup_cleanup_keep_option(self, cli_env: Path):
        from rwmod.cli import app

        result = runner.invoke(app, ["backup-cleanup", "--keep", "3"])
        assert result.exit_code == 0
        assert "保留 3 个" in _output(result)

    def test_backup_cleanup_invalid_keep(self, cli_env: Path):
        from rwmod.cli import app

        result = runner.invoke(app, ["backup-cleanup", "--keep", "abc"])
        assert result.exit_code == 2

    def test_profile_save_list_restore_delete(self, cli_env: Path):
        import rwmod.profile as prof_mod
        from rwmod.cli import app

        p = cli_env / "RimWorld" / "ModsConfig.xml"
        p.write_text(_modsconfig_xml(["brrainz.harmony", "a.mod"]), encoding="utf-8")

        saved = runner.invoke(app, ["profile-save", "my profile"])
        assert saved.exit_code == 0, _output(saved)
        assert (cli_env / ".rwmod" / "profiles" / "default" / "my profile.xml").exists()

        listed = runner.invoke(app, ["profile-list"])
        assert listed.exit_code == 0
        assert "my profile" in _output(listed)

        p.unlink()
        restored = runner.invoke(app, ["profile-restore", "my profile"])
        assert restored.exit_code == 0, _output(restored)
        assert p.exists()
        assert "a.mod" in p.read_text(encoding="utf-8")

        deleted = runner.invoke(app, ["profile-delete", "my profile"])
        assert deleted.exit_code == 0
        assert "已删除" in _output(deleted)
        assert not (cli_env / ".rwmod" / "profiles" / "default" / "my profile.xml").exists()
        assert str(prof_mod.PROFILES_DIR).startswith(str(cli_env))

    def test_profile_list_empty(self, cli_env: Path):
        from rwmod.cli import app

        result = runner.invoke(app, ["profile-list"])
        assert result.exit_code == 0
        assert "暂无存档 profile" in _output(result)

    def test_profile_save_generates_from_mods_when_no_config(self, cli_env: Path):
        _make_mod(cli_env / "Mods", "A", "a.mod")
        from rwmod.cli import app

        result = runner.invoke(app, ["profile-save", "generated"])
        assert result.exit_code == 0, _output(result)
        content = (cli_env / ".rwmod" / "profiles" / "default" / "generated.xml").read_text(
            encoding="utf-8"
        )
        assert "a.mod" in content

    def test_profile_restore_unknown_name(self, cli_env: Path):
        from rwmod.cli import app

        result = runner.invoke(app, ["profile-restore", "nope"])
        assert result.exit_code == 0
        assert "Profile 不存在" in _output(result)

    def test_profile_delete_unknown_name(self, cli_env: Path):
        from rwmod.cli import app

        result = runner.invoke(app, ["profile-delete", "nope"])
        assert result.exit_code == 0
        assert "Profile 不存在" in _output(result)
