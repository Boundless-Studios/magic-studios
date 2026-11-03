"""Saved plans must fail the gate even when the pattern scanner reports nothing."""
import importlib.util
import io
import json
from pathlib import Path
import zipfile

import pytest

spec = importlib.util.spec_from_file_location('scanner', Path(__file__).parents[1] / 'scripts/check_secrets.py')
scanner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scanner)


def archive(name, data):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as output:
        output.writestr(name, data)
    return stream.getvalue()


def test_nested_saved_plan_is_rejected_without_printing_values_or_member_names():
    data = archive('private-name.zip', archive('tfstate', json.dumps({'terraform_version': '1', 'resources': [], 'private': 'DO-NOT-PRINT'})))
    findings = scanner.sensitive_payloads(data, 'test-blob')
    assert {f['RuleID'] for f in findings} == {'saved-terraform-plan', 'terraform-state'}
    assert 'DO-NOT-PRINT' not in json.dumps(findings)
    assert 'private-name' not in json.dumps(findings)


def test_plain_state_and_service_account_key_are_rejected():
    assert scanner.sensitive_payloads(b'{"terraform_version":"1","outputs":{}}', 'test')
    assert scanner.sensitive_payloads(b'{"type":"service_account","private_key":"fixture"}', 'test')
    assert scanner.sensitive_payloads(b'{"example":"ordinary configuration"}', 'test') == []


def test_archive_limit_is_an_error_not_a_clean_scan(monkeypatch):
    monkeypatch.setattr(scanner, 'MAX_EXPANDED_BYTES', 4)
    with pytest.raises(RuntimeError, match='expansion'):
        scanner.sensitive_payloads(archive('data', b'12345'), 'test')


def test_annotated_tag_content_is_in_raw_object_scan(tmp_path, monkeypatch, capsys):
    import subprocess

    def git(*args):
        return subprocess.run(
            ['git', '-C', str(tmp_path), '-c', 'user.name=Fixture',
             '-c', 'user.email=fixture@example.test', *args],
            check=True, capture_output=True,
        )

    git('init')
    (tmp_path / 'README').write_text('Clean source')
    git('add', '.')
    git('commit', '-m', 'Initial')
    marker = 'SYNTHETIC_TAG_ONLY_PRIVATE_VALUE'
    git('tag', '-a', 'release-test', '-m', marker)

    def scan(tool, arguments, work, name, root=None):
        if name == 'raw-objects':
            tags = list((work / 'corpus').glob('tag-*.txt'))
            assert len(tags) == 1
            assert marker in tags[0].read_text()
            return {'scan': name, 'finding_count': 1, 'findings': [{'RuleID': 'tag', 'Commit': '', 'File': 'f', 'StartLine': 1}]}
        return {'scan': name, 'finding_count': 0, 'findings': []}

    monkeypatch.setattr(scanner, 'run_scan', scan)
    assert scanner.check(tmp_path, Path('/unused/scanner')) == 1
    output = capsys.readouterr().out
    assert marker not in output
    assert json.loads(output.split('SECRETS_CHECK ', 1)[1])['objects']['tag'] == 1


def test_nested_archive_leaves_are_forwarded_to_pattern_scanning():
    leaves = []
    data = archive('nested.whl', archive('private-config.txt', b'token=SYNTHETIC_ARCHIVE_SECRET'))
    assert scanner.sensitive_payloads(data, 'blob', emit=leaves.append) == []
    assert leaves == [b'token=SYNTHETIC_ARCHIVE_SECRET']


def test_archive_expansion_limit_prevents_emission(monkeypatch):
    monkeypatch.setattr(scanner, 'MAX_EXPANDED_BYTES', 4)
    leaves = []
    with pytest.raises(RuntimeError, match='expansion'):
        scanner.sensitive_payloads(archive('config', b'oversized'), 'blob', emit=leaves.append)
    assert leaves == []


def test_scan_report_does_not_echo_sensitive_filename(tmp_path, monkeypatch):
    from types import SimpleNamespace

    private = 'SYNTHETIC_PRIVATE_PATH_VALUE'

    def run(command, **kwargs):
        report = Path(next(x.split('=', 1)[1] for x in command if x.startswith('--report-path=')))
        report.write_text(json.dumps([{
            'RuleID': 'generic-api-key', 'File': '/private/' + private,
            'StartLine': 3, 'EndLine': 3, 'Commit': 'a' * 40,
            'Secret': private, 'Match': private,
        }]))
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(scanner.subprocess, 'run', run)
    result = scanner.run_scan(Path('/scanner'), ['dir', '/input'], tmp_path, 'fixture')
    assert result['finding_count'] == 1
    assert private not in json.dumps(result)
    assert result['findings'][0]['File'].startswith('path-sha256:')
    assert result['findings'][0]['StartLine'] == 3


def test_baseline_reports_reviewed_findings_but_fails_new_ones():
    known = {'RuleID': 'generic-api-key', 'Commit': 'abc', 'File': 'path-sha256:1', 'StartLine': 3}
    new = {**known, 'StartLine': 4}
    result = {'scan': 'history', 'finding_count': 2, 'findings': [known, new]}
    filtered = scanner.without_known(result, frozenset({scanner.fingerprint('history', known)}))
    assert filtered['finding_count'] == 1
    assert filtered['known_count'] == 1
    assert filtered['findings'] == [new]


def test_baseline_is_per_scan():
    finding = {'RuleID': 'r', 'Commit': '', 'File': 'f', 'StartLine': 1}
    baseline = frozenset({scanner.fingerprint('history', finding)})
    result = {'scan': 'raw-objects', 'finding_count': 1, 'findings': [finding]}
    assert scanner.without_known(result, baseline)['finding_count'] == 1


def test_checked_in_baseline_names_fingerprints_with_reasons():
    path = Path(__file__).parents[1] / '.github/secrets-baseline.json'
    entries = json.loads(path.read_text())['findings']
    # Empty is valid: the public repository starts with no reviewed findings.
    assert all(entry['fingerprint'].count('|') == 4 and entry['reason'] for entry in entries)
    assert scanner.load_baseline(path) == frozenset(entry['fingerprint'] for entry in entries)
