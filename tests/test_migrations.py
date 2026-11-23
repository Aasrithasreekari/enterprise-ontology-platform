from sqlalchemy import create_engine, inspect

from ontology_platform.cli import migrate
from ontology_platform.storage.orm import Base


def test_alembic_migrations_match_orm(tmp_path):
    url = f"sqlite:///{(tmp_path / 'm.db').as_posix()}"
    migrate(url)
    migrate(url)  # idempotent
    engine = create_engine(url)
    insp = inspect(engine)
    tables = set(insp.get_table_names()) - {"alembic_version"}
    assert tables == set(Base.metadata.tables)
    for name, table in Base.metadata.tables.items():
        assert {c["name"] for c in insp.get_columns(name)} == {c.name for c in table.columns}, name
    engine.dispose()
