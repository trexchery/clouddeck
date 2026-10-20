"""Build glue only (metadata is in pyproject.toml): copies the runtime data kept at the repo root into the wheel."""
import shutil
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py

HERE = Path(__file__).resolve().parent


def skip_workflow_files(directory, names):
    """The shipped workflows are the specs and scripts only: not a checkout's own media, results or my-*.toml specs (keep in step with clouddeck/job/cli.py)."""
    d = Path(directory)
    return [n for n in names if n in ("__pycache__", "outputs") or n.endswith(".pyc") or (n.startswith("my-") and n.endswith(".toml")) or (d.name == "inputs" and n != "README.md")]


class BuildPy(build_py):
    def run(self):
        super().run()
        dest = Path(self.build_lib) / "clouddeck" / "data"
        shutil.rmtree(dest, ignore_errors=True)
        dest.mkdir(parents=True)
        for name in ("sdl.yaml.tmpl", "config.example.toml"):
            shutil.copy2(HERE / name, dest / name)
        shutil.copytree(HERE / "workflows", dest / "workflows", ignore=skip_workflow_files)   # `ddc job workflows --copy DIR` hands these to the user
        for d in ("lease", "jobremote"):   # lease/: scripts that run on a dev box / worker; jobremote/: scripts `ddc job` runs on a job's box
            shutil.copytree(HERE / d, dest / d, ignore=shutil.ignore_patterns("__pycache__"))


setup(cmdclass={"build_py": BuildPy})
