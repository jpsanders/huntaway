"""Settings: the schema, validation, the commented TOML writer and rules."""

import stat

import pytest

from huntaway import config


def test_missing_file_gives_defaults(tmp_path):
    cfg = config.load(tmp_path / "none.toml")
    assert cfg.values == config.defaults()
    assert cfg["general.interval"] == 5


def test_render_and_load_round_trip(tmp_path, cfg):
    cfg = config.set_value(cfg, "memory.sustain", "9")
    cfg = config.set_value(cfg, "protect.names", ["transcriber", 'we"ird\\name'])
    cfg = config.set_value(cfg, "general.webhook", "https://ntfy.sh/topic\nline")
    path = config.save(cfg, tmp_path / "c.toml")
    back = config.load(path)
    assert back.values == cfg.values
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_defaults_are_written_commented_out(cfg):
    text = config.render(config.set_value(cfg, "cpu.action", "notify"))
    assert 'action = "notify"' in text
    assert "# interval = 5" in text
    assert "# One of: throttle, terminate, kill, notify." in text


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("memory.sustan", "4", "unknown option"),
        ("general.interval", "0", "at least 1"),
        ("general.interval", "2.5", "whole number"),
        ("general.mode", "chaos", "one of"),
        ("general.notify", "maybe", "true or false"),
        ("protect.patterns", ["("], "not a valid regular expression"),
        ("memory.min_available_percent", "nan", "finite"),
    ],
)
def test_bad_values_are_refused(cfg, key, value, message):
    with pytest.raises(config.ConfigError, match=message):
        config.set_value(cfg, key, value)


def test_unknown_key_suggests_the_right_one(cfg):
    with pytest.raises(config.ConfigError, match="did you mean memory.sustain"):
        config.set_value(cfg, "general.sustain", 3)


def test_strings_are_converted(cfg):
    assert config.set_value(cfg, "general.notify", "off")["general.notify"] is False
    assert config.set_value(cfg, "protect.names", "a, b,,c")["protect.names"] == ["a", "b", "c"]
    assert config.set_value(cfg, "cpu.process_percent", "250")["cpu.process_percent"] == 250.0


def test_rules_parse_and_round_trip(tmp_path, cfg):
    rules = config.parse_rules([{"name": "dev", "match": "node .*vite", "memory_mb": 4096, "action": "terminate"}])
    cfg.rules = rules
    path = config.save(cfg, tmp_path / "r.toml")
    back = config.load(path)
    assert back.rules[0].match.pattern == "node .*vite"
    assert back.rules[0].memory_mb == 4096


@pytest.mark.parametrize(
    ("rule", "message"),
    [
        ({"match": "x", "colour": "red"}, "unknown keys"),
        ({"name": "no match"}, "needs match"),
        ({"match": "x", "action": "explode"}, "action must be"),
        ({"match": "("}, "not a valid regular expression"),
        ({"match": "x", "sustain": -1}, "cannot be negative"),
    ],
)
def test_bad_rules_are_refused(rule, message):
    with pytest.raises(config.ConfigError, match=message):
        config.parse_rules([rule])


def test_unknown_sections_and_keys_fail_loading(tmp_path):
    path = tmp_path / "bad.toml"
    path.write_text("[nope]\nx = 1\n")
    with pytest.raises(config.ConfigError, match="unknown section"):
        config.load(path)
    path.write_text("[general]\nnope = 1\n")
    with pytest.raises(config.ConfigError, match="unknown option"):
        config.load(path)
    path.write_text("[general\n")
    with pytest.raises(config.ConfigError):
        config.load(path)


def test_schema_lists_every_option():
    rows = config.schema()
    assert len(rows) == len(config.SCHEMA)
    assert {r["section"] for r in rows} <= set(config.SECTIONS)
    assert all(r["help"] for r in rows)


def test_changed_and_public_views(cfg):
    cfg = config.set_value(cfg, "general.mode", "observe")
    assert cfg.changed() == {
        "general.mode": "observe",
        "general.notify": False,
        "agents.enabled": False,
        "sessions.archive_dir": cfg["sessions.archive_dir"],
    }
    assert cfg.to_public()["settings"]["general"]["mode"] == "observe"
