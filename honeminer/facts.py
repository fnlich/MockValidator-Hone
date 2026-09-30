"""Deterministic workspace scan: the facts the generated CLAUDE.md is built from.

No model is involved. Every command found here is later run once in the
grading image (``solve.verify_facts``); commands that cannot even start are dropped
before Claude sees them.
"""

from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Environment for every tool in the sandbox: the image has a read-only root,
# no network and no home directory, so caches and build output go to /tmp.
SANDBOX_ENV = {
    "HOME": "/tmp/home",
    "PYTHONDONTWRITEBYTECODE": "1",
    "CARGO_HOME": "/tmp/cargo",
    "CARGO_TARGET_DIR": "/tmp/target",
    "CARGO_NET_OFFLINE": "true",
    "GOCACHE": "/tmp/gocache",
    "GOPATH": "/tmp/gopath",
    "GOFLAGS": "-mod=mod",
    "GOPROXY": "off",
    "GOTOOLCHAIN": "local",
    "npm_config_cache": "/tmp/npm",
    "npm_config_offline": "true",
}

EXTENSIONS = {
    ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".rs": "rust",
    ".go": "go", ".py": "python", ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".java": "java", ".sh": "bash",
}
TEST_PATTERNS = (
    "tests/**", "test/**", "**/tests/**", "**/test/**", "src/test/**", "**/__tests__/**",
    "*_test.go", "**/*_test.go", "test_*.py", "**/test_*.py", "*_test.py", "**/*_test.py",
    "*.test.js", "**/*.test.js", "*.spec.js", "**/*.spec.js", "*.test.ts", "**/*.test.ts",
    "**/*Test.java", "*Test.java",
)
DOC_NAMES = ("README", "README.md", "README.rst", "README.txt", "CONTRIBUTING.md")
SKIP_DIRS = {".git", "node_modules", "target", "__pycache__"}
_IDENTIFIER = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\b")


@dataclass(frozen=True)
class Facts:
    task_type: str
    task_kind: str
    language: str
    working_directory: str
    result_tree_path: str
    build_cmd: str | None
    test_cmd: str | None
    driver_recipe: str | None
    protected: tuple[str, ...]
    doc_files: tuple[str, ...]
    gotchas: tuple[str, ...]
    languages: dict[str, int] = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return self.task_type == "terminal_script_v1"

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True) + "\n"


def _files(root: Path) -> list[str]:
    found = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part in SKIP_DIRS for part in relative.parts[:-1]) or not path.is_file():
            continue
        found.append(relative.as_posix())
    return found


def _languages(files: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for name in files:
        language = EXTENSIONS.get(Path(name).suffix.lower())
        if language and not name.startswith((".rlvr/", ".prebuilt/")):
            counts[language] = counts.get(language, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def _package_scripts(root: Path) -> dict[str, str]:
    try:
        return json.loads((root / "package.json").read_text()).get("scripts", {}) or {}
    except (OSError, ValueError, AttributeError):
        return {}


def _makefile_targets(root: Path) -> set[str]:
    for name in ("Makefile", "makefile", "GNUmakefile"):
        path = root / name
        if path.is_file():
            text = path.read_text(errors="replace")
            return set(re.findall(r"^([A-Za-z0-9_.-]+)\s*:(?!=)", text, flags=re.M))
    return set()


def _crate_name(root: Path) -> str:
    try:
        text = (root / "Cargo.toml").read_text()
    except OSError:
        return "crate"
    match = re.search(r'^\s*name\s*=\s*"([^"]+)"', text, flags=re.M)
    return (match.group(1) if match else "crate").replace("-", "_")


def _c_driver(language: str, inputs: str, base: str = "/work") -> str:
    compiler, ext = ("g++ -std=c++17", "cpp") if language == "cpp" else ("gcc -std=c11", "c")
    return f"{compiler} -I{base} /tmp/d.{ext} {inputs} -o /tmp/d".replace("  ", " ")


def _rust_driver(crate: str, base: str = "/work") -> str:
    lib = f"/tmp/lib{crate}.rlib"
    return (
        f"rustc --edition 2021 --crate-type rlib --crate-name {crate} {base}/src/lib.rs -o {lib}"
        f" && rustc --edition 2021 /tmp/d.rs --extern {crate}={lib} -o /tmp/d"
    )


def _go_driver(module: str, base: str = "/work") -> str:
    return (
        f"cp -r {base} /tmp/w && mkdir -p /tmp/w/cmd/chk"
        f" && (write /tmp/w/cmd/chk/main.go importing {module}) && cd /tmp/w && go run ./cmd/chk"
    )


def _node_main(root: Path) -> str:
    try:
        return json.loads((root / "package.json").read_text()).get("main") or "index.js"
    except (OSError, ValueError, AttributeError):
        return "index.js"


BuildInfo = tuple[str | None, str | None, list[str]]


def _build_and_driver(root: Path, files: list[str], language: str, base: str = "/work") -> BuildInfo:
    """Build command (run in the project directory) and a driver recipe whose paths live under ``base``,
    the project directory inside the container (``/work`` or ``/work/<working_directory>``)."""

    gotchas: list[str] = []
    if (root / ".rlvr" / "build.py").is_file():
        libraries = sorted(n for n in files if n.startswith(".prebuilt/") and n.endswith(".a"))
        include = f"-I{base}/include" if (root / "include").is_dir() else ""
        driver = _c_driver(language, f"{include} {base}/{libraries[0]}", base) if libraries else None
        gotchas.append("The build rewrites tracked files under .prebuilt/. That is expected; "
                       "those changes are removed from your diff automatically.")
        return "python3 .rlvr/build.py", driver, gotchas
    if (root / "CMakeLists.txt").is_file():
        gotchas.append("CMakeLists.txt exists but cmake is not installed.")
    if (root / "Cargo.toml").is_file():
        driver = _rust_driver(_crate_name(root), base) if (root / "src" / "lib.rs").is_file() else None
        return "cargo build --offline", driver, gotchas
    if (root / "go.mod").is_file():
        module = re.search(r"^module\s+(\S+)", (root / "go.mod").read_text(), flags=re.M)
        return "go build ./...", _go_driver(module.group(1) if module else "the module", base), gotchas
    if (root / "package.json").is_file() or language in ("javascript", "typescript"):
        build = "npm run build" if "build" in _package_scripts(root) else None
        if build is None and (root / "tsconfig.json").is_file():
            build = "tsc --noEmit -p ."
        return build, f"node -e \"const m = require('{base}/{_node_main(root)}'); ...\"", gotchas
    if language in ("c", "cpp"):
        sources = " ".join(
            f"{base}/{n}" for n in files
            if n.endswith((".c", ".cpp", ".cc")) and not re.search(r"(^|/)main\.(c|cpp|cc)$", n)
        )
        return ("make" if _makefile_targets(root) else None), _c_driver(language, sources, base), gotchas
    if language == "java":
        compile_all = f"javac -d /tmp/classes $(find {base} -name '*.java')"
        return compile_all, f"{compile_all} /tmp/Check.java && java -cp /tmp/classes Check", gotchas
    if language == "python":
        build = ("python3 -c \"import ast, pathlib; [ast.parse(p.read_bytes(), str(p)) "
                 "for p in pathlib.Path('.').rglob('*.py')]\"")
        return build, f"python3 -c \"import sys; sys.path.insert(0, '{base}'); import <module>; ...\"", gotchas
    return None, None, gotchas


def _test_command(root: Path, files: list[str]) -> str | None:
    targets = _makefile_targets(root)
    if (root / "Cargo.toml").is_file() and (any(n.startswith("tests/") for n in files)
                                           or any("#[cfg(test)]" in (root / n).read_text(errors="replace")
                                                  for n in files if n.endswith(".rs"))):
        return "cargo test --offline"
    if (root / "go.mod").is_file() and any(n.endswith("_test.go") for n in files):
        return "go test ./..."
    if "test" in _package_scripts(root):
        return "npm test"
    if any(fnmatch.fnmatchcase(n, "*.test.js") or fnmatch.fnmatchcase(n, "**/*.test.js") for n in files):
        return "node --test"
    if "test" in targets:
        return "make test"
    if "check" in targets:
        return "make check"
    if any(fnmatch.fnmatchcase(n, pattern) for n in files for pattern in ("test_*.py", "**/test_*.py")):
        return "python3 -m pytest -q"
    return None


def _doc_files(root: Path, files: list[str], instruction: str) -> tuple[str, ...]:
    tokens = {
        token for token in _IDENTIFIER.findall(instruction)
        if len(token) >= 4 and (any(ch.isupper() for ch in token[1:]) or "_" in token or "." in token)
    }
    squashed = {re.sub(r"[^a-z0-9]", "", token.lower()) for token in tokens}

    def relevance(name: str) -> tuple[int, str]:
        stem = re.sub(r"[^a-z0-9]", "", Path(name).stem.lower())
        hit = any(len(stem) >= 4 and (token in stem or stem in token) for token in squashed)
        return (0 if hit else 1 if Path(name).name in DOC_NAMES else 2, name)

    top_level = [n for n in files if "/" not in n and Path(n).name in DOC_NAMES]
    docs = sorted(top_level + [n for n in files if n.startswith(("docs/", "doc/"))], key=relevance)
    named: list[str] = []
    for token in sorted(tokens):
        stem = token.split(".")[0].lower()
        for name in files:
            if name.startswith((".rlvr/", ".prebuilt/")) or name in named:
                continue
            base = Path(name).stem.lower()
            if base == stem or base == token.lower():
                named.append(name)
    for name in files:
        if len(named) >= 6:
            break
        if not name.endswith((".h", ".hpp", ".py", ".go", ".rs", ".js", ".ts", ".java")) or name in named:
            continue
        if name.startswith((".rlvr/", ".prebuilt/")):
            continue
        try:
            text = (root / name).read_text(errors="replace")
        except OSError:
            continue
        if any(re.search(rf"\b(class|struct|func|fn|def|function)\s+{re.escape(t)}\b", text) for t in tokens):
            named.append(name)
    return tuple(dict.fromkeys(docs[:4] + named[:6]))


def scan(
    root: Path,
    *,
    task_type: str,
    instruction: str,
    task_kind: str = "bug_fix",
    language: str = "",
    working_directory: str = ".",
    result_tree_path: str = ".",
) -> Facts:
    files = _files(root)
    languages = _languages(files)
    primary = language or (next(iter(languages)) if languages else "unknown")
    protected = tuple(n for n in files if any(fnmatch.fnmatchcase(n, p) for p in TEST_PATTERNS))
    if task_type == "terminal_script_v1":
        return Facts(
            task_type=task_type, task_kind="terminal", language="bash", working_directory=".",
            result_tree_path=result_tree_path, build_cmd=None, test_cmd=None, driver_recipe=None,
            protected=(), doc_files=_doc_files(root, files, instruction), gotchas=(), languages=languages,
        )
    project = root / working_directory if working_directory not in ("", ".") else root
    base = "/work" if working_directory in ("", ".") else f"/work/{working_directory}"
    build, driver, gotchas = _build_and_driver(project, _files(project), primary, base)
    return Facts(
        task_type=task_type, task_kind=task_kind, language=primary, working_directory=working_directory,
        result_tree_path=".", build_cmd=build, test_cmd=_test_command(project, _files(project)),
        driver_recipe=driver, protected=protected, doc_files=_doc_files(root, files, instruction),
        gotchas=tuple(gotchas), languages=languages,
    )
