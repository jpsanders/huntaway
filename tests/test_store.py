"""The database: samples and rollups, apps, events, sessions search, findings and baselines."""

from huntaway.store import _fts_query


def test_samples_roll_up_and_series_pick_the_right_table(store):
    base = 1_800_000_000 // 3600 * 3600
    for i in range(0, 2 * 3600, 10):
        store.add_sample({"ts": base + i, "cpu": 10 + (i % 60), "mem": 50.0, "agents": 2})
    store.rollup(now=base + 2 * 3600 + 30)
    minutes = store.query("select count(*) as n from samples_1m")[0]["n"]
    hours = store.query("select count(*) as n from samples_1h")[0]["n"]
    assert minutes == 120 and hours == 2
    store.rollup(now=base + 2 * 3600 + 30)
    assert store.query("select count(*) as n from samples_1m")[0]["n"] == 120
    raw = store.series(3600, now=base + 2 * 3600)
    assert raw["table"] == "samples" and raw["ts"] and raw["cpu"][0] is not None
    week = store.series(7 * 86400, now=base + 2 * 3600)
    assert week["table"] == "samples_1m"


def test_apps_accumulate_and_rank(store):
    store.add_apps(600, {"chromium": (30.0, 1e9), "node": (90.0, 5e8)})
    store.add_apps(600, {"node": (30.0, 6e8)})
    top = store.top_apps(3600, now=700)
    assert top[0]["app"] == "node" and abs(top[0]["cpu_hours"] - 120 / 3600) < 1e-3 and top[0]["rss_peak"] == 6e8


def test_events_filter_and_decode(store):
    store.add_event("close", "closed one", detail={"rss": 5})
    store.add_event("throttle", "throttled one")
    rows = store.events(kind="close,kill")
    assert len(rows) == 1 and rows[0]["detail"] == {"rss": 5}
    assert len(store.events(limit=1)) == 1


def test_session_search_survives_hostile_queries(store):
    store.add_session(
        {
            "id": "claude:x",
            "harness": "claude",
            "title": "Stripe webhook",
            "archived": 1.0,
            "size": 1,
            "path": "/x",
            "cwd": "/w",
        },
        "We fixed the stripe webhook signature check",
    )
    assert store.sessions("webhook")[0]["id"] == "claude:x"
    assert store.sessions('sign" OR 1=1 --') == [] or store.sessions('sign" OR 1=1 --')[0]["id"] == "claude:x"
    assert store.sessions("signa")[0]["snippet"]
    assert store.sessions(harness="codex") == []
    store.delete_session("claude:x")
    assert store.sessions() == []
    assert _fts_query('a "b') == '"a" "b"*'


def test_findings_track_new_and_gone(store):
    new = store.upsert_findings("quick:x", [{"id": "a", "severity": "high", "title": "A"}])
    assert new == ["a"]
    assert store.upsert_findings("quick:x", [{"id": "a", "severity": "high", "title": "A"}]) == []
    store.upsert_findings("quick:x", [])
    assert store.findings() == []
    assert store.upsert_findings("quick:x", [{"id": "a", "severity": "high", "title": "A"}]) == ["a"]
    store.upsert_findings("quick:y", [{"id": "b", "severity": "info", "title": "B"}])
    assert [f["id"] for f in store.findings()] == ["a", "b"]


def test_baselines_meta_and_scans(store):
    store.set_baseline("x", {"k": "v"})
    assert store.baseline("x") == {"k": "v"}
    store.set_meta("deep_ts", "5")
    assert store.get_meta("deep_ts") == "5" and store.get_meta("nope", "d") == "d"
    store.add_scan("quick:x", 0.1, "ok", {"findings": 0})
    assert store.last_scans()[0]["summary"] == {"findings": 0}


def test_prune_and_heatmap(store):
    store.add_sample({"ts": 1, "cpu": 1})
    store.add_event("close", "old", ts=1)
    store.prune(1, 1, 1, 1)
    assert store.query("select count(*) as n from samples")[0]["n"] == 0
    assert store.events() == []
    grid = store.heatmap()
    assert len(grid) == 7 and all(len(row) == 24 for row in grid)
