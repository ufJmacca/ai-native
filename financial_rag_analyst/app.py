from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True, slots=True)
class FinancialRagApplication:
    name: str
    framework: str
    default_response_format: str
    repo_root: Path
    package_root: Path
    docs_dir: Path
    fixtures_dir: Path
    env_example_path: Path


def bootstrap_application(repo_root: Path | None = None) -> FinancialRagApplication:
    resolved_root = repo_root.resolve() if repo_root is not None else Path(__file__).resolve().parents[1]
    load_dotenv(resolved_root / ".env", override=False)

    application = FinancialRagApplication(
        name="financial-rag-analyst",
        framework="google-adk",
        default_response_format="a2ui",
        repo_root=resolved_root,
        package_root=resolved_root / "financial_rag_analyst",
        docs_dir=resolved_root / "docs",
        fixtures_dir=resolved_root / "fixtures",
        env_example_path=resolved_root / ".env.example",
    )
    _ensure_required_paths(application)
    return application


def _ensure_required_paths(application: FinancialRagApplication) -> None:
    required_paths = [
        application.package_root,
        application.docs_dir,
        application.fixtures_dir,
        application.env_example_path,
    ]
    missing_paths = [path for path in required_paths if not path.exists()]
    if missing_paths:
        missing = ", ".join(str(path) for path in missing_paths)
        raise FileNotFoundError(f"Financial RAG scaffold is incomplete. Missing: {missing}")
