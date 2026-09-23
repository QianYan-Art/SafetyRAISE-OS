from types import SimpleNamespace

from app.report_harness.execution import outbound_ready
from app.report_harness.lifecycle import active_outbound_runtime
from app.report_harness.schema_migrations import MIGRATIONS, MIGRATIONS_DIR, SCHEMA_VERSIONS


def engineering_runtime():
    return SimpleNamespace(production_outbound_enabled=False, development_outbound_enabled=True,
                           force_engineering_exports=True, business_workflow=object())


def test_formal_and_engineering_runtimes_are_ready_but_partial_objects_are_not():
    assert outbound_ready(SimpleNamespace(production_outbound_enabled=True))
    assert outbound_ready(engineering_runtime())
    partial = engineering_runtime()
    partial.force_engineering_exports = False
    assert not outbound_ready(partial)
    assert not outbound_ready(SimpleNamespace(production_outbound_enabled="yes"))
    assert not outbound_ready(None)


def test_main_runtime_takes_precedence_over_development_server_runtime():
    main, development = engineering_runtime(), engineering_runtime()
    state = SimpleNamespace(report_harness_runtime=main,
                            report_harness_development_runtime=development)
    assert active_outbound_runtime(state) is main
    state.report_harness_runtime = None
    assert active_outbound_runtime(state) is development
    state.report_harness_development_runtime = SimpleNamespace()
    assert active_outbound_runtime(state) is None


def test_migration_list_matches_files_and_versions():
    on_disk = sorted(path.name for path in MIGRATIONS_DIR.glob("*.sql"))
    assert list(MIGRATIONS) == on_disk
    assert SCHEMA_VERSIONS == tuple(int(name[:3]) for name in MIGRATIONS)
    for version, name in zip(SCHEMA_VERSIONS, MIGRATIONS):
        assert f"VALUES ({version})" in (MIGRATIONS_DIR / name).read_text("utf-8")
