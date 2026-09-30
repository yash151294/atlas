"""Derived context for the CLAUDE.md files, generated from the code so it cannot drift: module maps, the pipeline's
nodes, the API routes, the catalog, the parameter sections, the environment variables the code reads, the tests and
the decision index. A CLAUDE.md holds each derived block between `<!-- generated:<name>[ <argument>] -->` and
`<!-- /generated -->`; the prose outside the markers is never touched. Every repository path named in backticks in a
CLAUDE.md must exist (a wrong path costs an agent a blind read and a guess). The pre-commit hook runs this and stages
the result; CI runs it with --check.
Usage: uv run python scripts/context.py [--check]"""
import ast, re, subprocess, sys
from pathlib import Path
import yaml

REPO = Path(__file__).resolve().parent.parent
START = re.compile(r"<!-- generated:([a-z]+)(?: ([^ >]+))? -->\n")
END = "<!-- /generated -->"
ROOT_FILES = {"README.md", "CLAUDE.md", "Dockerfile", "pyproject.toml", "uv.lock"}
DATA_PREFIXES = ("data/", "Files/", "Tables/", "dist/", ".env", "http", "abfss:", "/", "~", "<")   # data-root and external paths, not repository files


def tracked() -> list[str]:
    """Repository files: tracked plus new files not ignored, so a CLAUDE.md added in the same commit counts."""
    out = subprocess.check_output(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=REPO, text=True).splitlines()
    return [f for f in out if (REPO / f).exists()]   # the index can still list a file deleted from the working tree


def first_line(doc: str | None) -> str:
    return (doc or "").strip().split("\n")[0].strip().rstrip(".")


def modules(folder: str) -> str:
    """One line per module: path, size, the first line of its docstring (the docstring is the documentation; keep it true)."""
    rows = []
    for p in sorted((REPO / folder).glob("*.py")):
        src = p.read_text()
        if p.name == "__init__.py" and not src.strip(): continue
        rows.append(f"- `{p.relative_to(REPO)}` ({src.count(chr(10))} lines): {first_line(ast.get_docstring(ast.parse(src))) or 'no docstring'}")
    return "\n".join(rows)


def nodes() -> str:
    """The PIPELINE list in atlas/pipeline.py: node, function, catalog inputs, catalog outputs."""
    tree = ast.parse((REPO / "atlas/pipeline.py").read_text())
    assign = next(n for n in tree.body if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "PIPELINE")
    rows = ["| node | function | reads | writes |", "|---|---|---|---|"]
    for call in assign.value.elts:
        name, fn, ins, outs = call.args
        rows.append(f"| {ast.literal_eval(name)} | `{ast.unparse(fn)}` | {', '.join(ast.literal_eval(ins))} | {', '.join(ast.literal_eval(outs)) or 'the serving tables, the Eventhouse table, the cache'} |")
    return "\n".join(rows)


def routes() -> str:
    """Every FastAPI route in app/main.py with its handler and the first line of its docstring."""
    tree = ast.parse((REPO / "app/main.py").read_text())
    rows = ["| route | handler | purpose |", "|---|---|---|"]
    for f in tree.body:
        if not isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)): continue
        for d in f.decorator_list:
            if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr in ("get", "post", "put", "delete") and d.args:
                rows.append(f"| {d.func.attr.upper()} `{ast.literal_eval(d.args[0])}` | `{f.name}` | {first_line(ast.get_docstring(f))} |")
    return "\n".join(rows)


def catalog() -> str:
    """Paths are under the data root, not the repository, so they are not backticked (the path check would look for them)."""
    rows = ["| dataset | path under the data root | format | note |", "|---|---|---|---|"]
    for line in (REPO / "conf/catalog.yaml").read_text().splitlines():
        m = re.match(r"(\w+):\s*\{path: ([^,]+), format: (\w+)\}\s*(?:#\s*(.*))?$", line)
        if m: rows.append(f"| {m[1]} | {m[2]} | {m[3]} | {m[4] or ''} |")
    return "\n".join(rows)


def parameters() -> str:
    """Sections and keys only; the values and their comments are in the file itself."""
    return "\n".join(f"- **{k}**: {', '.join(v)}" for k, v in yaml.safe_load((REPO / "conf/parameters.yaml").read_text()).items())


def env() -> str:
    """Environment variables the code reads (os.environ / os.getenv), with the files that read them."""
    seen: dict[str, set[str]] = {}
    for rel in tracked():
        if not rel.endswith(".py"): continue
        for m in re.finditer(r'os\.(?:environ\.get|getenv|environ\[)\s*\(?\s*"([A-Z][A-Z0-9_]+)"', (REPO / rel).read_text()):
            seen.setdefault(m[1], set()).add(rel)
    return "\n".join(f"- `{k}`: {', '.join(sorted(v))}" for k, v in sorted(seen.items()))


def tests() -> str:
    rows = []
    for p in sorted((REPO / "tests").glob("test_*.py")):
        for f in ast.parse(p.read_text()).body:
            if isinstance(f, ast.FunctionDef) and f.name.startswith("test_"):
                doc = first_line(ast.get_docstring(f))
                rows.append(f"- `{f.name}`" + (f": {doc}" if doc else ""))
    return "\n".join(rows)


def adrs() -> str:
    return "\n".join(f"- {n}. {t}" for n, t in re.findall(r"^(\d+)\. \*\*(.+?)\*\*", (REPO / "docs/architecture.md").read_text(), re.M))


GENERATORS = dict(modules=modules, nodes=nodes, routes=routes, catalog=catalog, parameters=parameters, env=env, tests=tests, adrs=adrs)


def render(text: str) -> str:
    out, pos = [], 0
    for m in START.finditer(text):
        end = text.index(END, m.end())
        out.append(text[pos:m.end()] + (GENERATORS[m[1]](m[2]) if m[2] else GENERATORS[m[1]]()) + "\n"); pos = end
    return "".join(out) + text[pos:]


def missing_paths(text: str, files: list[str]) -> list[str]:
    """Backticked tokens that look like repository paths (contain a slash, or are a root file) and are neither a tracked file nor a directory of one."""
    out = []
    for tok in re.findall(r"`([^`\n]+)`", text):
        tok = tok.split(":")[0].rstrip("/")
        if ("/" not in tok and tok not in ROOT_FILES) or " " in tok or tok.startswith(DATA_PREFIXES) or not re.fullmatch(r"[A-Za-z0-9_.][A-Za-z0-9_./-]*", tok): continue
        if tok not in files and not any(f.startswith(tok + "/") for f in files): out.append(tok)
    return sorted(set(out))


def main() -> int:
    check = "--check" in sys.argv
    files = tracked(); stale, broken = [], []
    for p in sorted(x for x in REPO.rglob("CLAUDE.md") if ".venv" not in x.parts):
        rel = str(p.relative_to(REPO)); old = p.read_text(); new = render(old)
        if new != old:
            stale.append(rel)
            if not check: p.write_text(new)
        broken += [f"{rel}: `{m}`" for m in missing_paths(new, files)]
    for s in stale: print(("stale: " if check else "regenerated: ") + s)
    for b in broken: print("missing path in " + b)
    return 1 if broken or (check and stale) else 0


if __name__ == "__main__":
    sys.exit(main())
