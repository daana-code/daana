"""The publish and test workflows' shape, each rule shown to bite by a mutation.

Needs PyYAML (installed for the test workflow only, with --require-hashes from
.github/tests/requirements.txt). publish.py itself is standard library only.
"""
import copy
import re
import subprocess
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
PUBLISH = ROOT / '.github/workflows/publish.yml'
TESTS = ROOT / '.github/workflows/tests.yml'
APP_TOKEN = 'actions/create-github-app-token@fee1f7d63c2ff003460e3d139729b119787bc349'
WORKFLOW_CREDENTIALS = {
    'RELEASE_REQUIRED_REVIEWER',
    'RELEASE_READER_APP_ID',
    'RELEASE_READER_PRIVATE_KEY',
    'RELEASE_APP_ID',
    'RELEASE_APP_PRIVATE_KEY',
    'DOCS_DISPATCH_APP_ID',
    'DOCS_DISPATCH_APP_PRIVATE_KEY',
}
READER_TOKEN_SCOPE = {
    'app-id': '${{ vars.RELEASE_READER_APP_ID }}',
    'private-key': '${{ secrets.RELEASE_READER_PRIVATE_KEY }}',
    'owner': 'daana-code',
    'repositories': 'daana-cli',
    'permission-actions': 'read',
}
WRITER_TOKEN_SCOPE = {
    'app-id': '${{ vars.RELEASE_APP_ID }}',
    'private-key': '${{ secrets.RELEASE_APP_PRIVATE_KEY }}',
    'owner': 'daana-code',
    'repositories': 'daana',
    'permission-contents': 'write',
}
DOCS_TOKEN_SCOPE = {
    'app-id': '${{ vars.DOCS_DISPATCH_APP_ID }}',
    'private-key': '${{ secrets.DOCS_DISPATCH_APP_PRIVATE_KEY }}',
    'owner': 'daana-code',
    'repositories': 'daana-cli',
    'permission-actions': 'write',
}
PUBLISH_ONLY = "inputs.mode == 'publish'"
REQUIRED_CHECK = 'Publisher tests'
# The same reviewed pins as the source repository's release workflows.
PINS = {
    'actions/checkout': 'd23441a48e516b6c34aea4fa41551a30e30af803',
    'actions/create-github-app-token': 'fee1f7d63c2ff003460e3d139729b119787bc349',
    'actions/upload-artifact': 'cf430e030ddbb5b0abf93d22962f4752f3646cd9',
}
PRIVATE_REFERENCE = re.compile('|'.join([
    r'(?<![\w/])#[0-9]{2,}\b',
    'daana-cli' + '#',
    r'\b10\.[0-9]+\.[0-9]+\.[0-9]+\b',
    r'\b192\.168\.',
    r'\.' + 'internal' + r'\b',
]), re.IGNORECASE)


# What the approver sees on the run and in the deployment review (display only, no shell).
RUN_NAME = '${{ inputs.mode }} ${{ inputs.release_tag }} from prepare run ${{ inputs.prepare_run_id }}'


def load(path=PUBLISH):
    # PyYAML reads the key `on` as True; map it back.
    doc = yaml.safe_load(path.read_text())
    if True in doc:
        doc['on'] = doc.pop(True)
    return doc


def strings(value, where=()):
    if isinstance(value, str):
        yield where, value
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from strings(item, where + (index,))
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from strings(item, where + (key,))


def step(doc, step_id):
    return next(s for s in doc['jobs']['publish']['steps'] if s.get('id') == step_id)


def index(doc, step_id):
    return [s.get('id') for s in doc['jobs']['publish']['steps']].index(step_id)


# --- guards over publish.yml ---------------------------------------------------

def guard_shape(doc):
    assert list(doc['on']) == ['workflow_dispatch'], 'dispatched only, never on a push, tag or release'
    inputs = doc['on']['workflow_dispatch']['inputs']
    assert list(inputs) == ['prepare_run_id', 'release_tag', 'mode'], 'the inputs are the prepare run, the tag and the mode'
    assert {k: inputs['mode'].get(k) for k in ('type', 'default', 'options')} == \
        {'type': 'choice', 'default': 'rehearse', 'options': ['rehearse', 'publish']}, 'rehearse is the default'
    assert doc.get('run-name') == RUN_NAME, 'the approval screen shows the mode, the tag and the prepare run'
    assert doc.get('permissions') == {}, 'no workflow-wide permission'
    assert doc.get('concurrency') == {'group': 'release-publish', 'cancel-in-progress': False}, 'one publish at a time'
    assert list(doc['jobs']) == ['publish'], 'one job'
    job = doc['jobs']['publish']
    assert job.get('if') == "github.ref == 'refs/heads/main'", 'runs only from main'
    assert job.get('environment') == 'release', 'approving the release deployment is the GO'
    assert job['runs-on'] == 'ubuntu-latest', 'hosted'
    assert job['permissions'] == {'contents': 'read', 'actions': 'read'}, 'GITHUB_TOKEN only reads; the App token writes'


def guard_order(doc):
    ids = [s.get('id') for s in doc['jobs']['publish']['steps']]
    assert ids == ['checkout', 'preflight', 'reader-token', 'fetch', 'app-token', 'publish', 'docs-token', 'docs', 'receipt'], \
        'preflight, then the reader, then fetch and verify, then the publisher token right before publishing, then the docs: ' + str(ids)


def guard_credentials(doc):
    found = {}
    for where, text in strings(doc):
        for name in re.findall(r'(?:secrets|vars)\.([A-Z_]+)', text):
            found.setdefault(name, []).append('.'.join(map(str, where)))
    reader, writer, docs = index(doc, 'reader-token'), index(doc, 'app-token'), index(doc, 'docs-token')
    preflight = index(doc, 'preflight')
    assert found == {
        'RELEASE_REQUIRED_REVIEWER': [f'jobs.publish.steps.{preflight}.env.RELEASE_REQUIRED_REVIEWER'],
        'RELEASE_READER_APP_ID': [f'jobs.publish.steps.{reader}.with.app-id'],
        'RELEASE_READER_PRIVATE_KEY': [f'jobs.publish.steps.{reader}.with.private-key'],
        'RELEASE_APP_ID': [f'jobs.publish.steps.{writer}.with.app-id'],
        'RELEASE_APP_PRIVATE_KEY': [f'jobs.publish.steps.{writer}.with.private-key'],
        'DOCS_DISPATCH_APP_ID': [f'jobs.publish.steps.{docs}.with.app-id'],
        'DOCS_DISPATCH_APP_PRIVATE_KEY': [f'jobs.publish.steps.{docs}.with.private-key'],
    }, 'each key and ID is read only by its own token step: ' + str(found)
    assert set(found) == WORKFLOW_CREDENTIALS, 'the workflow reads exactly the publisher credential names'
    assert all(step(doc, name)['uses'] == APP_TOKEN for name in ('reader-token', 'app-token', 'docs-token')), \
        'create-github-app-token pinned by SHA'
    assert step(doc, 'reader-token')['with'] == READER_TOKEN_SCOPE, \
        'the reader token: the source repository only, Actions read only'
    assert step(doc, 'app-token')['with'] == WRITER_TOKEN_SCOPE, \
        'the publisher token: this repository only, Contents write only'
    assert step(doc, 'docs-token')['with'] == DOCS_TOKEN_SCOPE, \
        'the docs token: the source repository only, Actions write only'
    uses = {name: ['.'.join(map(str, w)) for w, t in strings(doc) if f'steps.{name}.outputs.token' in t]
            for name in ('reader-token', 'app-token', 'docs-token')}
    assert uses == {'reader-token': [f'jobs.publish.steps.{index(doc, "fetch")}.env.READER_TOKEN'],
                    'app-token': [f'jobs.publish.steps.{index(doc, "publish")}.env.PUBLISH_TOKEN'],
                    'docs-token': [f'jobs.publish.steps.{index(doc, "docs")}.env.DOCS_TOKEN']}, \
        'each minted token reaches only its own step: ' + str(uses)
    github_token = ['.'.join(map(str, w)) for w, t in strings(doc) if 'github.token' in t or 'secrets.GITHUB_TOKEN' in t]
    assert github_token == [f'jobs.publish.steps.{preflight}.env.PREFLIGHT_TOKEN'], 'GITHUB_TOKEN is used only to read the environment'


def guard_commands(doc):
    for where, text in strings(doc['jobs']):
        if where[-1] == 'run':
            assert '${{' not in text, 'inputs reach a shell only through env: ' + '.'.join(map(str, where))
    assert step(doc, 'preflight')['run'] == \
        'python3 -I -B .github/scripts/publish.py preflight --repo daana-code/daana --environment release', 'the preflight is enforced'
    assert step(doc, 'fetch')['run'] == ('python3 -I -B .github/scripts/publish.py fetch --run-id "$PREPARE_RUN_ID" '
                                         '--tag "$RELEASE_TAG" --mode "$MODE" --out "$RUNNER_TEMP/bundle"'), 'fetch and verify the bundle'
    assert step(doc, 'publish')['run'] == (
        'python3 -I -B .github/scripts/publish.py publish --repo daana-code/daana --tag "$RELEASE_TAG" --run-id "$PREPARE_RUN_ID" '
        '--mode "$MODE" --bundle "$RUNNER_TEMP/bundle" --receipt "$RUNNER_TEMP/publish-receipt.json"'), 'publish to this repository'
    inputs = {'PREPARE_RUN_ID': '${{ inputs.prepare_run_id }}', 'RELEASE_TAG': '${{ inputs.release_tag }}', 'MODE': '${{ inputs.mode }}'}
    for step_id in ('fetch', 'publish'):
        env = dict(step(doc, step_id)['env'])
        env.pop('READER_TOKEN' if step_id == 'fetch' else 'PUBLISH_TOKEN')
        assert env == inputs, step_id + ' gets the same inputs'
    assert step(doc, 'docs')['run'] == ('python3 -I -B .github/scripts/publish.py docs --tag "$RELEASE_TAG" --mode "$MODE" '
                                        '--receipt "$RUNNER_TEMP/publish-receipt.json"'), 'deploy the docs of this release'
    env = dict(step(doc, 'docs')['env'])
    env.pop('DOCS_TOKEN')
    assert env == {'RELEASE_TAG': inputs['RELEASE_TAG'], 'MODE': inputs['MODE']}, 'docs gets the tag and the mode'
    checkout = step(doc, 'checkout')
    assert checkout['with'] == {'persist-credentials': False}, 'checkout keeps no credential'


def guard_receipt(doc):
    keep = step(doc, 'receipt')
    assert keep.get('if') == 'always()', 'the receipt is kept even when the job fails'
    assert keep['with'] == {'name': 'release-publish-receipt', 'path': '${{ runner.temp }}/publish-receipt.json',
                            'retention-days': 90, 'if-no-files-found': 'ignore'}, 'the receipt, kept 90 days'


def guard_docs(doc):
    for step_id in ('docs-token', 'docs'):
        assert step(doc, step_id).get('if') == PUBLISH_ONLY, \
            f'{step_id}: mode=publish only, and only after every earlier step succeeded'
    job = doc['jobs']['publish']
    assert job.get('environment') == 'release', 'the docs App ID and key are release environment settings'
    assert step(doc, 'docs').get('timeout-minutes') == 40, \
        'the docs step is bounded above publish.py\'s own deadlines (2 + 20 + 5 minutes and the request timeouts)'
    assert job.get('timeout-minutes') >= 30 + 40, 'the job leaves the publish its 30 minutes, plus the docs step'


def guard_pinned(doc, raw):
    for job in doc['jobs'].values():
        for s in job['steps']:
            if 'uses' not in s:
                continue
            action, _, ref = s['uses'].partition('@')
            assert PINS.get(action) == ref, f'{s["uses"]} is pinned by full SHA to the reviewed release'
            assert f'uses: {s["uses"]} # v' in raw, f'{s["uses"]} names the release its SHA is'


# --- guards over tests.yml -------------------------------------------------------

def guard_tests_workflow(doc):
    assert list(doc['on']) == ['pull_request'], 'tests run on pull requests only'
    assert doc['permissions'] == {}, 'no workflow-wide permission'
    # GitHub counts a job skipped by its `if` as passed, so a required check must always run.
    # Fork runs wait for approval (Actions setting) and get no secret anyway.
    for name, each in doc['jobs'].items():
        assert 'if' not in each, f'{name}: a required check has no job-level if (a skipped job counts as passed)'
    assert list(doc['jobs']) == ['test']
    job = doc['jobs']['test']
    assert job.get('name') == REQUIRED_CHECK, 'the required check name is Publisher tests'
    assert job.get('permissions') == {'contents': 'read'}, 'read only'
    assert 'environment' not in job, 'never the release environment'
    for where, text in strings(doc):
        assert not re.search(r'secrets\.|vars\.|github\.token', text), 'no secret, variable or token: ' + '.'.join(map(str, where))
    for s in job['steps']:
        if 'uses' in s:
            assert s['uses'].startswith('actions/'), 'every allowed action is created by GitHub: ' + s['uses']
    runs = '\n'.join(s.get('run', '') for s in job['steps'])
    for line in runs.splitlines():
        if 'pip install' in line:
            assert line.strip().endswith('pip install --require-hashes -r .github/tests/requirements.txt'), 'pip installs only hashed requirements'


GUARDS = [guard_shape, guard_order, guard_credentials, guard_commands, guard_receipt, guard_docs]


def find(doc, step_id):
    return step(doc, step_id)


MUTATIONS = [
    (guard_shape, lambda d: d['on'].update(push={'tags': ['v*']}), 'dispatched only'),
    (guard_shape, lambda d: d.update(permissions={'contents': 'write'}), 'no workflow-wide permission'),
    (guard_shape, lambda d: d.pop('run-name'), 'the approval screen shows the mode, the tag and the prepare run'),
    (guard_shape, lambda d: d.update({'run-name': 'Publish a release'}), 'the approval screen shows the mode, the tag and the prepare run'),
    (guard_shape, lambda d: d['jobs']['publish'].update(permissions={'contents': 'write', 'actions': 'read'}), 'GITHUB_TOKEN only reads'),
    (guard_shape, lambda d: d['jobs']['publish'].pop('environment'), 'approving the release deployment is the GO'),
    (guard_shape, lambda d: d['jobs']['publish'].update({'if': 'always()'}), 'runs only from main'),
    (guard_shape, lambda d: d['on']['workflow_dispatch']['inputs']['mode'].update(default='publish'), 'rehearse is the default'),
    (guard_shape, lambda d: d['concurrency'].update({'cancel-in-progress': True}), 'one publish at a time'),
    (guard_order, lambda d: d['jobs']['publish']['steps'].insert(2, d['jobs']['publish']['steps'].pop(4)),
     'the publisher token right before publishing'),
    (guard_order, lambda d: d['jobs']['publish']['steps'].pop(1), 'preflight, then the reader'),
    (guard_credentials, lambda d: find(d, 'fetch')['env'].update(KEY='${{ secrets.RELEASE_APP_PRIVATE_KEY }}'), 'each key and ID is read only by its own token step'),
    (guard_credentials, lambda d: find(d, 'app-token')['with'].update(repositories='daana,daana-cli'), 'the publisher token: this repository only'),
    (guard_credentials, lambda d: find(d, 'app-token')['with'].update({'permission-actions': 'write'}), 'the publisher token: this repository only'),
    (guard_credentials, lambda d: find(d, 'reader-token')['with'].update({'permission-contents': 'read'}), 'the reader token: the source repository only'),
    (guard_credentials, lambda d: find(d, 'reader-token').update(uses='actions/create-github-app-token@v2'), 'pinned by SHA'),
    (guard_credentials, lambda d: find(d, 'fetch')['env'].update(READER_TOKEN='${{ steps.app-token.outputs.token }}'), 'each minted token reaches only its own step'),
    (guard_credentials, lambda d: find(d, 'publish')['env'].update(EXTRA='${{ steps.reader-token.outputs.token }}'), 'each minted token reaches only its own step'),
    (guard_credentials, lambda d: find(d, 'publish')['env'].update(GH_TOKEN='${{ github.token }}'), 'GITHUB_TOKEN is used only to read the environment'),
    (guard_commands, lambda d: find(d, 'fetch').update(run='python3 -I -B .github/scripts/publish.py fetch --run-id "${{ inputs.prepare_run_id }}"'), 'inputs reach a shell only through env'),
    (guard_commands, lambda d: find(d, 'preflight').update(run=find(d, 'preflight')['run'] + ' || true'), 'the preflight is enforced'),
    (guard_commands, lambda d: find(d, 'publish').update(run=find(d, 'publish')['run'].replace('daana-code/daana', 'daana-code/daana-cli')), 'publish to this repository'),
    (guard_commands, lambda d: find(d, 'publish')['env'].update(MODE='publish'), 'publish gets the same inputs'),
    (guard_commands, lambda d: find(d, 'checkout')['with'].update({'persist-credentials': True}), 'checkout keeps no credential'),
    (guard_receipt, lambda d: find(d, 'receipt').pop('if'), 'the receipt is kept even when the job fails'),
    (guard_order, lambda d: d['jobs']['publish']['steps'].insert(5, d['jobs']['publish']['steps'].pop(7)), 'then the docs'),
    (guard_docs, lambda d: find(d, 'docs').pop('if'), 'docs: mode=publish only'),
    (guard_docs, lambda d: find(d, 'docs').update({'if': "always() && inputs.mode == 'publish'"}), 'docs: mode=publish only'),
    (guard_docs, lambda d: find(d, 'docs-token').update({'if': 'always()'}), 'docs-token: mode=publish only'),
    (guard_docs, lambda d: find(d, 'docs').update({'timeout-minutes': 30}), 'the docs step is bounded'),
    (guard_docs, lambda d: d['jobs']['publish'].update({'timeout-minutes': 60}), 'the job leaves the publish its 30 minutes'),
    (guard_credentials, lambda d: find(d, 'docs-token')['with'].update(repositories='daana-cli,daana'), 'the docs token: the source repository only'),
    (guard_credentials, lambda d: find(d, 'docs-token')['with'].update({'permission-contents': 'write'}), 'the docs token: the source repository only'),
    (guard_credentials, lambda d: find(d, 'docs-token').update(uses='actions/create-github-app-token@v2'), 'pinned by SHA'),
    (guard_credentials, lambda d: find(d, 'publish')['env'].update(DOCS_TOKEN='${{ steps.docs-token.outputs.token }}'), 'each minted token reaches only its own step'),
    (guard_credentials, lambda d: find(d, 'docs')['env'].update(KEY='${{ secrets.DOCS_DISPATCH_APP_PRIVATE_KEY }}'), 'each key and ID is read only by its own token step'),
    (guard_commands, lambda d: find(d, 'docs').update(run=find(d, 'docs')['run'].replace('--mode "$MODE"', '--mode publish')), 'deploy the docs of this release'),
    (guard_commands, lambda d: find(d, 'docs')['env'].update(MODE='publish'), 'docs gets the tag and the mode'),
]


class WorkflowTests(unittest.TestCase):
    def test_the_publish_workflow_keeps_every_rule(self):
        doc = load()
        for guard in GUARDS:
            with self.subTest(guard.__name__):
                guard(doc)
        guard_pinned(doc, PUBLISH.read_text())

    def test_each_rule_refuses_its_mutation(self):
        for guard, mutate, reason in MUTATIONS:
            with self.subTest(reason=reason):
                doc = load()
                mutate(doc)
                with self.assertRaises(AssertionError) as caught:
                    guard(doc)
                self.assertIn(reason, str(caught.exception))
        raw = PUBLISH.read_text()
        for old, new in ((APP_TOKEN, 'actions/create-github-app-token@v2.2.2'), ('# v6.1.0', '')):
            with self.subTest(pin=old):
                text = raw.replace(old, new)
                doc = yaml.safe_load(text)
                with self.assertRaises(AssertionError):
                    guard_pinned(doc, text)

    def test_the_tests_workflow_has_no_secret_and_always_runs(self):
        doc = load(TESTS)
        guard_tests_workflow(doc)
        guard_pinned(doc, TESTS.read_text())
        for mutate, reason in ((lambda d: d['on'].update(pull_request_target={}), 'pull requests only'),
                               (lambda d: d['jobs']['test'].update({'if': 'github.event.pull_request.head.repo.full_name == github.repository'}),
                                'a required check has no job-level if'),
                               (lambda d: d['jobs'].update(other={'if': 'false', 'runs-on': 'ubuntu-latest', 'permissions': {}, 'steps': []}),
                                'a required check has no job-level if'),
                               (lambda d: d['jobs']['test'].update(permissions={'contents': 'write'}), 'read only'),
                               (lambda d: d['jobs']['test']['steps'][-1].update(env={'T': '${{ secrets.RELEASE_APP_PRIVATE_KEY }}'}), 'no secret'),
                               (lambda d: d['jobs']['test'].update(environment='release'), 'never the release environment'),
                               (lambda d: d['jobs']['test'].update(name='Tests'), 'the required check name is Publisher tests'),
                               (lambda d: d['jobs']['test']['steps'][0].update(uses='third-party/checkout@v1'),
                                'every allowed action is created by GitHub')):
            with self.subTest(reason=reason):
                doc = load(TESTS)
                mutate(doc)
                with self.assertRaises(AssertionError) as caught:
                    guard_tests_workflow(doc)
                self.assertIn(reason, str(caught.exception))


class PublicRepositoryTests(unittest.TestCase):
    """Nothing private: no internal issue numbers, hosts or infrastructure names."""

    def test_no_private_references_in_tracked_files(self):
        listed = subprocess.run(['git', 'ls-files', '-co', '--exclude-standard'], cwd=ROOT, capture_output=True, text=True, check=True)
        for sample in ('fixes ' + '#' + '687', 'daana-cli' + '#' + '1', 'host 10.0.0.' + '5',
                       'host 192.' + '168.1.2', 'service.example.' + 'internal'):
            self.assertTrue(PRIVATE_REFERENCE.search(sample), 'the regex check must bite')
        for name in listed.stdout.splitlines():
            path = ROOT / name
            if '__pycache__' in path.parts:
                continue
            if not path.is_file():
                continue
            text = path.read_text(errors='replace')
            match = PRIVATE_REFERENCE.search(text)
            self.assertIsNone(match, f'{name}: {match and match.group(0)!r}')


if __name__ == '__main__':
    unittest.main()
