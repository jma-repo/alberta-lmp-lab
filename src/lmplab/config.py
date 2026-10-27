"""Paths and settings shared by every step."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(os.environ.get("LMPLAB_REPO", Path(__file__).resolve().parents[2])).resolve()


@dataclass(frozen=True)
class Paths:
    """Sources live in the repo; generated files in the work directory (the same folder by default).

    Raw AESO files and the warehouse are git-ignored and rebuilt with `lmplab fetch` and
    `lmplab ingest`; `artifacts/` holds the derived results the dashboard needs.
    """

    repo: Path
    work: Path

    @classmethod
    def default(cls) -> Paths:
        return cls(repo=REPO_ROOT, work=Path(os.environ.get("LMPLAB_HOME", REPO_ROOT)).resolve())

    @property
    def configs(self) -> Path:
        return self.repo / "configs"

    @property
    def raw(self) -> Path:
        return self.work / "data" / "raw"

    @property
    def warehouse(self) -> Path:
        return self.work / "data" / "warehouse.duckdb"

    @property
    def mlflow_uri(self) -> str:
        return "sqlite:///" + (self.work / "mlflow.db").as_posix()

    @property
    def mlflow_artifacts(self) -> Path:
        return self.work / "mlartifacts"

    @property
    def artifacts(self) -> Path:
        return self.work / "artifacts"

    @property
    def reports(self) -> Path:
        return self.artifacts / "reports"

    @property
    def models(self) -> Path:
        return self.artifacts / "models"

    @property
    def pdup(self) -> Path:
        return self.repo / "pdup"

    @property
    def knowledge(self) -> Path:
        return self.repo / "knowledge"

    @property
    def site(self) -> Path:
        return self.work / "site"


@dataclass(frozen=True)
class Settings:
    paths: Paths

    @cached_property
    def config(self) -> dict[str, Any]:
        with (self.paths.configs / "lmplab.yaml").open(encoding="utf-8") as handle:
            return yaml.safe_load(handle)

    @property
    def seed(self) -> int:
        return int(self.config["seed"])

    def source_path(self, name: str) -> Path:
        return self.paths.work / self.config["sources"][name]["path"]


def settings() -> Settings:
    return Settings(Paths.default())
