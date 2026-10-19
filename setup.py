"""Build glue only (metadata is in pyproject.toml): copies the runtime data kept at the repo root into the wheel."""
import shutil
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py

HERE = Path(__file__).resolve().parent


class BuildPy(build_py):
    def run(self):
        super().run()
        dest = Path(self.build_lib) / "clouddeck" / "data"
        shutil.rmtree(dest, ignore_errors=True)
        dest.mkdir(parents=True)
        for name in ("sdl.yaml.tmpl", "config.example.toml"):
            shutil.copy2(HERE / name, dest / name)
        for d in ("lease", "jobremote"):   # lease/: scripts that run on a dev box / worker; jobremote/: scripts `ddc job` runs on a job's box
            shutil.copytree(HERE / d, dest / d, ignore=shutil.ignore_patterns("__pycache__"))


setup(cmdclass={"build_py": BuildPy})
