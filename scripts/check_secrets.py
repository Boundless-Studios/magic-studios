#!/usr/bin/env python3
"""Full-history and raw-object Gitleaks checks with metadata-only public output.

The caller fetches refs first. No repository code is executed, ignored-file
rules cannot hide tracked objects, and no raw scanner output is printed.
``--baseline`` names reviewed findings already in history (by fingerprint, never
by value); they are reported but only a finding outside it fails the check.
"""

import argparse
import gzip
import hashlib
import io
import tarfile
import zipfile
import json
from pathlib import Path
import subprocess
import sys
import tempfile


MAX_OBJECT_BYTES = 64 * 1024 * 1024
MAX_EXPANDED_BYTES = 128 * 1024 * 1024


def sensitive_payloads(data: bytes, object_id: str, depth: int = 0, budget: list[int] | None = None, emit=None) -> list[dict]:
    """Reject saved infrastructure state and inspect archives independent of regex heuristics.

    Extract only into memory. Limits fail closed; neither member names nor values
    are copied into public findings because either could contain private data.
    """
    budget = [MAX_EXPANDED_BYTES] if budget is None else budget
    if depth > 3 or len(data) > MAX_OBJECT_BYTES:
        raise RuntimeError("archive inspection limit exceeded")
    def finding(rule):
        return {"RuleID": rule, "File": "blob-" + object_id, "StartLine": None, "EndLine": None, "Commit": ""}
    if data.startswith(b"PK\x03\x04"):
        results = []
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > 10000:
                raise RuntimeError("archive entry limit exceeded")
            for entry in entries:
                if entry.is_dir():
                    continue
                if Path(entry.filename).name in {"tfstate", "tfstate-prev", "tfplan"}:
                    results.append(finding("saved-terraform-plan"))
                budget[0] -= entry.file_size
                if entry.file_size > MAX_OBJECT_BYTES or budget[0] < 0:
                    raise RuntimeError("archive expansion limit exceeded")
                results.extend(sensitive_payloads(archive.read(entry), object_id, depth + 1, budget, emit))
        return results
    if data.startswith(b"\x1f\x8b"):
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as archive:
            expanded = archive.read(min(MAX_OBJECT_BYTES, budget[0]) + 1)
        budget[0] -= len(expanded)
        if budget[0] < 0:
            raise RuntimeError("archive expansion limit exceeded")
        return sensitive_payloads(expanded, object_id, depth + 1, budget, emit)
    if len(data) > 262 and data[257:262] == b"ustar":
        results = []
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            for count, entry in enumerate(archive):
                if count > 10000:
                    raise RuntimeError("archive entry limit exceeded")
                if not entry.isfile():
                    continue
                budget[0] -= entry.size
                if entry.size > MAX_OBJECT_BYTES or budget[0] < 0:
                    raise RuntimeError("archive expansion limit exceeded")
                results.extend(sensitive_payloads(archive.extractfile(entry).read(), object_id, depth + 1, budget, emit))
        return results
    if emit is not None and depth > 0:
        emit(data)
    try:
        parsed = json.loads(data)
    except (ValueError, UnicodeError):
        return []
    if isinstance(parsed, dict):
        if "terraform_version" in parsed and ("resources" in parsed or "outputs" in parsed):
            return [finding("terraform-state")]
        if parsed.get("type") == "service_account" and parsed.get("private_key"):
            return [finding("service-account-private-key")]
    return []


def run_scan(tool: Path, arguments: list[str], work: Path, name: str, root: Path | None = None) -> dict:
    report = work / f"{name}.json"
    config = work / "default-rules.toml"
    config.write_text("[extend]\nuseDefault = true\n")
    ignore = work / "empty-ignore"
    ignore.write_text("")
    result = subprocess.run(
        [str(tool), *arguments, "--no-banner", "--redact=100",
         "--ignore-gitleaks-allow", "--max-decode-depth=2", "--max-archive-depth=2",
         "--config=" + str(config), "--gitleaks-ignore-path=" + str(ignore),
         "--report-format=json", "--report-path=" + str(report)],
        cwd=work, capture_output=True, text=True, timeout=300,
    )
    # Never print stdout/stderr or matched credential fields, even on failure.
    if result.returncode not in (0, 1) or not report.is_file():
        raise RuntimeError(f"{name}: scanner failed to complete")
    findings = json.loads(report.read_text())
    if not isinstance(findings, list) or (result.returncode == 1 and not findings):
        raise RuntimeError(f"{name}: inconsistent scanner result")
    safe = []
    for finding in findings:
        metadata = {
            key: finding.get(key)
            for key in ("RuleID", "StartLine", "EndLine", "Commit")
        }
        # A path can itself contain private identifiers or credential text.
        # Keep an opaque correlation key; never echo scanner-supplied paths.
        path = str(finding.get("File", ""))
        if root is not None and path.startswith(str(root) + "/"):
            path = path[len(str(root)) + 1:]  # temp dir differs per run
        metadata["File"] = "path-sha256:" + hashlib.sha256(path.encode()).hexdigest()
        safe.append(metadata)
    return {"scan": name, "finding_count": len(safe), "findings": safe}


def check(repository: Path, tool: Path, baseline: frozenset[str] = frozenset()) -> int:
    repository, tool = repository.resolve(), tool.resolve()

    def git(*args: str, input: bytes | None = None) -> bytes:
        return subprocess.check_output(
            ["git", "-C", str(repository), *args], input=input,
            stderr=subprocess.PIPE, timeout=120,
        )

    if git("rev-parse", "--is-shallow-repository").strip() != b"false":
        raise RuntimeError("full history is required; shallow checkout refused")
    git("fsck", "--full", "--no-dangling")
    objects = git("rev-list", "--objects", "--all", "HEAD", "--no-object-names")
    descriptions = git(
        "cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)", input=objects
    ).decode().splitlines()
    with tempfile.TemporaryDirectory(prefix="studio-secrets-") as directory:
        work = Path(directory)
        corpus = work / "corpus"
        corpus.mkdir()
        counts = {"blob": 0, "commit": 0, "tag": 0}
        payload_findings = []
        for description in descriptions:
            sha, kind, size = description.split()
            if kind not in counts:
                continue
            if int(size) > MAX_OBJECT_BYTES:
                raise RuntimeError("object exceeds inspection limit")
            content = git("cat-file", kind, sha)
            if len(content) != int(size):
                raise RuntimeError("incomplete object read")
            if kind == "blob":
                payload_findings.extend(sensitive_payloads(
                    content, sha, emit=lambda data: (
                        corpus / ("expanded-" + hashlib.sha256(data).hexdigest() + ".txt")
                    ).write_bytes(data),
                ))
            (corpus / f"{kind}-{sha}.txt").write_bytes(content)
            counts[kind] += 1
        results = [
            run_scan(tool, ["git", str(repository), "--log-opts=--all HEAD --full-history -m"], work, "history"),
            run_scan(tool, ["dir", str(corpus)], work, "raw-objects", root=corpus),
        ]
        results.append({"scan": "sensitive-payloads", "finding_count": len(payload_findings), "findings": payload_findings})
        results = [without_known(result, baseline) for result in results]
        print("SECRETS_CHECK " + json.dumps({"objects": counts, "scans": results}, sort_keys=True))
        return 1 if any(result["finding_count"] for result in results) else 0


def fingerprint(scan: str, finding: dict) -> str:
    fields = (str(finding.get(key) or "") for key in ("RuleID", "Commit", "File", "StartLine"))
    return "|".join((scan, *fields))


def without_known(result: dict, baseline: frozenset[str]) -> dict:
    """Count reviewed findings as known; only the rest fail the check."""
    new = [f for f in result["findings"] if fingerprint(result["scan"], f) not in baseline]
    known = len(result["findings"]) - len(new)
    return {**result, "finding_count": len(new), "findings": new, "known_count": known}


def load_baseline(path: Path | None) -> frozenset[str]:
    if path is None:
        return frozenset()
    entries = json.loads(path.read_text())["findings"]
    return frozenset(entry["fingerprint"] for entry in entries)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path("."))
    parser.add_argument("--gitleaks", type=Path, required=True)
    parser.add_argument("--baseline", type=Path)
    args = parser.parse_args()
    try:
        return check(args.repository, args.gitleaks, load_baseline(args.baseline))
    except Exception as exc:
        # Exception text can include scanner output or inputs: only its type is public.
        print(f"Secrets check incomplete ({type(exc).__name__}); failing closed.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
