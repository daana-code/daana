"""publish.py against a local fake GitHub API.

The fake serves both repositories the publisher talks to: the source repository
(its prepare run, the run's artifacts and the bundle zip) and this repository
(its release environment and releases). It keeps every request it received and
can inject the faults the publisher must refuse. No real network, token or
repository is used.

MutationTests at the end prove that each guard bites: they remove the guard
from a copy of publish.py and show that the test that owns it then fails.
"""
import hashlib
import http.server
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / '.github/scripts/publish.py'


def load(source=None, name='publish'):
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    module = importlib.util.module_from_spec(spec)
    if source is None:
        spec.loader.exec_module(module)
    else:
        exec(compile(source, str(SCRIPT), 'exec'), module.__dict__)
    return module


publisher = load()

REPO = 'daana-code/daana'
SOURCE = 'daana-code/daana-cli'
TAG = 'v1.0.0-rc.1'
RUN = 7001
TAG_RUN = 4242
SECRET = 'PRIVATE-MARKER-do-not-print'
WORKFLOW = '.github/workflows/release-publish.yml'


def stamp(days_ago=0.0):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime('%Y-%m-%dT%H:%M:%SZ')
ARCHIVES = {'checksums.txt': ['daana-cli_1.0.0-rc.1_linux_amd64.tar.gz', 'daana-cli_1.0.0-rc.1_linux_arm64.tar.gz'],
            'checksums_darwin.txt': ['daana-cli_1.0.0-rc.1_darwin_arm64.tar.gz']}
ALL_ASSETS = sorted(['checksums.txt', 'checksums_darwin.txt', *ARCHIVES['checksums.txt'], *ARCHIVES['checksums_darwin.txt']])


def sha(data):
    return hashlib.sha256(data).hexdigest()


def bundle_files(tag=TAG, candidate=TAG, mode='publish', run=RUN, record=True, mutate=None):
    """The files a daana-cli prepare run uploads: assets, body, record, manifest."""
    files = {}
    for checksums, archives in ARCHIVES.items():
        lines = []
        for name in archives:
            data = ('archive ' + name).encode() * 50
            files['assets/' + name] = data
            lines.append(f'{sha(data)}  {name}')
        files['assets/' + checksums] = ('\n'.join(lines) + '\n').encode()
    files['release-body.md'] = b'## [1.0.0-rc.1] - 2026-12-01\n\n- Real notes.\n'
    if record:
        files['qualification.json'] = b'{"schema_version": 1}\n'
    manifest = {'schema_version': 1, 'source_repository': SOURCE, 'source_workflow': WORKFLOW, 'candidate_tag': candidate, 'release_tag': tag,
                'mode': mode, 'tag_run_id': TAG_RUN, 'tag_run_attempt': 1, 'tag_commit': 'c' * 40, 'prepare_run_id': run,
                'files': [{'name': name, 'size': len(data), 'sha256': sha(data)} for name, data in sorted(files.items())]}
    if mutate:
        mutate(files, manifest)
    files['bundle-manifest.json'] = json.dumps(manifest, indent=2).encode()
    return files


def zip_of(files, extra=None):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        for name, data in files.items():
            archive.writestr(name, data)
        if extra:
            extra(archive)
    return buffer.getvalue()


class FakeGitHub:
    """Both repositories in memory; faults are switched on per test."""

    def __init__(self):
        self.releases, self.assets, self.requests = {}, {}, []
        self.next_id = 100
        self.faults = set()
        # Tags that came into being: a release that is not a draft, or a git ref.
        self.tags_created = []
        self.gone = set()
        self.environment = {'name': 'release', 'can_admins_bypass': False,
                            'protection_rules': [{'type': 'required_reviewers', 'reviewers': [{'type': 'User', 'reviewer': {'login': 'peiman'}}]},
                                                 {'type': 'branch_policy'}],
                            'deployment_branch_policy': {'protected_branches': False, 'custom_branch_policies': True}}
        self.branch_policies = [{'name': 'main', 'type': 'branch'}]
        self.runs = {RUN: {'id': RUN, 'name': 'Release publish', 'path': '.github/workflows/release-publish.yml',
                           'event': 'workflow_dispatch', 'head_branch': 'main', 'head_sha': 'e' * 40,
                           'status': 'completed', 'conclusion': 'success', 'run_attempt': 1,
                           'created_at': stamp(0.1), 'repository': {'full_name': SOURCE}}}
        self.zips = {900: zip_of(bundle_files())}
        # The source repository's rulesets as a Metadata-read token sees them (no bypass actors).
        self.source_rulesets = [{'id': 31, 'name': 'no tag named main', 'target': 'tag', 'enforcement': 'active',
                                 'source_type': 'Repository', 'source': SOURCE,
                                 'conditions': {'ref_name': {'include': ['refs/tags/main'], 'exclude': []}},
                                 'rules': [{'type': 'creation'}, {'type': 'update'}, {'type': 'deletion'}],
                                 'created_at': stamp(40), 'updated_at': stamp(30)}]
        self.run_artifacts = {RUN: [self.artifact(900)]}
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _body(self):
                length = int(self.headers.get('Content-Length') or 0)
                return self.rfile.read(length) if length else b''

            def _send(self, status, payload=None, raw=None, headers=None):
                data = raw if raw is not None else (json.dumps(payload).encode() if payload is not None else b'')
                self.send_response(status)
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                if raw is None:
                    self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _route(self, method):
                url = urlparse(self.path)
                body = self._body()
                fake.requests.append((method, url.path, dict(self.headers), body))
                token = 'reader-token' if url.path.startswith(f'/repos/{SOURCE}/') else 'fake-token'
                if not url.path.startswith('/s3/') and self.headers.get('Authorization') != f'Bearer {token}':
                    return self._send(401, {'message': 'Bad credentials ' + SECRET})
                return fake.handle(self, method, url, body)

            def do_GET(self):
                self._route('GET')

            def do_POST(self):
                self._route('POST')

            def do_PATCH(self):
                self._route('PATCH')

            def do_DELETE(self):
                self._route('DELETE')

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def artifact(self, artifact_id, run=RUN, **change):
        return dict({'id': artifact_id, 'name': 'release-publish-bundle', 'expired': False,
                     'workflow_run': {'id': run, 'head_branch': 'main', 'head_sha': 'e' * 40}}, **change)

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def release_json(self, release):
        return dict(release, assets=[self.asset_json(a) for a in self.assets.values() if a['release'] == release['id']],
                    upload_url=f"{self.base}/uploads/repos/{REPO}/releases/{release['id']}/assets{{?name,label}}")

    def asset_json(self, asset):
        return {'id': asset['id'], 'name': asset['name'], 'size': len(asset['data']), 'state': 'uploaded',
                'url': f"{self.base}/repos/{REPO}/releases/assets/{asset['id']}"}

    def add_release(self, tag, draft):
        self.next_id += 1
        self.releases[self.next_id] = {'id': self.next_id, 'tag_name': tag, 'draft': draft, 'prerelease': '-' in tag,
                                       'name': tag, 'body': '', 'html_url': f'https://example.invalid/{tag}'}
        return self.releases[self.next_id]

    def handle_source(self, handler, method, path):
        if 'source-403' in self.faults:
            return handler._send(403, {'message': 'Resource not accessible ' + SECRET})
        if 'source-500' in self.faults:
            return handler._send(500, raw=('<html>' + SECRET + '</html>').encode())
        if 'source-garbage' in self.faults:
            return handler._send(200, raw=('not json ' + SECRET).encode())
        if 'source-drop' in self.faults:
            handler.close_connection = True
            handler.connection.shutdown(2)
            return None
        runs = f'/repos/{SOURCE}/actions/runs/'
        if method == 'GET' and path == f'/repos/{SOURCE}/rulesets':
            return handler._send(200, [{k: r[k] for k in ('id', 'name', 'target', 'enforcement', 'source_type', 'source')}
                                       for r in self.source_rulesets])
        if method == 'GET' and path.startswith(f'/repos/{SOURCE}/rulesets/'):
            found = [r for r in self.source_rulesets if r['id'] == int(path.split('/')[-1])]
            return handler._send(200, found[0]) if found else handler._send(404, {'message': 'Not Found'})
        if method == 'GET' and path.startswith(runs):
            parts = path[len(runs):].split('/')
            run = int(parts[0])
            if run not in self.runs:
                return handler._send(404, {'message': 'Not Found ' + SECRET})
            if parts[1:] == ['artifacts']:
                listed = self.run_artifacts.get(run, [])
                return handler._send(200, {'total_count': len(listed), 'artifacts': listed})
            return handler._send(200, self.runs[run])
        zips = f'/repos/{SOURCE}/actions/artifacts/'
        if method == 'GET' and path.startswith(zips) and path.endswith('/zip'):
            artifact = int(path[len(zips):].split('/')[0])
            if artifact in self.gone:
                return handler._send(410, {'message': 'Artifact has expired ' + SECRET})
            if artifact not in self.zips:
                return handler._send(404, {'message': 'Not Found'})
            # Like GitHub: a redirect to storage, which must not receive the token.
            return handler._send(302, headers={'Location': f'{self.base}/s3/artifact/{artifact}'}, raw=b'')
        return handler._send(404, {'message': f'no fake route {method} {path}'})

    def handle(self, handler, method, url, body):
        path = url.path
        if path.startswith(f'/repos/{SOURCE}/'):
            return self.handle_source(handler, method, path)
        prefix = f'/repos/{REPO}/releases'
        if path.startswith(f'/repos/{REPO}/environments/') and 'environment-403' in self.faults:
            return handler._send(403, {'message': 'Resource not accessible by integration'})
        if method == 'GET' and path == f'/repos/{REPO}/environments/release':
            return handler._send(200, self.environment) if self.environment else handler._send(404, {'message': 'Not Found'})
        if method == 'GET' and path == f'/repos/{REPO}/environments/release/deployment-branch-policies':
            return handler._send(200, {'total_count': len(self.branch_policies), 'branch_policies': self.branch_policies})
        if method == 'DELETE' and path.startswith(prefix + '/'):
            del self.releases[int(path.split('/')[-1])]
            return handler._send(204, raw=b'')
        if method == 'GET' and path.startswith(prefix + '/tags/'):
            tag = path[len(prefix + '/tags/'):]
            found = [r for r in self.releases.values() if r['tag_name'] == tag and not r['draft']]
            return handler._send(200, self.release_json(found[0])) if found else handler._send(404, {'message': 'Not Found'})
        if method == 'GET' and path == prefix:
            page = int(parse_qs(url.query).get('page', ['1'])[0])
            listed = [self.release_json(r) for r in self.releases.values()]
            return handler._send(200, listed if page == 1 else [])
        if method == 'POST' and path.startswith(f'/repos/{REPO}/git/'):
            self.tags_created.append(json.loads(body).get('ref'))
            return handler._send(201, {})
        if method == 'POST' and path == prefix:
            data = json.loads(body)
            if not data.get('draft', False):
                self.tags_created.append(data['tag_name'])
            release = self.add_release(data['tag_name'], data.get('draft', False))
            release.update(name=data.get('name'), body=data.get('body'), prerelease=data.get('prerelease', False))
            return handler._send(201, self.release_json(release))
        if path.startswith(f'/uploads{prefix}/') and method == 'POST':
            if 'upload-fails' in self.faults:
                return handler._send(502, {'message': 'Bad Gateway'})
            release = int(path.split('/')[-2])
            name = parse_qs(url.query)['name'][0]
            self.next_id += 1
            uploaded = self.assets[self.next_id] = {'id': self.next_id, 'release': release, 'name': name, 'data': body}
            if 'extra-asset' in self.faults and len([a for a in self.assets.values() if a['release'] == release]) == 1:
                self.next_id += 1
                self.assets[self.next_id] = {'id': self.next_id, 'release': release, 'name': 'unexpected.txt', 'data': b'x'}
            return handler._send(201, self.asset_json(uploaded))
        if method == 'GET' and path.startswith(prefix + '/assets/'):
            asset = self.assets[int(path.split('/')[-1])]
            if handler.headers.get('Accept') != 'application/octet-stream':
                return handler._send(200, self.asset_json(asset))
            return handler._send(302, headers={'Location': f"{self.base}/s3/asset/{asset['id']}"}, raw=b'')
        if method == 'GET' and path.startswith('/s3/'):
            if handler.headers.get('Authorization'):
                return handler._send(400, {'message': 'Only one auth mechanism allowed'})
            kind, ident = path.split('/')[2], int(path.split('/')[3])
            if kind == 'artifact':
                return handler._send(200, raw=self.zips[ident], headers={'Content-Type': 'application/zip'})
            data = self.assets[ident]['data']
            if 'corrupt-download' in self.faults:
                data = data[:-1] + bytes([data[-1] ^ 1])
            return handler._send(200, raw=data, headers={'Content-Type': 'application/octet-stream'})
        if method == 'GET' and path.startswith(prefix + '/') and path.endswith('/assets'):
            release = int(path.split('/')[-2])
            if 'swap-asset' in self.faults:
                self.faults.discard('swap-asset')
                old = next(a for a in self.assets.values() if a['release'] == release)
                del self.assets[old['id']]
                self.next_id += 1
                self.assets[self.next_id] = dict(old, id=self.next_id)
            return handler._send(200, [self.asset_json(a) for a in self.assets.values() if a['release'] == release])
        if method == 'PATCH' and path.startswith(prefix + '/'):
            release = self.releases[int(path.split('/')[-1])]
            if 'patch-fails' in self.faults:
                return handler._send(500, {'message': 'Server Error'})
            if 'patch-ignored' in self.faults:
                return handler._send(200, self.release_json(release))
            release.update(json.loads(body))
            if release['draft'] is False:
                self.tags_created.append(release['tag_name'])
            if 'patch-applied-then-drop' in self.faults:
                handler.close_connection = True
                handler.connection.shutdown(2)
                return None
            if 'patch-applied-then-garbage' in self.faults:
                return handler._send(200, raw=b'<html>gateway</html>')
            if 'patch-applied-then-slow' in self.faults:
                import time
                time.sleep(2)
                return handler._send(200, self.release_json(release))
            if 'publish-wrong-tag' in self.faults:
                release['tag_name'] = 'v9.9.9'
            if 'publish-assets-changed' in self.faults:
                self.next_id += 1
                self.assets[self.next_id] = {'id': self.next_id, 'release': release['id'], 'name': 'late.txt', 'data': b'x'}
            if 'patch-applied-then-timeout' in self.faults:
                return handler._send(504, {'message': 'Gateway Timeout'})
            return handler._send(200, self.release_json(release))
        if method == 'GET' and path.startswith(prefix + '/'):
            if 'readback-fails' in self.faults and any(m == 'PATCH' for m, _, _, _ in self.requests):
                return handler._send(500, {'message': 'Server Error'})
            release = self.releases.get(int(path.split('/')[-1]))
            return handler._send(200, self.release_json(release)) if release else handler._send(404, {'message': 'Not Found'})
        return handler._send(404, {'message': f'no fake route {method} {path}'})

    def writes(self):
        return [(method, path) for method, path, _, _ in self.requests if method != 'GET']


def call(module, argv, fake, env=None):
    out, err = io.StringIO(), io.StringIO()
    base = {'GITHUB_REF': 'refs/heads/main', 'PUBLISH_TOKEN': 'fake-token', 'PREFLIGHT_TOKEN': 'fake-token',
            'READER_TOKEN': 'reader-token', 'RELEASE_REQUIRED_REVIEWER': 'peiman'}
    with redirect_stdout(out), redirect_stderr(err):
        status = module.main([argv[0], '--api', fake.base, *argv[1:]], env={**base, **(env or {})})
    return status, out.getvalue(), err.getvalue()


class Case(unittest.TestCase):
    module = publisher

    def setUp(self):
        self.fake = FakeGitHub()
        self.tmp = tempfile.TemporaryDirectory(prefix='publish-')
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.fake.close()
        self.tmp.cleanup()

    def run_cmd(self, *argv, env=None):
        return call(self.module, list(argv), self.fake, env)

    def fetch(self, run=RUN, tag=TAG, mode='publish', out=None, env=None):
        out = out or self.root / 'bundle'
        return self.run_cmd('fetch', '--run-id', str(run), '--tag', tag, '--mode', mode, '--out', str(out), env=env)

    def write_bundle(self, files, where=None):
        where = where or self.root / 'bundle'
        for name, data in files.items():
            (where / name).parent.mkdir(parents=True, exist_ok=True)
            (where / name).write_bytes(data)
        return where

    def publish(self, tag=TAG, mode='publish', run=RUN, files=None, env=None):
        bundle = self.root / 'bundle'
        if not bundle.exists():
            self.write_bundle(files or bundle_files(tag=tag, candidate=tag, mode=mode, run=run))
        return self.run_cmd('publish', '--repo', REPO, '--tag', tag, '--run-id', str(run), '--mode', mode,
                            '--bundle', str(bundle), '--receipt', str(self.root / 'receipt.json'), env=env)

    def published(self):
        return [r for r in self.fake.releases.values() if not r['draft']]

    def receipt(self):
        return json.loads((self.root / 'receipt.json').read_text())

    def assertCode(self, result, code):
        status, out, err = result
        self.assertEqual(status, 1, out + err)
        self.assertTrue(err.startswith('REFUSED ') and f': {code}' in err, f'expected {code}, got {err!r}')


class FetchTests(Case):
    def test_fetches_and_verifies_the_prepare_runs_bundle(self):
        status, out, err = self.fetch()
        self.assertEqual(status, 0, err)
        self.assertTrue((self.root / 'bundle/assets/checksums.txt').is_file())
        self.assertIn('VERIFIED', out)
        # Only the reader token's repository was read, and nothing was written.
        self.assertEqual(self.fake.writes(), [])
        self.assertTrue(any(p.startswith('/s3/artifact/') for _, p, _, _ in self.fake.requests), 'the zip came from storage')

    def test_the_run_must_be_a_successful_main_dispatch_of_the_prepare_workflow(self):
        for change in ({'path': '.github/workflows/ci.yml'}, {'event': 'push'}, {'head_branch': 'feature'},
                       {'status': 'in_progress'}, {'conclusion': 'failure'}, {'conclusion': None},
                       {'repository': {'full_name': 'someone/daana-cli'}}, {'id': RUN + 1}):
            with self.subTest(change=change):
                original = dict(self.fake.runs[RUN])
                self.fake.runs[RUN].update(change)
                self.assertCode(self.fetch(), 'SOURCE_RUN_NOT_PREPARE')
                self.fake.runs[RUN] = original
        self.assertCode(self.fetch(run=RUN + 5), 'SOURCE_RUN_NOT_FOUND')

    def test_exactly_one_unexpired_bundle_artifact_of_that_run(self):
        good = self.fake.artifact(900)
        cases = {
            'missing': [],
            'duplicate': [good, self.fake.artifact(901)],
            'another run': [self.fake.artifact(900, run=RUN + 1)],
            'another branch': [dict(good, workflow_run=dict(good['workflow_run'], head_branch='feature'))],
            'another commit': [dict(good, workflow_run=dict(good['workflow_run'], head_sha='f' * 40))],
        }
        for name, listed in cases.items():
            with self.subTest(name):
                self.fake.run_artifacts[RUN] = listed
                shutil.rmtree(self.root / 'bundle', ignore_errors=True)
                self.assertCode(self.fetch(), 'BUNDLE_ARTIFACT_NOT_ONE')
                self.assertFalse(any('/actions/artifacts/' in p for _, p, _, _ in self.fake.requests), name + ': nothing downloaded')
        # Other artifacts of the run are ignored.
        self.fake.run_artifacts[RUN] = [good, dict(self.fake.artifact(950), name='something-else')]
        self.assertEqual(self.fetch()[0], 0)

    def test_the_manifest_binds_the_tag_and_the_run(self):
        self.assertCode(self.fetch(tag='v1.0.0-rc.2'), 'MANIFEST_TAG_MISMATCH')
        self.fake.zips[900] = zip_of(bundle_files(run=RUN + 1))
        shutil.rmtree(self.root / 'bundle', ignore_errors=True)
        self.assertCode(self.fetch(), 'MANIFEST_RUN_MISMATCH')

    def test_a_tampered_or_incomplete_bundle_is_refused(self):
        def change(name, data):
            return lambda files, manifest: files.__setitem__(name, data)

        def drop_from_manifest(name):
            return lambda files, manifest: manifest.update(files=[f for f in manifest['files'] if f['name'] != name])

        cases = {
            'changed archive bytes': (change('assets/' + ARCHIVES['checksums.txt'][0], b'tampered'), 'BUNDLE_FILE_MISMATCH'),
            'extra file': (lambda files, manifest: files.__setitem__('assets/extra.txt', b'x'), 'BUNDLE_FILES_DIFFER'),
            'missing file': (lambda files, manifest: files.pop('release-body.md'), 'BUNDLE_FILES_DIFFER'),
            'unlisted file': (drop_from_manifest('assets/checksums_darwin.txt'), 'BUNDLE_FILES_DIFFER'),
            'wrong size': (lambda files, manifest: manifest['files'][0].update(size=1), 'BUNDLE_FILE_MISMATCH'),
            'wrong schema': (lambda files, manifest: manifest.update(schema_version=2), 'MANIFEST_INVALID'),
            'bad commit': (lambda files, manifest: manifest.update(tag_commit='main'), 'MANIFEST_INVALID'),
            'release tag not from candidate': (lambda files, manifest: manifest.update(candidate_tag='v0.9.0'), 'MANIFEST_INVALID'),
            'tag run id zero': (lambda files, manifest: manifest.update(tag_run_id=0), 'MANIFEST_INVALID'),
            'tag run id text': (lambda files, manifest: manifest.update(tag_run_id='4242'), 'MANIFEST_INVALID'),
            'tag run attempt a boolean': (lambda files, manifest: manifest.update(tag_run_attempt=True), 'MANIFEST_INVALID'),
            'tag run attempt missing': (lambda files, manifest: manifest.pop('tag_run_attempt'), 'MANIFEST_INVALID'),
        }
        for name, (mutate, code) in cases.items():
            with self.subTest(name):
                files = bundle_files()
                manifest = json.loads(files.pop('bundle-manifest.json'))
                mutate(files, manifest)
                files['bundle-manifest.json'] = json.dumps(manifest).encode()
                self.fake.zips[900] = zip_of(files)
                shutil.rmtree(self.root / 'bundle', ignore_errors=True)
                self.assertCode(self.fetch(), code)

    def test_the_archives_must_match_both_checksum_files(self):
        def unlisted(files, manifest):
            files['assets/daana-cli_1.0.0-rc.1_linux_386.tar.gz'] = b'x'

        def wrong_line(files, manifest):
            files['assets/checksums_darwin.txt'] = f'{"0" * 64}  {ARCHIVES["checksums_darwin.txt"][0]}\n'.encode()

        def no_darwin(files, manifest):
            del files['assets/checksums_darwin.txt']

        for name, mutate in (('unlisted archive', unlisted), ('wrong checksum', wrong_line), ('missing checksum file', no_darwin)):
            with self.subTest(name):
                # Manifest regenerated over the changed files, so only the checksum check can refuse.
                self.fake.zips[900] = zip_of(bundle_files(mutate=lambda files, manifest: (mutate(files, manifest), manifest.update(
                    files=[{'name': n, 'size': len(d), 'sha256': sha(d)} for n, d in sorted(files.items())]))))
                shutil.rmtree(self.root / 'bundle', ignore_errors=True)
                self.assertCode(self.fetch(), 'CHECKSUMS_MISMATCH')

    def test_publish_mode_refuses_a_rehearsal_bundle_or_tag_and_needs_the_record(self):
        self.fake.zips[900] = zip_of(bundle_files(mode='rehearse', record=False))
        self.assertCode(self.fetch(), 'MANIFEST_NOT_FOR_PUBLISH')
        shutil.rmtree(self.root / 'bundle')
        self.assertEqual(self.fetch(mode='rehearse')[0], 0, 'a rehearse bundle may be drafted')
        shutil.rmtree(self.root / 'bundle')
        self.fake.zips[900] = zip_of(bundle_files(record=False))
        self.assertCode(self.fetch(), 'MANIFEST_NOT_FOR_PUBLISH')
        shutil.rmtree(self.root / 'bundle')
        rehearsal = 'v1.0.0-rc.1-rehearsal.1'
        self.fake.zips[900] = zip_of(bundle_files(tag=rehearsal, candidate=rehearsal))
        self.assertCode(self.fetch(tag=rehearsal), 'REHEARSAL_TAG')
        shutil.rmtree(self.root / 'bundle', ignore_errors=True)
        self.assertEqual(self.fetch(tag=rehearsal, mode='rehearse')[0], 0)

    def test_an_unsafe_zip_is_refused(self):
        cases = {
            'traversal': lambda archive: archive.writestr('../evil', b'x'),
            'absolute': lambda archive: archive.writestr('/etc/evil', b'x'),
            'nested': lambda archive: archive.writestr('assets/deeper/evil', b'x'),
            'hidden': lambda archive: archive.writestr('.evil', b'x'),
        }
        for name, extra in cases.items():
            with self.subTest(name):
                self.fake.zips[900] = zip_of(bundle_files(), extra)
                shutil.rmtree(self.root / 'bundle', ignore_errors=True)
                self.assertCode(self.fetch(), 'BUNDLE_ZIP_UNSAFE')
                self.assertFalse((self.root / 'evil').exists())

        def symlink(archive):
            info = zipfile.ZipInfo('assets/link')
            info.external_attr = (0o120777 << 16)
            archive.writestr(info, 'checksums.txt')
        self.fake.zips[900] = zip_of(bundle_files(), symlink)
        shutil.rmtree(self.root / 'bundle', ignore_errors=True)
        self.assertCode(self.fetch(), 'BUNDLE_ZIP_UNSAFE')
        self.fake.zips[900] = b'not a zip'
        shutil.rmtree(self.root / 'bundle', ignore_errors=True)
        self.assertCode(self.fetch(), 'BUNDLE_ZIP_UNSAFE')

    def test_an_oversized_zip_is_refused(self):
        self.addCleanup(setattr, self.module, 'MAX_BUNDLE_BYTES', self.module.MAX_BUNDLE_BYTES)
        self.module.MAX_BUNDLE_BYTES = 1000
        self.assertCode(self.fetch(), 'BUNDLE_TOO_LARGE')

    def test_the_output_directory_must_be_new(self):
        (self.root / 'bundle').mkdir()
        (self.root / 'bundle/old').write_text('x')
        self.assertCode(self.fetch(), 'OUTPUT_NOT_EMPTY')

    def test_inputs_are_checked_before_any_request(self):
        for argv in (('--run-id', '0'), ('--run-id', '12; rm'), ('--tag', 'main'), ('--mode', 'publish-now')):
            with self.subTest(argv=argv):
                args = dict(zip(('--run-id', '--tag', '--mode'), (str(RUN), TAG, 'publish')))
                args[argv[0]] = argv[1]
                self.assertCode(self.run_cmd('fetch', *sum(args.items(), ()), '--out', str(self.root / 'b')), 'INPUT_INVALID')
        self.assertEqual(self.fake.requests, [])

    def test_only_a_dispatch_from_main_may_fetch(self):
        self.assertCode(self.fetch(env={'GITHUB_REF': 'refs/heads/feature'}), 'NOT_MAIN')
        self.assertEqual(self.fake.requests, [])

    def test_the_storage_redirect_never_receives_the_reader_token(self):
        self.assertEqual(self.fetch()[0], 0)
        storage = [h for m, p, h, _ in self.fake.requests if p.startswith('/s3/')]
        self.assertTrue(storage)
        self.assertFalse(any('Authorization' in h for h in storage))


class SourceBindingTests(Case):
    """The manifest itself must name the source repository and its prepare workflow,
    checked apart from the run API (which can only vouch for the run, not the bytes)."""

    CASES = {
        'another repository': lambda m: m.update(source_repository='someone/daana-cli'),
        'another workflow': lambda m: m.update(source_workflow='.github/workflows/ci.yml'),
        'no workflow': lambda m: m.pop('source_workflow'),
        'no repository': lambda m: m.pop('source_repository'),
    }

    def test_fetch_refuses_a_manifest_from_another_source_even_when_the_run_checks_out(self):
        for name, change in self.CASES.items():
            with self.subTest(name):
                self.fake.zips[900] = zip_of(bundle_files(mutate=lambda files, manifest: change(manifest)))
                shutil.rmtree(self.root / 'bundle', ignore_errors=True)
                self.assertCode(self.fetch(), 'MANIFEST_SOURCE_MISMATCH')

    def test_publish_refuses_a_manifest_from_another_source_before_any_write(self):
        for name, change in self.CASES.items():
            with self.subTest(name):
                shutil.rmtree(self.root / 'bundle', ignore_errors=True)
                self.assertCode(self.publish(files=bundle_files(mutate=lambda files, manifest: change(manifest))), 'MANIFEST_SOURCE_MISMATCH')
                self.assertEqual(self.fake.writes(), [])


class SourceTagMainTests(Case):
    """A run dispatched on a daana-cli TAG named `main` reports head_branch `main`, exactly
    like a run on the branch, and the Reader (Actions: read, Metadata: read) cannot tell
    them apart. So the source must block a tag named main: an active tag ruleset that
    covers refs/tags/main with creation and update rules (readable with Metadata: read)."""

    def refused(self, change):
        self.fake.close()
        self.fake = FakeGitHub()
        change(self.fake)
        shutil.rmtree(self.root / 'bundle', ignore_errors=True)
        result = self.fetch()
        self.assertCode(result, 'SOURCE_TAG_MAIN_UNPROTECTED')
        self.assertFalse(any('/artifacts' in p for _, p, _, _ in self.fake.requests), 'refused before the bundle is touched')

    def test_the_source_must_block_a_tag_named_main(self):
        rule = lambda f: f.source_rulesets[0]
        cases = {
            'no ruleset': lambda f: f.source_rulesets.clear(),
            'disabled': lambda f: rule(f).update(enforcement='disabled'),
            'evaluate only': lambda f: rule(f).update(enforcement='evaluate'),
            'a branch ruleset': lambda f: rule(f).update(target='branch'),
            'covers only v*': lambda f: rule(f).update(conditions={'ref_name': {'include': ['refs/tags/v*'], 'exclude': []}}),
            'main excluded': lambda f: rule(f).update(conditions={'ref_name': {'include': ['~ALL'], 'exclude': ['refs/tags/main']}}),
            'all excluded': lambda f: rule(f).update(conditions={'ref_name': {'include': ['~ALL'], 'exclude': ['~ALL']}}),
            'creation allowed': lambda f: rule(f).update(rules=[{'type': 'update'}, {'type': 'deletion'}]),
            'update allowed': lambda f: rule(f).update(rules=[{'type': 'creation'}, {'type': 'deletion'}]),
            # GitHub's `*` does not cross `/`; Python's fnmatch does. Only known covering forms count.
            'include refs/*': lambda f: rule(f).update(conditions={'ref_name': {'include': ['refs/*'], 'exclude': []}}),
            'include refs/tags/m*': lambda f: rule(f).update(conditions={'ref_name': {'include': ['refs/tags/m*'], 'exclude': []}}),
            'include short main': lambda f: rule(f).update(conditions={'ref_name': {'include': ['main'], 'exclude': []}}),
            'exclude refs/*': lambda f: rule(f).update(conditions={'ref_name': {'include': ['~ALL'], 'exclude': ['refs/*']}}),
            'exclude a brace set': lambda f: rule(f).update(conditions={'ref_name': {'include': ['~ALL'], 'exclude': ['refs/tags/{main,x}']}}),
            'exclude short main': lambda f: rule(f).update(conditions={'ref_name': {'include': ['~ALL'], 'exclude': ['main']}}),
            'exclude m?in': lambda f: rule(f).update(conditions={'ref_name': {'include': ['refs/tags/**'], 'exclude': ['refs/tags/m?in']}}),
        }
        for name, change in cases.items():
            with self.subTest(name):
                self.refused(change)

    def test_broader_tag_rulesets_also_count(self):
        for name, conditions, source in (('all but v*', {'include': ['~ALL'], 'exclude': ['refs/tags/v*']}, 'Repository'),
                                         ('every tag', {'include': ['refs/tags/*'], 'exclude': []}, 'Repository'),
                                         ('every tag, double star', {'include': ['refs/tags/**'], 'exclude': ['refs/tags/v*', 'refs/heads/main']}, 'Repository'),
                                         ('from the organization', {'include': ['~ALL'], 'exclude': ['refs/tags/v*']}, 'Organization')):
            with self.subTest(name):
                self.fake.close()
                self.fake = FakeGitHub()
                self.fake.source_rulesets[0].update(conditions={'ref_name': conditions}, source_type=source)
                # One unrelated ruleset first: only a covering one is needed.
                self.fake.source_rulesets.insert(0, {'id': 30, 'name': 'branches', 'target': 'branch', 'enforcement': 'active',
                                                     'source_type': 'Repository', 'source': SOURCE, 'conditions': {}, 'rules': []})
                shutil.rmtree(self.root / 'bundle', ignore_errors=True)
                status, out, err = self.fetch()
                self.assertEqual(status, 0, err)

    def test_the_ruleset_must_predate_the_prepare_run(self):
        # A tag `main` made before the ruleset (or while it was off) survives Restrict creations;
        # so the covering ruleset must be unchanged since before the run was created.
        rule = lambda f: f.source_rulesets[0]
        for name, change in (('updated after the run', lambda f: rule(f).update(updated_at=stamp(0.01))),
                             ('no updated_at', lambda f: rule(f).pop('updated_at')),
                             ('unreadable updated_at', lambda f: rule(f).update(updated_at='last week'))):
            with self.subTest(name):
                self.refused(change)
        for name, value in (('Z form', stamp(1)), ('offset and fraction', (datetime.now(timezone.utc) - timedelta(days=1))
                                                   .astimezone(timezone(timedelta(hours=-5))).isoformat(timespec='milliseconds'))):
            with self.subTest(name):
                self.fake.close()
                self.fake = FakeGitHub()
                self.fake.source_rulesets[0]['updated_at'] = value
                shutil.rmtree(self.root / 'bundle', ignore_errors=True)
                status, out, err = self.fetch()
                self.assertEqual(status, 0, err)

    def test_an_unreadable_ruleset_list_is_the_fixed_private_code(self):
        self.fake.close()
        self.fake = FakeGitHub()
        self.fake.source_rulesets[0]['id'] = 99  # listed, but its detail answers 404
        original = self.fake.handle_source

        def missing_detail(handler, method, path):
            if path.endswith('/rulesets/99'):
                return handler._send(404, {'message': 'Not Found ' + SECRET})
            return original(handler, method, path)
        self.fake.handle_source = missing_detail
        status, out, err = self.fetch()
        self.assertEqual((status, out), (1, ''))
        self.assertEqual(err, 'REFUSED fetch: SOURCE_TAG_MAIN_UNPROTECTED\n')


class ExpiryTests(Case):
    """A prepare run older than the bundle's 7 days refuses with a fixed code, never a raw 404."""

    def test_a_run_older_than_seven_days_is_refused_before_any_download(self):
        self.fake.runs[RUN]['created_at'] = stamp(7.01)
        self.assertCode(self.fetch(), 'BUNDLE_EXPIRED')
        self.assertFalse(any('/artifacts' in p for _, p, _, _ in self.fake.requests), 'refused before listing or downloading')
        self.fake.runs[RUN]['created_at'] = stamp(6.9)
        self.assertEqual(self.fetch()[0], 0)

    def test_an_unreadable_run_time_is_refused(self):
        for value in (None, 'yesterday', stamp(-1)):
            with self.subTest(value=value):
                self.fake.runs[RUN]['created_at'] = value
                shutil.rmtree(self.root / 'bundle', ignore_errors=True)
                self.assertCode(self.fetch(), 'SOURCE_RUN_NOT_PREPARE')

    def test_an_expired_or_vanished_bundle_is_refused_with_the_fixed_code(self):
        good = self.fake.artifact(900)
        for name, change in (('expired flag', lambda f: f.run_artifacts.__setitem__(RUN, [dict(good, expired=True)])),
                             ('expires_at passed', lambda f: f.run_artifacts.__setitem__(RUN, [dict(good, expires_at=stamp(0.01))])),
                             ('zip gone (410)', lambda f: f.gone.add(900)),
                             ('zip missing (404)', lambda f: f.zips.pop(900))):
            with self.subTest(name):
                self.fake.close()
                self.fake = FakeGitHub()
                change(self.fake)
                shutil.rmtree(self.root / 'bundle', ignore_errors=True)
                status, out, err = self.fetch()
                self.assertEqual((status, out, err), (1, '', f'REFUSED fetch: BUNDLE_EXPIRED run={RUN}\n'))


class PublicLogTests(Case):
    """Actions logs here are public: a private-repository failure prints only a fixed code."""

    def test_a_failing_private_api_call_prints_only_the_fixed_code(self):
        for fault in ('source-403', 'source-500', 'source-garbage', 'source-drop'):
            with self.subTest(fault=fault):
                self.fake.faults = {fault}
                status, out, err = self.fetch()
                self.assertEqual(status, 1)
                self.assertEqual((out, err), ('', 'REFUSED fetch: SOURCE_API_FAILED\n'), fault)
                self.assertNotIn(SECRET, out + err)

    def test_a_bad_reader_token_prints_only_the_fixed_code(self):
        status, out, err = self.fetch(env={'READER_TOKEN': 'wrong'})
        self.assertEqual((status, out, err), (1, '', 'REFUSED fetch: SOURCE_API_FAILED\n'))

    def test_an_unexpected_error_prints_only_internal_error(self):
        original = self.module.fetch

        def boom(*args, **kwargs):
            raise RuntimeError('detail from a private body ' + SECRET)
        self.module.fetch = boom
        try:
            status, out, err = self.fetch()
        finally:
            self.module.fetch = original
        self.assertEqual((status, out, err), (1, '', 'REFUSED fetch: INTERNAL_ERROR\n'))

    def test_details_are_only_ids_tags_names_and_digests(self):
        refused = self.module.Refused('CODE', name='daana-cli_1.0.0_linux_amd64.tar.gz', id=12, text='a body: ' + SECRET)
        line = refused.line('fetch')
        self.assertNotIn(SECRET, line)
        self.assertIn('name=daana-cli_1.0.0_linux_amd64.tar.gz', line)
        self.assertIn('id=12', line)


class PublishTests(Case):
    def test_publishes_the_exact_bytes_after_a_digest_checked_draft(self):
        status, out, err = self.publish()
        self.assertEqual(status, 0, err)
        [release] = self.published()
        self.assertEqual((release['tag_name'], release['prerelease']), (TAG, True))
        self.assertEqual(sorted(a['name'] for a in self.fake.assets.values()), ALL_ASSETS)
        for asset in self.fake.assets.values():
            self.assertEqual(asset['data'], (self.root / 'bundle/assets' / asset['name']).read_bytes())
        writes = self.fake.writes()
        self.assertEqual(writes[0], ('POST', f'/repos/{REPO}/releases'))
        self.assertEqual(writes[-1][0], 'PATCH')
        self.assertTrue(all(method == 'POST' and '/uploads/' in path for method, path in writes[1:-1]))
        create = json.loads([b for m, p, _, b in self.fake.requests if m == 'POST' and p == f'/repos/{REPO}/releases'][0])
        self.assertTrue(create['draft'])
        self.assertIn('Real notes.', create['body'])
        receipt = self.receipt()
        self.assertEqual((receipt['tag'], receipt['published'], receipt['prepare_run_id']), (TAG, True, RUN))
        self.assertEqual(len(receipt['assets']), 5)
        self.assertTrue(any(p.startswith('/s3/asset/') for _, p, _, _ in self.fake.requests))
        # Never the record or the body as an asset.
        self.assertNotIn('qualification.json', [a['name'] for a in self.fake.assets.values()])

    def test_a_final_release_is_not_a_prerelease(self):
        status, out, err = self.publish(tag='v1.0.0', files=bundle_files(tag='v1.0.0', candidate=TAG))
        self.assertEqual(status, 0, err)
        [release] = self.published()
        self.assertEqual((release['tag_name'], release['prerelease']), ('v1.0.0', False))

    def test_publish_re_verifies_the_bundle_before_any_write(self):
        files = bundle_files()
        files['assets/' + ARCHIVES['checksums.txt'][0]] = b'changed after fetch'
        self.assertCode(self.publish(files=files), 'BUNDLE_FILE_MISMATCH')
        self.assertEqual(self.fake.writes(), [])

    def test_a_rehearse_run_never_creates_a_tag_or_sends_draft_false(self):
        for tag, candidate, mode in ((TAG, TAG, 'rehearse'), ('v1.0.0-rc.1-rehearsal.1', 'v1.0.0-rc.1-rehearsal.1', 'rehearse')):
            with self.subTest(tag=tag):
                self.fake.close()
                self.fake = FakeGitHub()
                shutil.rmtree(self.root / 'bundle', ignore_errors=True)
                (self.root / 'receipt.json').unlink(missing_ok=True)
                status, out, err = self.publish(tag=tag, mode='rehearse', files=bundle_files(tag=tag, candidate=candidate, mode=mode, record=False))
                self.assertEqual(status, 0, err)
                self.assertEqual(self.fake.tags_created, [], 'no tag comes into being')
                creates = [json.loads(b) for m, p, _, b in self.fake.requests if m == 'POST' and p == f'/repos/{REPO}/releases']
                self.assertEqual([c['draft'] for c in creates], [True], 'the release is created as a draft')
                self.assertFalse(any(b'"draft": false' in b.replace(b' ', b' ') or b'"draft":false' in b for _, _, _, b in self.fake.requests),
                                 'draft=false is never sent')
                self.assertFalse(any(m == 'PATCH' for m, _ in self.fake.writes()))
                self.assertFalse(any('/git/' in p for _, p, _, _ in self.fake.requests), 'no git ref is read or created')
                self.assertEqual({p.split('?')[0].rsplit('/', 1)[0] if '/uploads/' in p else p for m, p in self.fake.writes()},
                                 {f'/repos/{REPO}/releases', f'/uploads/repos/{REPO}/releases/{self.receipt()["release_id"]}'},
                                 'the only writes are the draft and its uploads')
        # A publish, by contrast, does make the tag (when the draft is published).
        self.fake.close()
        self.fake = FakeGitHub()
        shutil.rmtree(self.root / 'bundle')
        (self.root / 'receipt.json').unlink()
        self.assertEqual(self.publish()[0], 0)
        self.assertEqual(self.fake.tags_created, [TAG])

    def test_a_rehearse_run_drafts_and_never_publishes_even_a_rehearsal(self):
        rehearsal = 'v1.0.0-rc.1-rehearsal.1'
        status, out, err = self.publish(tag=rehearsal, mode='rehearse',
                                        files=bundle_files(tag=rehearsal, candidate=rehearsal, mode='rehearse', record=False))
        self.assertEqual(status, 0, err)
        self.assertEqual(self.published(), [])
        self.assertEqual(len(self.fake.releases), 1)
        self.assertFalse(any(m == 'PATCH' for m, _ in self.fake.writes()), 'draft=false is never sent')
        self.assertEqual((self.receipt()['published'], self.receipt()['state']), (False, 'draft-verified'))
        self.assertIn('DRAFT', out)

    def test_a_rehearsal_tag_or_bundle_is_never_published(self):
        rehearsal = 'v1.0.0-rc.1-rehearsal.1'
        self.assertCode(self.publish(tag=rehearsal, files=bundle_files(tag=rehearsal, candidate=rehearsal)), 'REHEARSAL_TAG')
        self.assertEqual(self.fake.writes(), [])
        # A rehearsal candidate under a real tag is not a D10 pair at all.
        for tag in (TAG, 'v1.0.0'):
            shutil.rmtree(self.root / 'bundle')
            self.assertCode(self.publish(tag=tag, files=bundle_files(tag=tag, candidate=rehearsal)), 'MANIFEST_INVALID')
        self.assertEqual(self.fake.writes(), [])
        shutil.rmtree(self.root / 'bundle')
        self.assertCode(self.publish(files=bundle_files(mode='rehearse')), 'MANIFEST_NOT_FOR_PUBLISH')
        self.assertEqual(self.fake.writes(), [])

    def test_a_digest_mismatch_on_re_download_is_refused_and_nothing_is_published(self):
        self.fake.faults.add('corrupt-download')
        self.assertCode(self.publish(), 'ASSET_DIGEST_MISMATCH')
        self.assertEqual(self.published(), [])
        self.assertFalse(any(m == 'PATCH' for m, _ in self.fake.writes()))

    def test_an_extra_asset_on_the_draft_is_refused(self):
        self.fake.faults.add('extra-asset')
        self.assertCode(self.publish(), 'DRAFT_ASSETS_MISMATCH')
        self.assertEqual(self.published(), [])

    def test_a_swapped_asset_is_refused_by_its_id_before_any_download(self):
        self.fake.faults = {'swap-asset'}
        self.assertCode(self.publish(), 'DRAFT_ASSET_IDS_CHANGED')
        self.assertFalse(any(p.startswith('/s3/') or '/releases/assets/' in p for m, p, _, _ in self.fake.requests if m == 'GET'))
        self.assertEqual(self.published(), [])

    def test_an_existing_published_release_or_draft_is_refused(self):
        for draft in (False, True):
            with self.subTest(draft=draft):
                self.fake.releases.clear()
                self.fake.requests.clear()
                self.fake.add_release(TAG, draft)
                self.assertCode(self.publish(), 'RELEASE_EXISTS')
                self.assertEqual(self.fake.writes(), [])

    def test_the_draft_id_is_recorded_before_the_first_upload(self):
        self.fake.faults.add('upload-fails')
        self.assertCode(self.publish(), 'PUBLIC_API_FAILED')
        [draft] = self.fake.releases.values()
        receipt = self.receipt()
        self.assertEqual((receipt['release_id'], receipt['published'], receipt['state']), (draft['id'], False, 'draft-created'))
        self.assertEqual(self.published(), [])

    def test_an_unconfirmed_publish_leaves_the_receipt_unknown_never_unpublished(self):
        self.addCleanup(setattr, self.module, 'TIMEOUT', self.module.TIMEOUT)
        self.module.TIMEOUT = 1
        for fault in ('patch-applied-then-timeout', 'readback-fails', 'patch-fails', 'patch-applied-then-drop',
                      'patch-applied-then-garbage', 'patch-applied-then-slow', 'patch-ignored'):
            with self.subTest(fault=fault):
                self.fake.releases.clear()
                self.fake.assets.clear()
                self.fake.requests.clear()
                self.fake.faults = {fault}
                (self.root / 'receipt.json').unlink(missing_ok=True)
                self.assertCode(self.publish(), 'PUBLISH_OUTCOME_UNKNOWN')
                self.assertEqual((self.receipt()['state'], self.receipt()['published']), ('unknown', None), fault)

    def test_a_rehearse_draft_is_unmistakable_and_never_carries_a_real_tag(self):
        for tag, candidate, draft_tag in ((TAG, TAG, f'v1.0.0-rc.1-rehearsal.{RUN}'), ('v1.0.0', TAG, f'v1.0.0-rehearsal.{RUN}'),
                                          ('v1.0.0-rc.1-rehearsal.1', 'v1.0.0-rc.1-rehearsal.1', 'v1.0.0-rc.1-rehearsal.1')):
            with self.subTest(tag=tag):
                self.fake.close()
                self.fake = FakeGitHub()
                shutil.rmtree(self.root / 'bundle', ignore_errors=True)
                (self.root / 'receipt.json').unlink(missing_ok=True)
                status, out, err = self.publish(tag=tag, mode='rehearse', files=bundle_files(tag=tag, candidate=candidate))
                self.assertEqual(status, 0, err)
                [create] = [json.loads(b) for m, p, _, b in self.fake.requests if m == 'POST' and p == f'/repos/{REPO}/releases']
                self.assertEqual(create['tag_name'], draft_tag)
                self.assertTrue(self.module.is_rehearsal(create['tag_name']), 'a rehearse draft never carries a non-rehearsal tag')
                self.assertTrue(create['name'].startswith('[REHEARSAL \u2014 do not publish] '), create['name'])
                self.assertTrue(create['prerelease'] and create['draft'])
                self.assertEqual((self.receipt()['tag'], self.receipt()['release_tag']), (draft_tag, tag))
                self.assertIn(draft_tag, out)

    def test_a_publish_draft_carries_the_real_tag_and_name(self):
        self.assertEqual(self.publish()[0], 0)
        [create] = [json.loads(b) for m, p, _, b in self.fake.requests if m == 'POST' and p == f'/repos/{REPO}/releases']
        self.assertEqual((create['tag_name'], create['name']), (TAG, TAG))
        self.assertEqual((self.receipt()['tag'], self.receipt()['release_tag']), (TAG, TAG))

    def test_a_confirmed_but_mismatched_publish_records_the_real_outcome(self):
        for fault, code in (('publish-wrong-tag', 'PUBLISHED_WRONG_TAG'), ('publish-assets-changed', 'PUBLISHED_ASSETS_CHANGED')):
            with self.subTest(fault):
                self.fake.close()
                self.fake = FakeGitHub()
                self.fake.faults = {fault}
                (self.root / 'receipt.json').unlink(missing_ok=True)
                self.assertCode(self.publish(), code)
                self.assertEqual((self.receipt()['state'], self.receipt()['published']), ('published-mismatch', True))

    def test_a_symlink_in_the_bundle_is_refused_before_any_write(self):
        outside = self.root / 'outside'
        outside.mkdir()
        files = bundle_files()
        name = 'assets/' + ARCHIVES['checksums.txt'][0]
        for case in ('symlinked asset', 'symlinked assets directory'):
            with self.subTest(case):
                shutil.rmtree(self.root / 'bundle', ignore_errors=True)
                shutil.rmtree(outside)
                outside.mkdir()
                bundle = self.write_bundle(files)
                if case == 'symlinked asset':
                    (outside / 'archive').write_bytes(files[name])
                    (bundle / name).unlink()
                    (bundle / name).symlink_to(outside / 'archive')
                else:
                    shutil.move(str(bundle / 'assets'), str(outside / 'assets'))
                    (bundle / 'assets').symlink_to(outside / 'assets', target_is_directory=True)
                self.assertCode(self.publish(), 'BUNDLE_FILES_DIFFER')
                self.assertEqual(self.fake.writes(), [])

    def test_publish_requested_is_recorded_before_the_patch(self):
        self.fake.faults = {'patch-fails'}
        seen = []
        original = self.module.GitHub.request

        def watch(github, method, url, *args, **kwargs):
            if method == 'PATCH':
                seen.append((self.receipt()['state'], self.receipt()['published']))
            return original(github, method, url, *args, **kwargs)
        self.module.GitHub.request = watch
        try:
            self.publish()
        finally:
            self.module.GitHub.request = original
        self.assertEqual(seen, [('publish-requested', None)])

    def test_only_a_dispatch_from_main_may_publish_and_a_token_is_required(self):
        self.assertCode(self.publish(env={'GITHUB_REF': 'refs/heads/feature'}), 'NOT_MAIN')
        self.assertCode(self.publish(env={'PUBLISH_TOKEN': ''}), 'TOKEN_MISSING')
        self.assertEqual(self.fake.writes(), [])


class RecoverTests(Case):
    def recover(self, release_id, tag=TAG, delete=True):
        return self.run_cmd('recover', '--repo', REPO, '--release-id', str(release_id), '--tag', tag,
                            *(['--delete-draft'] if delete else []))

    def test_recover_deletes_only_a_draft_by_its_id(self):
        draft = self.fake.add_release(TAG, True)
        status, out, err = self.recover(draft['id'])
        self.assertEqual(status, 0, err)
        self.assertNotIn(draft['id'], self.fake.releases)
        self.assertIn('DELETED', out)

    def test_recover_never_deletes_a_published_release(self):
        public = self.fake.add_release(TAG, False)
        self.assertCode(self.recover(public['id']), 'RELEASE_NOT_A_DRAFT')
        self.assertIn(public['id'], self.fake.releases)
        self.assertFalse(any(m == 'DELETE' for m, _ in self.fake.writes()))

    def test_recover_refuses_another_tag_or_a_missing_release(self):
        draft = self.fake.add_release('v1.0.0-rc.2', True)
        self.assertCode(self.recover(draft['id']), 'RELEASE_OTHER_TAG')
        self.assertIn(draft['id'], self.fake.releases)
        self.assertCode(self.recover(999), 'RELEASE_NOT_FOUND')
        self.assertFalse(any(m == 'DELETE' for m, _ in self.fake.writes()))

    def test_recover_without_delete_only_reports(self):
        public = self.fake.add_release(TAG, False)
        status, out, err = self.recover(public['id'], delete=False)
        self.assertEqual(status, 0, err)
        self.assertIn('PUBLISHED', out)
        self.assertEqual(self.fake.writes(), [])


class PreflightTests(Case):
    def preflight(self, reviewer='peiman'):
        return self.run_cmd('preflight', '--repo', REPO, '--environment', 'release', env={'RELEASE_REQUIRED_REVIEWER': reviewer})

    def test_a_protected_main_only_environment_passes(self):
        status, out, err = self.preflight()
        self.assertEqual(status, 0, err)
        self.assertIn('READY', out)

    def test_each_missing_protection_is_refused(self):
        cases = {
            'missing environment': (lambda f: setattr(f, 'environment', None), 'ENV_MISSING'),
            'no required reviewer': (lambda f: f.environment.update(protection_rules=[{'type': 'branch_policy'}]), 'REVIEWER_NOT_SOLE'),
            'reviewer rule without reviewers': (lambda f: f.environment.update(protection_rules=[{'type': 'required_reviewers', 'reviewers': []}]), 'REVIEWER_NOT_SOLE'),
            'admins can bypass': (lambda f: f.environment.update(can_admins_bypass=True), 'ADMIN_BYPASS'),
            'admin bypass not reported': (lambda f: f.environment.pop('can_admins_bypass'), 'ADMIN_BYPASS'),
            'any branch': (lambda f: f.environment.update(deployment_branch_policy=None), 'BRANCHES_NOT_MAIN_ONLY'),
            'protected branches': (lambda f: f.environment.update(deployment_branch_policy={'protected_branches': True, 'custom_branch_policies': False}), 'BRANCHES_NOT_MAIN_ONLY'),
            'more than main': (lambda f: setattr(f, 'branch_policies', [{'name': 'main', 'type': 'branch'}, {'name': 'release/*', 'type': 'branch'}]), 'BRANCHES_NOT_MAIN_ONLY'),
            'a tag policy': (lambda f: setattr(f, 'branch_policies', [{'name': 'main', 'type': 'tag'}]), 'BRANCHES_NOT_MAIN_ONLY'),
            'unreadable': (lambda f: f.faults.add('environment-403'), 'ENV_UNREADABLE'),
        }
        for name, (change, code) in cases.items():
            with self.subTest(name):
                self.fake.close()
                self.fake = FakeGitHub()
                change(self.fake)
                self.assertCode(self.preflight(), code)

    def test_the_configured_reviewer_must_be_set_and_the_only_required_reviewer(self):
        self.assertCode(self.preflight(reviewer=''), 'REVIEWER_UNSET')
        self.assertCode(self.preflight(reviewer='someone-else'), 'REVIEWER_NOT_SOLE')
        self.assertEqual(self.preflight(reviewer='Peiman')[0], 0, 'GitHub logins compare case-insensitively')
        peiman = {'type': 'User', 'reviewer': {'login': 'peiman'}}
        team = {'type': 'Team', 'reviewer': {'slug': 'release-team'}}
        other = {'type': 'User', 'reviewer': {'login': 'someone-else'}}
        rule = lambda *reviewers: {'type': 'required_reviewers', 'reviewers': list(reviewers)}
        cases = {
            'exactly peiman': ([rule(peiman), {'type': 'branch_policy'}], True),
            'exactly Peiman, other case': ([rule({'type': 'User', 'reviewer': {'login': 'PEIMAN'}})], True),
            'peiman + team': ([rule(peiman, team)], False),
            'peiman + another user': ([rule(peiman, other)], False),
            'only a team': ([rule(team)], False),
            'a team carrying the login': ([rule({'type': 'Team', 'reviewer': {'slug': 'peiman', 'login': 'peiman'}})], False),
            'only another user': ([rule(other)], False),
            'two reviewer rules': ([rule(peiman), rule(other)], False),
            'two reviewer rules, both peiman': ([rule(peiman), rule(peiman)], False),
            'no reviewer rule': ([{'type': 'branch_policy'}], False),
        }
        for name, (rules, ok) in cases.items():
            with self.subTest(name):
                self.fake.environment['protection_rules'] = rules
                result = self.preflight()
                if ok:
                    self.assertEqual(result[0], 0, name + ': ' + result[2])
                else:
                    self.assertCode(result, 'REVIEWER_NOT_SOLE')

    def test_only_a_dispatch_from_main_may_preflight(self):
        self.assertCode(self.run_cmd('preflight', '--repo', REPO, '--environment', 'release',
                                     env={'GITHUB_REF': 'refs/heads/feature'}), 'NOT_MAIN')


# --- mutation proofs: remove each guard and the test that owns it must fail ---

MUTATIONS = [
    # (guard, exact text in publish.py, replacement, owning test)
    ('run path/event/branch/status', "if seen != (SOURCE_WORKFLOW, 'workflow_dispatch', 'main', 'completed', 'success'):",
     'if False:', 'FetchTests.test_the_run_must_be_a_successful_main_dispatch_of_the_prepare_workflow'),
    ('exactly one bundle artifact', 'if len(bundles) != 1 or not bundle_ok(bundles[0]):', 'if not bundles:',
     'FetchTests.test_exactly_one_unexpired_bundle_artifact_of_that_run'),
    ('manifest tag', "if manifest.get('release_tag') != tag:", 'if False:', 'FetchTests.test_the_manifest_binds_the_tag_and_the_run'),
    ('manifest run', "if manifest.get('prepare_run_id') != int(run_id):", 'if False:', 'FetchTests.test_the_manifest_binds_the_tag_and_the_run'),
    ('no missing or extra files', 'if set(present) != set(listed):', 'if False:', 'FetchTests.test_a_tampered_or_incomplete_bundle_is_refused'),
    ('every sha256 and size', "if len(data) != entry['size'] or sha256(data) != entry['sha256']:", 'if False:',
     'FetchTests.test_a_tampered_or_incomplete_bundle_is_refused'),
    ('archives match the checksum files', 'if set(assets) != expected:', 'if False:', 'FetchTests.test_the_archives_must_match_both_checksum_files'),
    ('archive bytes match the checksum line', 'if name in sums and sha256(data) != sums[name]:', 'if False:',
     'FetchTests.test_the_archives_must_match_both_checksum_files'),
    ('publish needs a publish bundle with its record', "if manifest.get('mode') != 'publish' or RECORD not in listed:", 'if False:',
     'FetchTests.test_publish_mode_refuses_a_rehearsal_bundle_or_tag_and_needs_the_record'),
    ('no rehearsal tag when publishing', 'if is_rehearsal(tag) or is_rehearsal(candidate):', 'if False:',
     'PublishTests.test_a_rehearsal_tag_or_bundle_is_never_published'),
    ('zip entry names', 'if not ENTRY.fullmatch(info.filename.rstrip', 'if not ENTRY.fullmatch(info.filename.lstrip("./").rstrip',
     'FetchTests.test_an_unsafe_zip_is_refused'),
    ('zip symlinks', 'if stat.S_ISLNK(info.external_attr >> 16):', 'if False:', 'FetchTests.test_an_unsafe_zip_is_refused'),
    ('zip size cap', 'if size > MAX_BUNDLE_BYTES:', 'if False:', 'FetchTests.test_an_oversized_zip_is_refused'),
    ('private failures are codes only', "raise Refused('SOURCE_API_FAILED') from None", "raise Refused('SOURCE_API_FAILED', text=str(error)) from None",
     'PublicLogTests.test_a_failing_private_api_call_prints_only_the_fixed_code'),
    ('unexpected errors are codes only', "print(Refused('INTERNAL_ERROR').line(args.command), file=sys.stderr)",
     "print(f'REFUSED {args.command}: {error}', file=sys.stderr)", 'PublicLogTests.test_an_unexpected_error_prints_only_internal_error'),
    ('details are allowlisted', 'if isinstance(value, int) or SAFE.fullmatch(str(value)):', 'if True:',
     'PublicLogTests.test_details_are_only_ids_tags_names_and_digests'),
    ('main only', "if env.get('GITHUB_REF') != MAIN:", 'if False:', 'FetchTests.test_only_a_dispatch_from_main_may_fetch'),
    ('manifest source binding', "if manifest.get('source_repository') != SOURCE_REPO or manifest.get('source_workflow') != SOURCE_WORKFLOW:",
     'if False:', 'SourceBindingTests.test_publish_refuses_a_manifest_from_another_source_before_any_write'),
    ('run age', 'if now() - created > MAX_BUNDLE_AGE:', 'if False:', 'ExpiryTests.test_a_run_older_than_seven_days_is_refused_before_any_download'),
    ('expired artifact', 'if bundle_expired(bundles[0]):', 'if False:', 'ExpiryTests.test_an_expired_or_vanished_bundle_is_refused_with_the_fixed_code'),
    ('source blocks a tag named main', 'if not any(ruleset_blocks_tag_main(ruleset, run_created) for ruleset in rulesets):', 'if False:',
     'SourceTagMainTests.test_the_source_must_block_a_tag_named_main'),
    ('the tag ruleset needs creation and update', "return covers and {'creation', 'update'} <= kinds", 'return covers',
     'SourceTagMainTests.test_the_source_must_block_a_tag_named_main'),
    ('the tag ruleset must cover main', 'covers = includes and not excludes', 'covers = True',
     'SourceTagMainTests.test_the_source_must_block_a_tag_named_main'),
    ('only known covering include forms', 'includes = any(str(p) in COVERING_INCLUDES for p in names.get(\'include\') or [])',
     'includes = any(p == \'~ALL\' or fnmatch.fnmatchcase(TAG_MAIN, str(p)) for p in names.get(\'include\') or [])',
     'SourceTagMainTests.test_the_source_must_block_a_tag_named_main'),
    ('any exclude that could match main', 'excludes = any(could_match_main(p) for p in names.get(\'exclude\') or [])',
     'excludes = any(str(p) in COVERING_INCLUDES for p in names.get(\'exclude\') or [])',
     'SourceTagMainTests.test_the_source_must_block_a_tag_named_main'),
    ('the ruleset predates the run', 'if updated is None or updated > run_created:', 'if updated is None:',
     'SourceTagMainTests.test_the_ruleset_must_predate_the_prepare_run'),
    ('rehearse drafts carry a rehearsal tag', "draft_tag = tag if mode == 'publish' or is_rehearsal(tag) else f'{tag}-rehearsal.{manifest[\"prepare_run_id\"]}'",
     'draft_tag = tag', 'PublishTests.test_a_rehearse_draft_is_unmistakable_and_never_carries_a_real_tag'),
    ('rehearse drafts are named for what they are', "name = tag if mode == 'publish' else f'{REHEARSAL_TITLE} {tag}'", 'name = tag',
     'PublishTests.test_a_rehearse_draft_is_unmistakable_and_never_carries_a_real_tag'),
    ('a mismatched publish is recorded', "receipt.update(state='published-mismatch', published=True)", 'pass',
     'PublishTests.test_a_confirmed_but_mismatched_publish_records_the_real_outcome'),
    ('bundle files are not links', 'if item.is_symlink() or not item.is_file():', 'if False:',
     'PublishTests.test_a_symlink_in_the_bundle_is_refused_before_any_write'),
    ('manifest run IDs are counts', "if not all(is_count(manifest.get(key)) for key in ('tag_run_id', 'tag_run_attempt', 'prepare_run_id')):",
     'if False:', 'FetchTests.test_a_tampered_or_incomplete_bundle_is_refused'),
    ('gone zip is a fixed code', "code = 'BUNDLE_EXPIRED' if error.status in (404, 410) else 'SOURCE_API_FAILED'", "code = 'SOURCE_API_FAILED'",
     'ExpiryTests.test_an_expired_or_vanished_bundle_is_refused_with_the_fixed_code'),
    ('draft first, never a tag in rehearse', "'body': body, 'draft': True,", "'body': body, 'draft': mode == 'publish',",
     'PublishTests.test_a_rehearse_run_never_creates_a_tag_or_sends_draft_false'),
    ('storage never sees the token', "request.add_unredirected_header('Authorization', f'Bearer {self.token}')",
     "request.add_header('Authorization', f'Bearer {self.token}')", 'FetchTests.test_the_storage_redirect_never_receives_the_reader_token'),
    ('re-download digest', 'if sha256(downloaded) != sha256(data):', 'if False:',
     'PublishTests.test_a_digest_mismatch_on_re_download_is_refused_and_nothing_is_published'),
    ('exact draft assets', 'if names != sorted(files):', 'if False:', 'PublishTests.test_an_extra_asset_on_the_draft_is_refused'),
    ('existing release', 'if existing:', 'if False:', 'PublishTests.test_an_existing_published_release_or_draft_is_refused'),
    ('read-back confirms draft false', "if final is None or final.get('draft') is not False:", 'if final is None:',
     'PublishTests.test_an_unconfirmed_publish_leaves_the_receipt_unknown_never_unpublished'),
    ('rehearse never publishes', "if mode == 'publish':", 'if True:', 'PublishTests.test_a_rehearse_run_drafts_and_never_publishes_even_a_rehearsal'),
    ('recover deletes only a draft', "if release.get('draft') is not True:", 'if False:', 'RecoverTests.test_recover_never_deletes_a_published_release'),
    ('sole reviewer', "if only.get('type') != 'User' or login.lower() != required.lower():", 'if False:',
     'PreflightTests.test_the_configured_reviewer_must_be_set_and_the_only_required_reviewer'),
    ('admin bypass', "if environment.get('can_admins_bypass') is not False:", 'if False:', 'PreflightTests.test_each_missing_protection_is_refused'),
    ('main-only branch policy', "if listed.get('branch_policies') is None or names != [('main', 'branch')]:", 'if False:',
     'PreflightTests.test_each_missing_protection_is_refused'),
]


class MutationTests(unittest.TestCase):
    def test_each_guard_bites_when_removed(self):
        source = SCRIPT.read_text()
        for guard, old, new, owner in MUTATIONS:
            with self.subTest(guard):
                self.assertEqual(source.count(old), 1, f'{guard}: the guarded text must occur exactly once')
                mutated = load(source.replace(old, new), name='publish_mutant')
                case, method = owner.split('.')
                cls = type('Mutant' + case, (globals()[case],), {'module': mutated})
                result = unittest.TestResult()
                with redirect_stderr(io.StringIO()):
                    cls(method).run(result)
                self.assertFalse(result.wasSuccessful(), f'{guard}: {owner} still passes without the guard')


if __name__ == '__main__':
    unittest.main()
