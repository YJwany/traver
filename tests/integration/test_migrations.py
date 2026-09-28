import os
import subprocess
import sys


def test_alembic_upgrade_downgrade_upgrade_is_repeatable(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'migration.db'}"
    environment = os.environ.copy()
    environment["DATABASE_URL"] = database_url

    for command in (("upgrade", "head"), ("downgrade", "base"), ("upgrade", "head")):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", *command],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
        assert result.returncode == 0, result.stdout + result.stderr
