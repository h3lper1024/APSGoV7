"""Protect frozen residuals except approved documents; never publish root IDE files."""

import argparse
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = Path(
    "docs/implementation/evidence/solverpy_path_cover_local_search/function_00_project_boundary"
)
STABLE_FILE = "protected_stable_workspace_residuals.sha256"
VOLATILE_FILE = "volatile_workspace_residuals.paths"
MANIFEST_HASHES = {
    STABLE_FILE: "7836b324cb07b00830a08ef28aa01cc5b22ccddac4cb08787a745e5e7f853084",
    VOLATILE_FILE: "aaedd08fa4b8087052d6b42e7db9bf742a4758331f500db2cd99a166bdeb8e4c",
}
CACHE_NAMES = {".DS_Store", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"}
PROMOTED_DOCUMENTS = {
    "docs/design/assets/apsgo_v6_solverpy_v6_combined_flow/solverpy_v6_combined_solver_flow.drawio",
    "docs/design/assets/apsgo_v6_solverpy_v6_combined_flow/solverpy_v6_combined_solver_flow.png",
}


def is_volatile(path: str) -> bool:
    parts = Path(path).parts
    return (
        path == ".idea/workspace.xml"
        or any(part in CACHE_NAMES or part.startswith(".coverage") for part in parts)
        or Path(path).suffix in {".pyc", ".pyo"}
    )


def is_root_ide(path: str) -> bool:
    return Path(path).parts[:1] == (".idea",)


def git_paths(root: Path, *args: str) -> set[str]:
    output = subprocess.check_output(["git", "-C", str(root), "ls-files", "-z", *args])
    return set(output.decode("utf-8").split("\0")) - {""}


def read_manifests(root: Path) -> tuple[dict[str, str], set[str]]:
    contents = {}
    for name, digest in MANIFEST_HASHES.items():
        raw = (root / EVIDENCE / name).read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError(f"Frozen manifest changed: {name}")
        contents[name] = raw.decode("utf-8").splitlines()
    stable = dict(line.split("\t") for line in contents[STABLE_FILE])
    volatile = set(contents[VOLATILE_FILE])
    if len(stable) != 16 or len(volatile) != 992:
        raise ValueError("Frozen residual count changed")
    if any(is_volatile(path) for path in stable) or not all(map(is_volatile, volatile)):
        raise ValueError("Frozen residual classification changed")
    return stable, volatile


def verify(root: Path, stable: dict[str, str], volatile: set[str]) -> dict:
    errors = []
    protected = (set(stable) | volatile) - PROMOTED_DOCUMENTS
    # Promotion changes current ownership, not the historical manifest or its hashes.
    active_stable = {
        path: digest
        for path, digest in stable.items()
        if not is_root_ide(path) and path not in PROMOTED_DOCUMENTS
    }
    for path in sorted(PROMOTED_DOCUMENTS):
        file = root / path
        parts = Path(path).parts
        # Check only within this root; system aliases above it are outside this policy.
        if not file.is_file() or any(
            root.joinpath(*parts[:length]).is_symlink() for length in range(len(parts) + 1)
        ):
            errors.append({"missing_or_linked_promoted_document": path})
    if (root / ".git").exists():
        mode = "shared_workspace"
        tracked = git_paths(root, "--cached")
        if PROMOTED_DOCUMENTS - tracked:
            errors.append({"untracked_promoted_documents": sorted(PROMOTED_DOCUMENTS - tracked)})
        leaked = sorted(
            (tracked & protected) | {p for p in tracked if is_volatile(p) or is_root_ide(p)}
        )
        if leaked:
            errors.append({"tracked_residuals": leaked})
        for path, digest in active_stable.items():
            file = root / path
            if file.is_symlink() or not file.is_file():
                errors.append({"missing_or_linked_stable_residual": path})
            elif hashlib.sha256(file.read_bytes()).hexdigest() != digest:
                errors.append({"changed_stable_residual": path})
        # Taking the union prevents .gitignore changes from hiding protected files.
        remaining = git_paths(root, "--others", "--exclude-standard") | git_paths(
            root, "--others", "--ignored", "--exclude-standard"
        )
        if set(active_stable) - remaining:
            errors.append(
                {"stable_residuals_not_untracked": sorted(set(active_stable) - remaining)}
            )
        missing_volatile = sorted(p for p in volatile - remaining if not is_root_ide(p))
        new_volatile = sorted(
            p for p in remaining - protected if is_volatile(p) and not is_root_ide(p)
        )
    else:
        mode = "clean_export"
        leaked = {p for p in protected if (root / p).exists() or (root / p).is_symlink()}
        if (root / ".idea").exists() or (root / ".idea").is_symlink():
            leaked.add(".idea")
        if leaked:
            errors.append({"exported_residuals": sorted(leaked)})
        missing_volatile, new_volatile = [], []
    return {
        "status": "fail" if errors else "pass",
        "mode": mode,
        "stable_count": len(stable),
        "volatile_count": len(volatile),
        "volatile_paths_removed": missing_volatile,
        "volatile_paths_added": new_volatile,
        "volatile_content": "Not frozen; root .idea is ignored locally but forbidden in Git and exports.",
        "errors": errors,
    }


def self_test() -> None:
    assert is_volatile("src/a/__pycache__/x.pyc")
    assert is_volatile(".idea/workspace.xml")
    assert is_volatile("tests/.coverage.branch")
    assert not is_volatile(".idea/modules.xml")
    assert not is_volatile("src/core/cache.py")
    assert is_root_ide(".idea/modules.xml")
    assert not is_root_ide("nested/.idea/modules.xml")
    with tempfile.TemporaryDirectory(prefix="apsgo-residual-check-") as directory:
        root = Path(directory)
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        file = root / "legacy.txt"
        file.write_bytes(b"keep")
        ide_file = root / ".idea" / "modules.xml"
        ide_file.parent.mkdir()
        ide_file.write_bytes(b"keep")
        nested_ide = root / "nested" / ".idea" / "modules.xml"
        nested_ide.parent.mkdir(parents=True)
        nested_ide.write_bytes(b"keep")
        stable = {
            path: hashlib.sha256(b"keep").hexdigest()
            for path in ("legacy.txt", ".idea/modules.xml", "nested/.idea/modules.xml")
        }
        unapproved = "docs/design/assets/apsgo_v6_solverpy_v6_combined_flow/unapproved.txt"
        for path in sorted(PROMOTED_DOCUMENTS | {unapproved}):
            document = root / path
            document.parent.mkdir(parents=True, exist_ok=True)
            document.write_bytes(b"project document" if path in PROMOTED_DOCUMENTS else b"keep")
            stable[path] = hashlib.sha256(b"keep").hexdigest()
        assert verify(root, stable, set())["errors"] == [
            {"untracked_promoted_documents": sorted(PROMOTED_DOCUMENTS)}
        ]
        subprocess.run(
            ["git", "-C", str(root), "add", "--", *sorted(PROMOTED_DOCUMENTS)], check=True
        )
        for path in sorted(PROMOTED_DOCUMENTS):
            document = root / path
            document.unlink()
            assert {"missing_or_linked_promoted_document": path} in verify(root, stable, set())[
                "errors"
            ]
            document.mkdir()
            assert verify(root, stable, set())["status"] == "fail"
            document.rmdir()
            document.symlink_to(file)
            assert verify(root, stable, set())["status"] == "fail"
            document.unlink()
            document.write_bytes(b"project document")
        assert verify(root, stable, set())["status"] == "pass"
        (root / ".gitignore").write_text("legacy.txt\n/.idea/\n", encoding="utf-8")
        ide_file.write_bytes(b"changed")
        assert verify(root, stable, set())["status"] == "pass"
        ide_file.unlink()
        ide_file.parent.rmdir()
        assert verify(root, stable, set())["status"] == "pass"
        new_ide = root / ".idea" / "new.xml"
        new_ide.parent.mkdir()
        new_ide.write_bytes(b"new")
        assert verify(root, stable, set())["status"] == "pass"
        file.write_bytes(b"changed")
        assert verify(root, stable, set())["status"] == "fail"
        file.unlink()
        assert verify(root, stable, set())["status"] == "fail"
        file.write_bytes(b"keep")
        nested_ide.write_bytes(b"changed")
        assert verify(root, stable, set())["status"] == "fail"
        nested_ide.write_bytes(b"keep")
        assert verify(root, stable, set())["status"] == "pass"
        subprocess.run(["git", "-C", str(root), "add", "-f", ".idea/new.xml"], check=True)
        report = verify(root, stable, set())
        assert report["status"] == "fail"
        assert ".idea/new.xml" in report["errors"][0]["tracked_residuals"]
        subprocess.run(["git", "-C", str(root), "add", "-f", "legacy.txt"], check=True)
        assert "legacy.txt" in verify(root, stable, set())["errors"][0]["tracked_residuals"]
        subprocess.run(["git", "-C", str(root), "add", "--", unapproved], check=True)
        assert unapproved in verify(root, stable, set())["errors"][0]["tracked_residuals"]
        export = root / "export"
        export.mkdir()
        assert verify(export, stable, set())["status"] == "fail"
        for path in sorted(PROMOTED_DOCUMENTS):
            document = export / path
            document.parent.mkdir(parents=True, exist_ok=True)
            document.write_bytes(b"project document")
        assert verify(export, stable, set())["status"] == "pass"
        for candidate_root in (root, export):
            parent = candidate_root / Path(sorted(PROMOTED_DOCUMENTS)[0]).parent
            detached = root / "detached-documents"
            parent.rename(detached)
            parent.symlink_to(detached, target_is_directory=True)
            errors = verify(candidate_root, stable, set())["errors"]
            assert all(
                {"missing_or_linked_promoted_document": path} in errors
                for path in PROMOTED_DOCUMENTS
            )
            parent.unlink()
            detached.rename(parent)
        outside_link = root / "outside-link"
        outside_link.symlink_to(root, target_is_directory=True)
        assert verify(outside_link / "export", stable, set())["status"] == "pass"
        outside_link.unlink()
        for path in sorted(PROMOTED_DOCUMENTS):
            document = export / path
            document.unlink()
            document.symlink_to(file)
            assert {"missing_or_linked_promoted_document": path} in verify(export, stable, set())[
                "errors"
            ]
            document.unlink()
            document.write_bytes(b"project document")
        (export / unapproved).write_bytes(b"keep")
        assert verify(export, stable, set())["errors"] == [{"exported_residuals": [unapproved]}]
        (export / unapproved).unlink()
        assert verify(export, stable, set())["status"] == "pass"
        exported_ide = export / ".idea" / "not-in-manifest.xml"
        exported_ide.parent.mkdir()
        exported_ide.write_bytes(b"new")
        assert verify(export, stable, set())["status"] == "fail"
        exported_ide.unlink()
        exported_ide.parent.rmdir()
        (export / "legacy.txt").write_bytes(b"keep")
        assert verify(export, stable, set())["status"] == "fail"
    print(
        "Residual guard self-test passed (promoted documents, IDE/protected drift, staging, export)."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--verify", action="store_true")
    mode.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return 0
    try:
        stable, volatile = read_manifests(ROOT)
        report = verify(ROOT, stable, volatile)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        report = {"status": "fail", "errors": [str(error)]}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return int(report["status"] != "pass")


if __name__ == "__main__":
    raise SystemExit(main())
