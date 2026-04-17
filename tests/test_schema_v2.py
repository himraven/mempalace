from mempalace.schema_v2 import enrich, validate


def _valid_v2(i: int = 0, **overrides):
    doc = {
        "schema_version": "v2",
        "memory_type": "decision",
        "source_system": "daemon",
        "source_id": f"src-{i}",
        "wing": "nova",
        "room": "decisions",
        "created_at": "2026-04-16T10:00:00Z",
        "event_time": "2026-04-16T09:58:00Z",
    }
    doc.update(overrides)
    return doc


def test_ten_valid_v2_memories():
    cases = [
        _valid_v2(i, memory_type=memory_type)
        for i, memory_type in enumerate(
            [
                "cron_summary",
                "llm_output",
                "diary_daily",
                "raven_feedback",
                "raven_preference",
                "kg_event",
                "decision",
                "incident",
                "legacy_import",
                "claude_code_memory",
            ]
        )
    ]

    for case in cases:
        ok, errors = validate(enrich(case))
        assert ok, errors


def test_five_invalid_v2_memories():
    cases = [
        _valid_v2(schema_version="v3"),
        _valid_v2(memory_type="bad"),
        _valid_v2(importance=6),
        _valid_v2(privacy_scope="specific-agents", readable_by=[]),
        _valid_v2(created_at="not-a-date"),
    ]

    for case in cases:
        ok, errors = validate(enrich(case))
        assert not ok
        assert errors


def test_five_valid_legacy_memories():
    cases = [
        {
            "schema_version": "legacy-import-v1",
            "stamp_source": "phase2_schema_stamp",
            "stamped_at": f"2026-04-16T10:0{i}:00Z",
        }
        for i in range(5)
    ]

    for case in cases:
        ok, errors = validate(case)
        assert ok, errors


def test_enrich_fills_defaults_without_mutating():
    original = _valid_v2()
    enriched = enrich(original, {"source_system": "raven-manual"})

    assert enriched["importance"] == 3
    assert enriched["privacy_scope"] == "agent-wide"
    assert enriched["source_system"] == "daemon"
    assert "importance" not in original
