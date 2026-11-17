#!/usr/bin/env python3
"""Publish a prepared Daana CLI release bundle to this repository after GO.

The private source repository builds and qualifies a release, then a dispatched
"prepare" run of its release-publish workflow uploads one artifact, the publish
bundle: the platform archives and both checksum files (assets/), the release
body, the qualification record and bundle-manifest.json (every file's name,
size and SHA-256). This script takes exactly those bytes and publishes them
here. Approving the `release` environment deployment of publish.yml is the GO.
Nothing is built here. Standard library only.

  preflight --repo OWNER/NAME --environment release
      Refuse unless the environment exists, its one and only required reviewer
      is the User named in RELEASE_REQUIRED_REVIEWER, admin bypass is off and it
      deploys from main only. Token: PREFLIGHT_TOKEN.

  fetch --run-id N --tag TAG --mode rehearse|publish --out DIR
      Prove run N is a completed, successful workflow_dispatch run of the
      source's .github/workflows/release-publish.yml on main, created less than
      7 days ago (else BUNDLE_EXPIRED), find its one unexpired bundle artifact, download it by ID, unpack it safely into the
      new directory DIR and verify it (see verify_bundle). Token: READER_TOKEN
      (the Release Reader App, Actions: read on the source repository only).

  publish --repo OWNER/NAME --tag TAG --run-id N --mode rehearse|publish
          --bundle DIR --receipt FILE
      Verify the bundle again, refuse an existing release or draft for TAG,
      create a draft, write the receipt with the draft's ID before the first
      upload, upload exactly the assets, check the draft holds exactly those
      asset IDs, download each again and compare its SHA-256. mode=publish then
      records publish-requested, sets draft=false (a pre-release when TAG has a
      pre-release suffix) and reads the release back; any lost answer leaves
      the receipt `unknown`. mode=rehearse stops at the verified draft.
      Token: PUBLISH_TOKEN (the Release Publisher App, Contents: write here).

  recover --repo OWNER/NAME --release-id N --tag TAG [--delete-draft]
      Read a release by ID. --delete-draft deletes it only while it is still a
      draft of TAG; a published release is never deleted. Token: PUBLISH_TOKEN.

preflight, fetch and publish refuse unless GITHUB_REF is refs/heads/main.

Actions logs of this repository are public. Output is limited to fixed codes,
asset names, sizes, SHA-256s, numeric IDs and tags: no API response bodies,
URLs or exception text. Exit 0 passes; exit 1 prints `REFUSED <command>: <CODE>`.
"""
import argparse
import fnmatch
from datetime import datetime, timedelta, timezone
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile

API = 'https://api.github.com'
MAIN = 'refs/heads/main'
SOURCE_REPO = 'daana-code/daana-cli'
SOURCE_WORKFLOW = '.github/workflows/release-publish.yml'
BUNDLE_ARTIFACT = 'release-publish-bundle'
MANIFEST = 'bundle-manifest.json'
BODY = 'release-body.md'
RECORD = 'qualification.json'
CHECKSUMS = ('checksums.txt', 'checksums_darwin.txt')
MODES = ('rehearse', 'publish')
# A run dispatched on a source TAG named `main` reports head_branch `main`, like the branch.
TAG_MAIN = 'refs/tags/main'
# The include patterns that count as covering refs/tags/main, in the full form the
# rulesets API returns. Nothing else counts: Python's fnmatch lets `*` cross `/`,
# GitHub's patterns do not, so matching any other glob here could fail open.
COVERING_INCLUDES = ('~ALL', 'refs/tags/main', 'refs/tags/*', 'refs/tags/**')
REHEARSAL_TITLE = '[REHEARSAL \u2014 do not publish]'

REPO = re.compile(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+')
TAG = re.compile(r'v([0-9]+\.[0-9]+\.[0-9]+)(-[0-9A-Za-z.-]+)?')
ID = re.compile(r'[1-9][0-9]{0,18}')
HEX40 = re.compile(r'[0-9a-f]{40}')
HEX64 = re.compile(r'[0-9a-f]{64}')
CHECKSUM_LINE = re.compile(r'([0-9a-f]{64})  (\S+)')
# A bundle file: flat, or one level under assets/; no hidden or traversing names.
ENTRY = re.compile(r'(?:assets/)?[A-Za-z0-9][A-Za-z0-9._+-]{0,199}')
# A detail value that may appear in a public log: an ID, a tag, an asset name or a digest.
SAFE = re.compile(r'[A-Za-z0-9._+-]{1,128}')

# Bytes a bundle may hold, compressed or not; tests shorten it.
MAX_BUNDLE_BYTES = 4 * 1024 ** 3
# The prepare run keeps its bundle 7 days; an older run refuses with BUNDLE_EXPIRED.
MAX_BUNDLE_AGE = timedelta(days=7)
# Seconds before an API request gives up; tests shorten it.
TIMEOUT = 300


class Refused(Exception):
    """A fixed code, plus details that are printed only when they look like an ID, tag, name or digest.

    The shape check is a backstop, not the rule. The rule, for every call site:
    pass only an int, a tag already checked against TAG, an asset name already
    checked against ENTRY, a HEX64 digest or a fixed word written here. Never
    pass a value read from the source repository's API: a single-token private
    value (a branch name, a token) would pass the shape check.
    """

    def __init__(self, code, **detail):
        super().__init__(code)
        self.code, self.detail = code, detail

    def line(self, command):
        shown = []
        for key, value in sorted(self.detail.items()):
            if isinstance(value, int) or SAFE.fullmatch(str(value)):
                shown.append(f'{key}={value}')
            else:
                shown.append(f'{key}=<withheld>')
        return ' '.join([f'REFUSED {command}: {self.code}', *shown])


class ApiError(Exception):
    """An API call failed; only the HTTP status is kept, never the body or the URL."""

    def __init__(self, status=None):
        super().__init__()
        self.status = status


def require_main(env):
    if env.get('GITHUB_REF') != MAIN:
        raise Refused('NOT_MAIN')


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def is_rehearsal(tag):
    return '-rehearsal.' in str(tag).lower()


def within_limit(size):
    if size > MAX_BUNDLE_BYTES:
        raise Refused('BUNDLE_TOO_LARGE')


def now():
    return datetime.now(timezone.utc)


def parse_time(value):
    """A GitHub UTC timestamp (2026-11-16T12:00:00Z), or None."""
    try:
        return datetime.strptime(str(value), '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def parse_any_time(value):
    """An ISO 8601 timestamp with a zone (Z or an offset, any fraction), or None."""
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def could_match_main(pattern):
    """Fail closed for excludes: anything that might match the tag main, under any glob reading."""
    pattern = str(pattern)
    return pattern == '~ALL' or '{' in pattern or any(fnmatch.fnmatchcase(name, pattern) for name in (TAG_MAIN, 'main'))


def ruleset_blocks_tag_main(ruleset, run_created):
    """True when an active tag ruleset, unchanged since before the run was created, covers
    refs/tags/main with creation and update rules.

    A tag `main` made before the ruleset existed, or while it was off, survives Restrict
    creations; so a ruleset edited after the run does not count. The cost: after any edit
    to the ruleset, prepare runs from before the edit are refused (prepare again).
    """
    if not isinstance(ruleset, dict) or ruleset.get('target') != 'tag' or ruleset.get('enforcement') != 'active':
        return False
    names = ((ruleset.get('conditions') or {}).get('ref_name') or {})
    includes = any(str(p) in COVERING_INCLUDES for p in names.get('include') or [])
    excludes = any(could_match_main(p) for p in names.get('exclude') or [])
    covers = includes and not excludes
    updated = parse_any_time(ruleset.get('updated_at'))
    if updated is None or updated > run_created:
        return False
    kinds = {rule.get('type') for rule in ruleset.get('rules') or [] if isinstance(rule, dict)}
    return covers and {'creation', 'update'} <= kinds


def require_tag_main_blocked(source, run_created):
    """The Reader cannot tell a run on the branch main from one on a tag named main
    (both report head_branch `main`; telling them apart needs Contents: read). So the
    source must block that tag: rulesets are readable with Metadata: read. Bypass actors
    are not readable; keep the source repository's bypass list empty."""
    rulesets, page = [], 1
    while True:
        listed = source_call(source.request, 'GET', f'/rulesets?includes_parents=true&per_page=100&page={page}')
        if not isinstance(listed, list):
            break
        for summary in listed:
            if isinstance(summary, dict) and summary.get('target') == 'tag' and is_count(summary.get('id')):
                rulesets.append(source_call(source.request, 'GET', f'/rulesets/{summary["id"]}?includes_parents=true'))
        if len(listed) < 100:
            break
        page += 1
    if not any(ruleset_blocks_tag_main(ruleset, run_created) for ruleset in rulesets):
        raise Refused('SOURCE_TAG_MAIN_UNPROTECTED')


def bundle_expired(artifact):
    expires = parse_time(artifact.get('expires_at')) if artifact.get('expires_at') is not None else None
    return artifact.get('expired') is not False or (expires is not None and expires <= now())


def is_count(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def ships_candidate(release, candidate):
    """D10: the candidate's own tag, or the final version of an rc candidate."""
    r, c = TAG.fullmatch(release), TAG.fullmatch(candidate)
    if not r or not c:
        return False
    return release == candidate or (r.group(2) is None and r.group(1) == c.group(1)
                                    and re.fullmatch(r'-rc\.[1-9][0-9]*', c.group(2) or '') is not None)


class _NoAuthRedirect(urllib.request.HTTPRedirectHandler):
    """Downloads redirect to storage; the token is an unredirected header, so storage never sees it."""


class GitHub:
    def __init__(self, api, repo, token):
        self.api, self.repo, self.token = api.rstrip('/'), repo, token
        self.opener = urllib.request.build_opener(_NoAuthRedirect)

    def request(self, method, url, payload=None, data=None, headers=None, raw=False, sink=None):
        if not url.startswith('http'):
            url = f'{self.api}/repos/{self.repo}{url}'
        body = json.dumps(payload).encode() if payload is not None else data
        request = urllib.request.Request(url, data=body, method=method)
        request.add_unredirected_header('Authorization', f'Bearer {self.token}')
        request.add_header('Accept', 'application/vnd.github+json')
        request.add_header('X-GitHub-Api-Version', '2022-11-28')
        if payload is not None:
            request.add_header('Content-Type', 'application/json')
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with self.opener.open(request, timeout=TIMEOUT) as response:
                if sink is not None:
                    total = 0
                    while True:
                        chunk = response.read(1 << 20)
                        if not chunk:
                            return total
                        total += len(chunk)
                        within_limit(total)
                        sink.write(chunk)
                content = response.read()
        except urllib.error.HTTPError as error:
            error.close()
            if error.code == 404 and method == 'GET':
                return None
            raise ApiError(error.code) from None
        except (urllib.error.URLError, OSError, http.client.HTTPException):
            raise ApiError() from None
        if raw:
            return content
        try:
            return json.loads(content) if content else None
        except ValueError:
            raise ApiError() from None


# --- the source repository: read with the Reader token, failures are codes only ---

def source_call(call, *args, **kwargs):
    try:
        return call(*args, **kwargs)
    except ApiError:
        raise Refused('SOURCE_API_FAILED') from None


def check_inputs(args, need_repo=False):
    if (not ID.fullmatch(str(args.run_id)) or not TAG.fullmatch(str(args.tag)) or args.mode not in MODES
            or (need_repo and not REPO.fullmatch(str(args.repo)))):
        raise Refused('INPUT_INVALID')


def fetch(args, token):
    check_inputs(args)
    out = Path(args.out)
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise Refused('OUTPUT_NOT_EMPTY')
    if not token:
        raise Refused('TOKEN_MISSING')
    run_id = int(args.run_id)
    source = GitHub(args.api, SOURCE_REPO, token)
    run = source_call(source.request, 'GET', f'/actions/runs/{run_id}')
    if not isinstance(run, dict):
        raise Refused('SOURCE_RUN_NOT_FOUND', run=run_id)
    seen = (run.get('path'), run.get('event'), run.get('head_branch'), run.get('status'), run.get('conclusion'))
    if seen != (SOURCE_WORKFLOW, 'workflow_dispatch', 'main', 'completed', 'success'):
        raise Refused('SOURCE_RUN_NOT_PREPARE', run=run_id)
    if run.get('id') != run_id or (run.get('repository') or {}).get('full_name') != SOURCE_REPO:
        raise Refused('SOURCE_RUN_NOT_PREPARE', run=run_id)
    created = parse_time(run.get('created_at'))
    if created is None or created > now() + timedelta(minutes=5):
        raise Refused('SOURCE_RUN_NOT_PREPARE', run=run_id)
    if now() - created > MAX_BUNDLE_AGE:
        raise Refused('BUNDLE_EXPIRED', run=run_id)
    require_tag_main_blocked(source, created)
    artifacts, page = [], 1
    while True:
        listed = (source_call(source.request, 'GET', f'/actions/runs/{run_id}/artifacts?per_page=100&page={page}') or {})
        listed = listed.get('artifacts') if isinstance(listed, dict) else None
        if not isinstance(listed, list):
            raise Refused('SOURCE_API_FAILED')
        artifacts += listed
        if len(listed) < 100:
            break
        page += 1
    bundles = [artifact for artifact in artifacts if isinstance(artifact, dict) and artifact.get('name') == BUNDLE_ARTIFACT]

    def bundle_ok(artifact):
        origin = artifact.get('workflow_run') or {}
        return (is_count(artifact.get('id')) and origin.get('id') == run_id
                and origin.get('head_branch') == 'main' and origin.get('head_sha') == run.get('head_sha'))
    if len(bundles) != 1 or not bundle_ok(bundles[0]):
        raise Refused('BUNDLE_ARTIFACT_NOT_ONE', run=run_id, found=len(bundles))
    if bundle_expired(bundles[0]):
        raise Refused('BUNDLE_EXPIRED', run=run_id)
    artifact_id = bundles[0]['id']
    with tempfile.TemporaryDirectory(prefix='publish-bundle-') as scratch:
        archive = Path(scratch) / 'bundle.zip'
        with archive.open('wb') as sink:
            try:
                got = source.request('GET', f'{source.api}/repos/{SOURCE_REPO}/actions/artifacts/{artifact_id}/zip', sink=sink)
            except ApiError as error:
                # Expired storage answers 410: a fixed code, never the answer.
                code = 'BUNDLE_EXPIRED' if error.status in (404, 410) else 'SOURCE_API_FAILED'
                raise Refused(code, run=run_id) from None
        if got is None:
            raise Refused('BUNDLE_EXPIRED', run=run_id)
        extract(archive, out)
    assets, _, _ = verify_bundle(out, args.tag, run_id, args.mode)
    lines = [f'ASSET {name} {len(data)} {sha256(data)}' for name, data in sorted(assets.items())]
    lines.append(f'VERIFIED prepare run {run_id} artifact {artifact_id}: {len(assets)} assets for {args.tag} ({args.mode})')
    return '\n'.join(lines)


def extract(archive, out):
    """Unpack only flat names or assets/<name>, no links, within the size limit."""
    try:
        bundle = zipfile.ZipFile(archive)
    except (zipfile.BadZipFile, OSError):
        raise Refused('BUNDLE_ZIP_UNSAFE') from None
    with bundle:
        infos, seen = bundle.infolist(), set()
        for info in infos:
            if stat.S_ISLNK(info.external_attr >> 16):
                raise Refused('BUNDLE_ZIP_UNSAFE')
            if info.is_dir():
                if info.filename != 'assets/':
                    raise Refused('BUNDLE_ZIP_UNSAFE')
                continue
            if not ENTRY.fullmatch(info.filename.rstrip('/')) or info.filename in seen or info.filename == 'assets':
                raise Refused('BUNDLE_ZIP_UNSAFE')
            seen.add(info.filename)
        within_limit(sum(info.file_size for info in infos))
        out.mkdir(parents=True, exist_ok=True)
        for info in infos:
            if info.is_dir():
                continue
            target = out / info.filename
            target.parent.mkdir(exist_ok=True)
            try:
                data = bundle.read(info)
            except (zipfile.BadZipFile, OSError, ValueError):
                raise Refused('BUNDLE_ZIP_UNSAFE') from None
            with target.open('xb') as stream:
                stream.write(data)


def manifest_problem(manifest):
    """True when the manifest is not a well-formed schema-1 bundle manifest from the source repository."""
    if not isinstance(manifest, dict) or manifest.get('schema_version') != 1:
        return True
    if manifest.get('mode') not in MODES or not isinstance(manifest.get('candidate_tag'), str) or not isinstance(manifest.get('release_tag'), str):
        return True
    if not ships_candidate(manifest['release_tag'], manifest['candidate_tag']):
        return True
    if not all(is_count(manifest.get(key)) for key in ('tag_run_id', 'tag_run_attempt', 'prepare_run_id')):
        return True
    if not HEX40.fullmatch(str(manifest.get('tag_commit'))):
        return True
    files = manifest.get('files')
    if not isinstance(files, list) or not files:
        return True
    names = []
    for entry in files:
        if not isinstance(entry, dict) or set(entry) != {'name', 'size', 'sha256'}:
            return True
        name = entry['name']
        if not isinstance(name, str) or not ENTRY.fullmatch(name) or name == MANIFEST:
            return True
        if not name.startswith('assets/') and name not in (BODY, RECORD):
            return True
        if not isinstance(entry['size'], int) or isinstance(entry['size'], bool) or entry['size'] < 0:
            return True
        if not HEX64.fullmatch(str(entry['sha256'])):
            return True
        names.append(name)
    return len(names) != len(set(names)) or BODY not in names


def bundle_present(directory):
    """Every file in the bundle directory except the manifest; anything that is not a regular file refuses."""
    present = []
    for path in directory.iterdir():
        if path.name == MANIFEST:
            continue
        if path.name == 'assets' and path.is_dir() and not path.is_symlink():
            for item in path.iterdir():
                if item.is_symlink() or not item.is_file():
                    raise Refused('BUNDLE_FILES_DIFFER')
                present.append('assets/' + item.name)
        elif path.is_symlink() or not path.is_file():
            raise Refused('BUNDLE_FILES_DIFFER')
        else:
            present.append(path.name)
    return present


def verify_bundle(directory, tag, run_id, mode):
    """The bundle is exactly the manifest's files, byte for byte, for this tag and prepare run.

    Returns the release assets (archives and checksum files, by name), the
    release body and the manifest.
    """
    directory = Path(directory)
    try:
        manifest = json.loads((directory / MANIFEST).read_text())
    except (OSError, ValueError):
        raise Refused('MANIFEST_INVALID') from None
    if manifest_problem(manifest):
        raise Refused('MANIFEST_INVALID')
    # The manifest names its own source, apart from what the run API said.
    if manifest.get('source_repository') != SOURCE_REPO or manifest.get('source_workflow') != SOURCE_WORKFLOW:
        raise Refused('MANIFEST_SOURCE_MISMATCH')
    if manifest.get('release_tag') != tag:
        raise Refused('MANIFEST_TAG_MISMATCH', tag=tag)
    if manifest.get('prepare_run_id') != int(run_id):
        raise Refused('MANIFEST_RUN_MISMATCH', run=int(run_id))
    candidate = manifest['candidate_tag']
    listed = {entry['name']: entry for entry in manifest['files']}
    if mode != 'rehearse':
        if is_rehearsal(tag) or is_rehearsal(candidate):
            raise Refused('REHEARSAL_TAG', tag=tag, candidate=candidate)
        if manifest.get('mode') != 'publish' or RECORD not in listed:
            raise Refused('MANIFEST_NOT_FOR_PUBLISH', tag=tag)
    present = bundle_present(directory)
    if set(present) != set(listed):
        raise Refused('BUNDLE_FILES_DIFFER', missing=len(set(listed) - set(present)), extra=len(set(present) - set(listed)))
    contents = {}
    for name in sorted(listed):
        entry = listed[name]
        data = (directory / name).read_bytes()
        if len(data) != entry['size'] or sha256(data) != entry['sha256']:
            raise Refused('BUNDLE_FILE_MISMATCH', name=name.split('/')[-1])
        contents[name] = data
    assets = {name[len('assets/'):]: data for name, data in contents.items() if name.startswith('assets/')}
    sums = {}
    for checksums in CHECKSUMS:
        if checksums not in assets:
            raise Refused('CHECKSUMS_MISMATCH', name=checksums)
        for line in assets[checksums].decode('utf-8', 'replace').splitlines():
            match = CHECKSUM_LINE.fullmatch(line)
            if not match or match.group(2) in sums:
                raise Refused('CHECKSUMS_MISMATCH', name=checksums)
            sums[match.group(2)] = match.group(1)
    expected = set(sums) | set(CHECKSUMS)
    if set(assets) != expected:
        raise Refused('CHECKSUMS_MISMATCH', missing=len(expected - set(assets)), extra=len(set(assets) - expected))
    for name, data in sorted(assets.items()):
        if name in sums and sha256(data) != sums[name]:
            raise Refused('CHECKSUMS_MISMATCH', name=name)
    try:
        body = contents[BODY].decode('utf-8')
    except UnicodeDecodeError:
        raise Refused('MANIFEST_INVALID') from None
    return assets, body, manifest


# --- this repository: read and written with the Publisher token ---

def releases_for(github, tag):
    found = []
    published = github.request('GET', f'/releases/tags/{urllib.parse.quote(tag)}')
    if published:
        found.append(published)
    page = 1
    while True:
        listed = github.request('GET', f'/releases?per_page=100&page={page}') or []
        found += [release for release in listed if release.get('tag_name') == tag and release.get('draft')]
        if len(listed) < 100:
            return found
        page += 1


def write_receipt(path, receipt, new=False):
    with Path(path).open('x' if new else 'w') as stream:
        json.dump(receipt, stream, indent=2, sort_keys=True)
        stream.write('\n')


def publish(args, token):
    check_inputs(args, need_repo=True)
    if not token:
        raise Refused('TOKEN_MISSING')
    files, body, manifest = verify_bundle(args.bundle, args.tag, args.run_id, args.mode)
    github = GitHub(args.api, args.repo, token)
    try:
        return publish_verified(github, args, files, body, manifest)
    except ApiError as error:
        raise Refused('PUBLIC_API_FAILED', status=error.status or 0) from None


def publish_verified(github, args, files, body, manifest):
    tag, mode = args.tag, args.mode
    # A rehearse draft never carries the real tag or name: one click in the UI must
    # not publish an unqualified bundle as the real release (and v* tags are
    # restricted to the Publisher App by the tag ruleset anyway).
    draft_tag = tag if mode == 'publish' or is_rehearsal(tag) else f'{tag}-rehearsal.{manifest["prepare_run_id"]}'
    name = tag if mode == 'publish' else f'{REHEARSAL_TITLE} {tag}'
    existing = releases_for(github, draft_tag)
    if existing:
        raise Refused('RELEASE_EXISTS', kind='draft' if existing[0].get('draft') else 'published', id=existing[0].get('id'))
    release = github.request('POST', '/releases', payload={
        'tag_name': draft_tag, 'name': name, 'body': body, 'draft': True, 'prerelease': '-' in draft_tag})
    receipt = {'schema_version': 1, 'repository': args.repo, 'tag': draft_tag, 'release_tag': tag,
               'candidate_tag': manifest['candidate_tag'],
               'mode': mode, 'prepare_run_id': manifest['prepare_run_id'], 'tag_run_id': manifest['tag_run_id'],
               'release_id': release['id'], 'state': 'draft-created', 'published': False, 'assets': []}
    write_receipt(args.receipt, receipt, new=True)
    upload = release['upload_url'].split('{', 1)[0]
    uploaded = {}
    for name, data in files.items():
        asset = github.request('POST', f'{upload}?name={urllib.parse.quote(name)}', data=data,
                               headers={'Content-Type': 'application/octet-stream'})
        uploaded[name] = asset['id']
    listed = github.request('GET', f'/releases/{release["id"]}/assets?per_page=100') or []
    names = sorted(asset['name'] for asset in listed)
    if names != sorted(files):
        raise Refused('DRAFT_ASSETS_MISMATCH', release=release['id'], held=len(names), expected=len(files))
    if {asset['name']: asset['id'] for asset in listed} != uploaded:
        raise Refused('DRAFT_ASSET_IDS_CHANGED', release=release['id'])
    for name, data in files.items():
        downloaded = github.request('GET', f'/releases/assets/{uploaded[name]}', raw=True,
                                    headers={'Accept': 'application/octet-stream'})
        if downloaded is None:
            raise Refused('ASSET_VANISHED', name=name, id=uploaded[name])
        if sha256(downloaded) != sha256(data):
            raise Refused('ASSET_DIGEST_MISMATCH', name=name, got=sha256(downloaded), want=sha256(data))
    receipt.update(state='draft-verified', assets=[{'name': name, 'id': uploaded[name], 'size': len(data), 'sha256': sha256(data)}
                                                   for name, data in files.items()])
    write_receipt(args.receipt, receipt)
    if mode == 'publish':
        # From here the release may become public even if no answer arrives, so
        # until the read-back confirms it the outcome is unknown, never "not published".
        receipt.update(state='publish-requested', published=None)
        write_receipt(args.receipt, receipt)
        try:
            github.request('PATCH', f'/releases/{release["id"]}', payload={'draft': False})
            final = github.request('GET', f'/releases/{release["id"]}')
        except Exception:  # noqa: BLE001 - any lost answer: the release may be public
            final = None
        if final is None or final.get('draft') is not False:
            receipt.update(state='unknown', published=None)
            write_receipt(args.receipt, receipt)
            raise Refused('PUBLISH_OUTCOME_UNKNOWN', release=release['id'])
        wrong_tag = final.get('tag_name') != tag
        if wrong_tag or {asset['name']: asset['id'] for asset in final.get('assets') or []} != uploaded:
            # Public, but not as verified: record that before refusing.
            receipt.update(state='published-mismatch', published=True)
            write_receipt(args.receipt, receipt)
            raise Refused('PUBLISHED_WRONG_TAG' if wrong_tag else 'PUBLISHED_ASSETS_CHANGED', release=release['id'])
        receipt.update(state='published', published=True, html_url=final.get('html_url'))
        write_receipt(args.receipt, receipt)
    state = 'PUBLISHED' if receipt['published'] else 'DRAFT'
    lines = [f'ASSET {item["name"]} {item["size"]} {item["sha256"]} id={item["id"]}' for item in receipt['assets']]
    lines.append(f'{state} {args.repo} {draft_tag} release {release["id"]} with {len(files)} assets')
    return '\n'.join(lines)


def recover(args, token):
    """Read a release by ID; delete it only while it is still a draft of this tag."""
    if not REPO.fullmatch(args.repo) or not TAG.fullmatch(args.tag) or not ID.fullmatch(args.release_id):
        raise Refused('INPUT_INVALID')
    if not token:
        raise Refused('TOKEN_MISSING')
    github = GitHub(args.api, args.repo, token)
    try:
        release = github.request('GET', f'/releases/{args.release_id}')
        if not release:
            raise Refused('RELEASE_NOT_FOUND', release=int(args.release_id))
        if release.get('tag_name') != args.tag:
            raise Refused('RELEASE_OTHER_TAG', release=int(args.release_id))
        state = 'DRAFT' if release.get('draft') is True else 'PUBLISHED' if release.get('draft') is False else 'UNKNOWN'
        if args.delete_draft:
            if release.get('draft') is not True:
                raise Refused('RELEASE_NOT_A_DRAFT', release=int(args.release_id), state=state)
            github.request('DELETE', f'/releases/{args.release_id}')
            state = 'DELETED'
    except ApiError as error:
        raise Refused('PUBLIC_API_FAILED', status=error.status or 0) from None
    return f'{state} {args.repo} {args.tag} release {args.release_id}'


def sole_reviewer_problem(environment, required):
    """The GO is one person's: one required-reviewer rule naming exactly that User and nobody else.

    GitHub accepts any one required reviewer's approval, so a second reviewer
    (a user or a team) could give the GO.
    """
    rules = [rule for rule in environment.get('protection_rules') or [] if rule.get('type') == 'required_reviewers']
    if len(rules) != 1:
        return True
    reviewers = rules[0].get('reviewers') or []
    if len(reviewers) != 1:
        return True
    only = reviewers[0]
    login = str((only.get('reviewer') or {}).get('login') or '')
    if only.get('type') != 'User' or login.lower() != required.lower():
        return True
    return False


def preflight(args, token, required):
    """The GO depends on the environment: its sole required reviewer, no admin bypass, main only."""
    if not REPO.fullmatch(args.repo) or not re.fullmatch(r'[A-Za-z0-9_.-]+', args.environment):
        raise Refused('INPUT_INVALID')
    if not token:
        raise Refused('TOKEN_MISSING')
    if not required:
        raise Refused('REVIEWER_UNSET')
    github = GitHub(args.api, args.repo, token)
    problems = []
    try:
        environment = github.request('GET', f'/environments/{args.environment}')
        if not environment:
            raise Refused('ENV_MISSING')
        if sole_reviewer_problem(environment, required):
            problems.append('REVIEWER_NOT_SOLE')
        if environment.get('can_admins_bypass') is not False:
            problems.append('ADMIN_BYPASS')
        policy = environment.get('deployment_branch_policy') or {}
        if policy.get('custom_branch_policies') is not True or policy.get('protected_branches') is not False:
            problems.append('BRANCHES_NOT_MAIN_ONLY')
        else:
            listed = github.request('GET', f'/environments/{args.environment}/deployment-branch-policies?per_page=100') or {}
            names = [(item.get('name'), item.get('type', 'branch')) for item in listed.get('branch_policies') or []]
            if listed.get('branch_policies') is None or names != [('main', 'branch')]:
                problems.append('BRANCHES_NOT_MAIN_ONLY')
    except ApiError as error:
        if error.status in (401, 403):
            raise Refused('ENV_UNREADABLE', status=error.status) from None
        raise Refused('PUBLIC_API_FAILED', status=error.status or 0) from None
    if problems:
        raise Refused(problems[0], also='+'.join(problems[1:]) or 'none')
    return f'READY {args.repo} environment {args.environment}'


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)
    check = commands.add_parser('preflight')
    check.add_argument('--api', default=API)
    check.add_argument('--repo', required=True)
    check.add_argument('--environment', required=True)
    get = commands.add_parser('fetch')
    get.add_argument('--api', default=API)
    get.add_argument('--run-id', required=True)
    get.add_argument('--tag', required=True)
    get.add_argument('--mode', required=True)
    get.add_argument('--out', type=Path, required=True)
    put = commands.add_parser('publish')
    put.add_argument('--api', default=API)
    put.add_argument('--repo', required=True)
    put.add_argument('--tag', required=True)
    put.add_argument('--run-id', required=True)
    put.add_argument('--mode', required=True)
    put.add_argument('--bundle', type=Path, required=True)
    put.add_argument('--receipt', type=Path, required=True)
    fix = commands.add_parser('recover')
    fix.add_argument('--api', default=API)
    fix.add_argument('--repo', required=True)
    fix.add_argument('--release-id', required=True)
    fix.add_argument('--tag', required=True)
    fix.add_argument('--delete-draft', action='store_true')
    return parser


def main(argv=None, env=None):
    args = build_parser().parse_args(argv)
    env = os.environ if env is None else env
    try:
        if args.command == 'recover':
            # An operator's recovery, not a dispatch: its safety is the draft check.
            print(recover(args, env.get('PUBLISH_TOKEN', '')))
            return 0
        require_main(env)
        if args.command == 'preflight':
            print(preflight(args, env.get('PREFLIGHT_TOKEN', ''), env.get('RELEASE_REQUIRED_REVIEWER', '').strip()))
        elif args.command == 'fetch':
            print(fetch(args, env.get('READER_TOKEN', '')))
        else:
            print(publish(args, env.get('PUBLISH_TOKEN', '')))
    except Refused as refused:
        print(refused.line(args.command), file=sys.stderr)
        return 1
    except Exception as error:  # noqa: BLE001,F841 - never print exception text to a public log
        print(Refused('INTERNAL_ERROR').line(args.command), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
