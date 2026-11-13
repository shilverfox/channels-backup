# /// script
# requires-python = ">=3.10"
# dependencies = ["cramjam==2.11.0", "dfindexeddb==20260210"]
# [tool.uv]
# override-dependencies = ["python-snappy==0.7.3"]
# ///
"""Find cached WeChat Channels videos and download complete playable MP4s."""

import argparse
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from channels_storage import DEFAULT_ROOT, StorageError
from channels_sources import catalog, discover
from isaac64 import MAX_ENCRYPT_LENGTH, decrypt_head


CHUNK_SIZE = 524288
HOSTS = {'finder.video.qq.com', 'wxapp.tc.qq.com'}
UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
      'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36')


class DownloadError(ValueError):
    pass


def number(value):
    try:
        return float(value or 0)
    except (ValueError, TypeError):
        return 0


def select_format(feed, preference='best-h264'):
    specs = [s for s in feed.media.get('spec', [])
             if isinstance(s, dict) and re.fullmatch(r'[A-Za-z0-9_-]+', str(s.get('fileFormat', '')))]
    if preference in ('best-h264', 'best-h265'):
        codec = preference.removeprefix('best-')
        specs = [s for s in specs if s.get('codingFormat') == codec]
    elif preference != 'best':
        specs = [s for s in specs if s.get('fileFormat') == preference]
    if not specs:
        raise DownloadError('Requested format is not in this cached record; run list --json.')
    return max(specs, key=lambda s: (
        number(s.get('width')) * number(s.get('height')),
        number(s.get('videoBitrate', s.get('bitRate'))), number(s.get('vfps')),
    ))


def validate_url(url):
    parsed = urllib.parse.urlsplit(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise DownloadError('Invalid CDN port') from exc
    if (parsed.scheme != 'https' or parsed.hostname not in HOSTS
            or parsed.username or parsed.password or port not in (None, 443)):
        raise DownloadError('Refusing a non-HTTPS or unexpected CDN address')
    return parsed


class CDNRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def playback_url(feed, spec):
    url, token = feed.media.get('url'), feed.media.get('urlToken', '')
    if not isinstance(url, str) or not isinstance(token, str):
        raise DownloadError('Missing playback address or token; replay the video in WeChat.')
    parsed = validate_url(url + token)
    if parsed.hostname == 'wxapp.tc.qq.com':
        parsed = parsed._replace(netloc='finder.video.qq.com')
    # Preserve the signed query byte-for-byte; only replace player-owned parameters.
    params = {'web', 'taskid', 'fexam', 'X-snsvideoflag'}
    query = '&'.join(p for p in parsed.query.split('&')
                     if p and urllib.parse.unquote(p.split('=', 1)[0]) not in params)
    extra = urllib.parse.urlencode({
        'web': '1', 'taskid': 'pc-%d' % (time.time_ns() // 1000),
        'fexam': '1', 'X-snsvideoflag': spec['fileFormat'],
    })
    return urllib.parse.urlunsplit(parsed._replace(query=query + '&' + extra, fragment=''))


def safe_filename(feed):
    title = re.sub(r'[\x00-\x1f\x7f/\\:*?"<>|]', '_',
                   feed.nickname + '_' + feed.description.split('#', 1)[0])
    title = re.sub(r'\s+', ' ', title).strip(' ._') or 'video'
    # Leave room for the ID and extension on byte-limited filesystems.
    title = title.encode('utf-8')[:150].decode('utf-8', 'ignore').rstrip(' .')
    return '%s_%s.mp4' % (title, feed.id)


def is_mp4(data):
    return len(data) >= 12 and data[4:8] == b'ftyp'


def read_range(opener, url, pos, total, retries=3, timeout=30):
    headers = {
        'User-Agent': UA, 'Referer': 'https://liteapp.weixin.qq.com/',
        'Origin': 'https://liteapp.weixin.qq.com', 'Accept-Encoding': 'identity',
        'Range': 'bytes=%d-%d' % (pos, pos + CHUNK_SIZE - 1),
    }
    for attempt in range(retries + 1):
        try:
            with opener.open(urllib.request.Request(url, headers=headers), timeout=timeout) as response:
                if response.status != 206:
                    raise DownloadError('CDN did not return a ranged response (206).')
                match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)',
                                     response.headers.get('Content-Range', ''))
                if match is None:
                    raise DownloadError('Missing or invalid Content-Range.')
                start, end, size = map(int, match.groups())
                if (start != pos or end < start or end >= size
                        or end >= pos + CHUNK_SIZE or (total is not None and size != total)):
                    raise DownloadError('CDN returned an inconsistent byte range or file size.')
                part = response.read(CHUNK_SIZE + 1)
                if len(part) != end - start + 1:
                    raise OSError('Incomplete range response')
                return part, size, response.headers.get('X-encflag')
        except urllib.error.HTTPError as exc:
            if exc.code in (400, 401, 403, 404, 410):
                raise DownloadError('CDN HTTP %s: cached address may have expired. '
                                    'Replay this video in WeChat and try again.' % exc.code) from None
            if exc.code not in (408, 429, 500, 502, 503, 504) or attempt == retries:
                raise DownloadError('CDN HTTP %s; download stopped.' % exc.code) from None
        except (OSError, urllib.error.URLError, http.client.HTTPException):
            if attempt == retries:
                # Do not include exception text: network errors can contain signed URLs.
                raise DownloadError('Network read failed after %d attempts.' % (retries + 1)) from None
        time.sleep(min(2 ** attempt, 8))


def check_verification_tools(mode):
    required = [] if mode == 'none' else ['ffprobe']
    if mode == 'full':
        required.append('ffmpeg')
    missing = [name for name in required if not shutil.which(name)]
    if missing:
        raise DownloadError('Missing %s. Install FFmpeg or explicitly choose '
                            '--verify metadata/none; those do not prove full decodability.'
                            % ', '.join(missing))


def verify_file(path, mode='full', expected_duration=0):
    with Path(path).open('rb') as stream:
        if not is_mp4(stream.read(32)):
            raise DownloadError('Output has no valid MP4 ftyp header; refusing to publish it.')
    if mode == 'none':
        return {'validation': 'byte-count-and-mp4-header-only'}
    result = subprocess.run([
        'ffprobe', '-v', 'error', '-show_entries',
        'format=duration,size,bit_rate:stream=codec_name,codec_type,width,height,r_frame_rate,duration',
        '-of', 'json', str(path),
    ], capture_output=True, text=True)
    try:
        metadata = json.loads(result.stdout)
    except ValueError:
        metadata = {}
    videos = [s for s in metadata.get('streams', []) if s.get('codec_type') == 'video']
    duration = number(metadata.get('format', {}).get('duration'))
    if result.returncode or not videos or duration <= 0:
        raise DownloadError('ffprobe could not validate a playable video.')
    if expected_duration and abs(duration - expected_duration) > 2:
        raise DownloadError('Duration does not match the cached record (%.3fs vs %.3fs).'
                            % (duration, expected_duration))
    if mode == 'full':
        result = subprocess.run([
            'ffmpeg', '-hide_banner', '-nostdin', '-v', 'error', '-xerror',
            '-i', str(path), '-map', '0:v', '-map', '0:a?', '-f', 'null', '-',
        ], capture_output=True, text=True)
        if result.returncode or result.stderr.strip():
            raise DownloadError('Full audio/video decode failed; no final file was published.')
    metadata['validation'] = 'full-decode' if mode == 'full' else 'metadata-only'
    return metadata


def download(feed, output_dir, preference='best-h264', mode='full', retries=3,
             overwrite=False, progress=True, opener=None):
    check_verification_tools(mode)
    spec = select_format(feed, preference)
    url = playback_url(feed, spec)
    directory = Path(output_dir).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / safe_filename(feed)
    if destination.exists() and not overwrite:
        raise DownloadError('Output already exists: %s. Choose another directory or use --overwrite.'
                            % destination)
    opener = opener or urllib.request.build_opener(CDNRedirect())
    fd, temporary = tempfile.mkstemp(prefix='.channels-', suffix='.part', dir=directory)
    temporary = Path(temporary)
    total, pos, encryption, last_progress = None, 0, None, 0
    mask = None
    try:
        with os.fdopen(fd, 'wb') as stream:
            while total is None or pos < total:
                part, total, flag = read_range(opener, url, pos, total, retries)
                if pos == 0:
                    if flag not in (None, '0', '1'):
                        raise DownloadError('Unsupported encryption flag: %s' % flag)
                    encryption = flag
                    if flag == '1' or (flag is None and not is_mp4(part)):
                        try:
                            mask = decrypt_head(bytes(MAX_ENCRYPT_LENGTH), feed.media['decodeKey'])
                        except (KeyError, ValueError, TypeError):
                            raise DownloadError('Missing or invalid decodeKey; replay this video.') from None
                        encryption = '1'
                    elif flag is None:
                        encryption = '0'
                elif flag is not None and encryption is not None and flag != encryption:
                    raise DownloadError('Encryption flag changed during download.')
                if mask is not None and pos < MAX_ENCRYPT_LENGTH:
                    count = min(len(part), MAX_ENCRYPT_LENGTH - pos)
                    part = bytes(a ^ b for a, b in zip(part[:count], mask[pos:pos + count])) + part[count:]
                if pos == 0 and not is_mp4(part):
                    raise DownloadError('Decoded head is not MP4. The player format may have changed.')
                stream.write(part)
                pos += len(part)
                now = time.monotonic()
                if progress and (now - last_progress >= 1 or pos == total):
                    print('\rDownloaded %.1f / %.1f MB (%.1f%%)' %
                          (pos / 1e6, total / 1e6, pos * 100 / total), end='', file=sys.stderr, flush=True)
                    last_progress = now
            stream.flush()
            os.fsync(stream.fileno())
        if progress:
            print(file=sys.stderr)
        if pos != total or temporary.stat().st_size != total:
            raise DownloadError('Final byte count does not match the CDN size.')
        expected = number(spec.get('durationMs')) / 1000 or number(feed.media.get('videoPlayLen'))
        if progress and mode != 'none':
            print('Verifying %s...' % ('full decode' if mode == 'full' else 'metadata'), file=sys.stderr)
        metadata = verify_file(temporary, mode, expected)
        if overwrite:
            os.replace(temporary, destination)
        else:
            # Atomic no-clobber publication, even if another download races this one.
            try:
                os.link(temporary, destination)
            except FileExistsError:
                raise DownloadError('Output appeared during download; it was not overwritten.') from None
            temporary.unlink()
        result = {
            'id': feed.id, 'nickname': feed.nickname, 'description': feed.description,
            'cache_state': 'historical' if feed.historical else 'active',
            'path': str(destination), 'bytes': total, 'format': spec['fileFormat'],
            'encryption': encryption, 'metadata': metadata,
        }
        if feed.metadata_source:
            result.update(metadata_source=feed.metadata_source, sources=feed.provenance)
        return result
    finally:
        temporary.unlink(missing_ok=True)


def choose_feed(feeds, fid=None, query=None, latest=False):
    matches = feeds
    if fid:
        matches = [f for f in matches if f.id == fid]
    if query:
        needle = query.casefold()
        matches = [f for f in matches if needle in
                   (f.id + ' ' + f.nickname + ' ' + f.description).casefold()]
    if not matches:
        raise DownloadError('No matching cached video in the selected sources. '
                            'Use list --diagnostics and --include-history to inspect coverage.')
    if len(matches) > 1 and not latest:
        choices = '\n'.join('%s  %s  %s' % (f.id, f.nickname, f.description[:80])
                            for f in matches[:20])
        raise DownloadError('Multiple matches; select an exact --id:\n' + choices)
    return matches[0]


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest='command', required=True)
    sources = commands.add_parser('sources', help='List source capabilities, including research-only branches')
    sources.add_argument('--json', action='store_true')
    for name in ('list', 'download'):
        sub = commands.add_parser(name)
        sub.add_argument('--profiles-root', type=Path, default=DEFAULT_ROOT,
                         help='WeChat profiles root, one profile, or a Session Storage directory')
        sub.add_argument('--source', action='append',
                         help='all (default), session, or preload; repeat to select several sources')
        sub.add_argument('--query', help='Substring of account, description or ID')
        sub.add_argument('--include-history', action='store_true',
                         help='Also inspect retained deleted records and expired detail caches; CDN validates tokens')
        if name == 'list':
            sub.add_argument('--limit', type=int, default=20)
            sub.add_argument('--json', action='store_true', help='Safe metadata; never tokens or keys')
            sub.add_argument('--diagnostics', action='store_true',
                             help='JSON object with matching feeds and per-source coverage/status')
        else:
            selection = sub.add_mutually_exclusive_group()
            selection.add_argument('--id')
            selection.add_argument('--latest', action='store_true',
                                   help='Explicitly select the most recent cached match, not necessarily the playing video')
            sub.add_argument('--output-dir', type=Path, default=Path.cwd() / 'video')
            sub.add_argument('--format', default='best-h264',
                             help='best-h264 (default), best-h265, best, or an exact cached format code')
            sub.add_argument('--verify', choices=('full', 'metadata', 'none'), default='full')
            sub.add_argument('--retries', type=int, choices=range(0, 6), default=3)
            sub.add_argument('--overwrite', action='store_true')
            sub.add_argument('--quiet', action='store_true')
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == 'sources':
            if args.json:
                print(json.dumps(catalog(), ensure_ascii=False, indent=2))
            else:
                for source in catalog():
                    print('%s\t%s\t%s\t%s' % (source['name'], source['kind'],
                                                  source['status'], source['description']))
            return 0
        discovery = discover(args.profiles_root, include_history=args.include_history,
                             source_names=args.source)
        feeds = discovery.feeds
        for warning in discovery.warnings:
            print('Source warning: ' + warning, file=sys.stderr)
        if args.command == 'list':
            if args.limit < 1:
                raise DownloadError('--limit must be positive.')
            if args.query:
                needle = args.query.casefold()
                feeds = [f for f in feeds if needle in
                         (f.id + ' ' + f.nickname + ' ' + f.description).casefold()]
            selected = feeds[:args.limit]
            if args.diagnostics:
                print(json.dumps({'feeds': [f.public() for f in selected],
                                  'sources': discovery.reports, 'warnings': discovery.warnings},
                                 ensure_ascii=False, indent=2))
            elif args.json:
                print(json.dumps([f.public() for f in selected], ensure_ascii=False, indent=2))
            else:
                for feed in selected:
                    print('%s\t%s\t%s\t%ss\t%s' % (feed.id, feed.nickname,
                          'historical' if feed.historical else 'active',
                          feed.media.get('videoPlayLen', '?'), feed.description.replace('\n', ' ')[:100]))
                if not selected:
                    print('No matching video records in selected sources. Use --diagnostics for coverage; '
                          'absence does not identify the current video or prove it cannot be downloaded.')
        else:
            if not (args.id or args.query or args.latest):
                raise DownloadError('Choose --id, --query, or explicitly --latest.')
            feed = choose_feed(feeds, args.id, args.query, args.latest)
            result = download(feed, args.output_dir, args.format, args.verify,
                              args.retries, args.overwrite, not args.quiet)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (StorageError, DownloadError, OSError) as exc:
        print('Error: %s' % exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('\nCancelled; incomplete temporary output removed.', file=sys.stderr)
        return 130


if __name__ == '__main__':
    sys.exit(main())
