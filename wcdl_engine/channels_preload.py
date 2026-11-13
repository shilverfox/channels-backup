"""Read only the Channels app's persisted IndexedDB detail caches."""

from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import struct
import time

import channels_storage as storage
from channels_source_types import SourceResult


APP_ID = 'wxalite28e098851253e34f4777f7ca024236fd'
APP_DATABASE = 'liteapp.' + APP_ID
DATABASES = {APP_DATABASE, 'finder:feed'}
TTL_MS = 3600000
MAX_FILE_BYTES = 128 * 1024 * 1024


class OversizedValue(ValueError):
    pass


def quiet_decode(function, *args):
    with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
        return function(*args)


def decode_value(key, data):
    _, offset = storage.varint(data)
    if data[offset:offset + 3] == b'\xff\x11\x02':
        import cramjam
        size = cramjam.snappy.decompress_raw_len(data[offset + 3:])
        if size > storage.MAX_BLOCK:
            raise OversizedValue('Serialized value exceeds the supported bound.')
    return quiet_decode(key.ParseValue, data)


def decoder():
    # Lazy loading lets other sources remain usable without this optional parser.
    from dfindexeddb.indexeddb.chromium import record
    return record


def read_entries(directory, counts):
    latest, puts = {}, {}
    for path in sorted(directory.iterdir()):
        if path.suffix not in ('.log', '.ldb', '.sst'):
            continue
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                counts['oversized_files'] += 1
                continue
            data = path.read_bytes()
            entries = (list(storage.table_entries(data)) if path.suffix != '.log' else
                       [e for batch in storage.log_records(data) for e in storage.batch_entries(batch)])
            counts['files_read'] += 1
            if path.suffix == '.log' and data.strip(b'\x00') and not entries:
                counts['incomplete_log_files'] += 1
            for entry in entries:
                counts['records_read'] += 1
                snapshot = (entry, path)
                if entry.key not in latest or entry.sequence > latest[entry.key][0].sequence:
                    latest[entry.key] = snapshot
                if not entry.deleted and (entry.key not in puts or entry.sequence > puts[entry.key][0].sequence):
                    puts[entry.key] = snapshot
        except (OSError, ValueError, struct.error):
            counts['storage_errors'] += 1
    return latest, puts


def parse_json(value):
    return json.loads(value) if isinstance(value, str) else value


def worker_row(wrapper, expected_id):
    """finder/27 QueueStore persists {id, ts, value: CGI response}."""
    wrapper = parse_json(wrapper)
    if not isinstance(wrapper, dict):
        return None, 'unsupported'
    if any(key in wrapper for key in ('id', 'ts', 'value')):
        if str(wrapper.get('id')) != expected_id:
            return None, 'identity_mismatch'
        response = wrapper.get('value')
        if not isinstance(response, dict):
            return None, 'unsupported'
        return {'createTime': wrapper.get('ts'), 'data': response}, None
    # Retain compatibility with the earlier timestamp/data cache representation.
    return wrapper, None


def cached_feed(wrapper, source, sequence, expected_id=None, deleted=False,
                include_history=False, now_ms=None):
    """Normalize the verified native/worker response wrapper, not arbitrary strings."""
    wrapper = parse_json(wrapper)
    if not isinstance(wrapper, dict):
        return None, 'unsupported'
    created = wrapper.get('createTime', wrapper.get('timestamp'))
    if isinstance(created, bool) or not isinstance(created, (int, float)) or created <= 0:
        return None, 'unsupported'
    now_ms = now_ms if now_ms is not None else time.time() * 1000
    if created > now_ms + 300000:
        return None, 'unsupported'
    expired = now_ms - created > TTL_MS
    if (deleted or expired) and not include_history:
        return None, 'deleted' if deleted else 'expired'
    value = wrapper.get('data')
    # Native cache: data.data.object. Worker cache may store data.object.
    if not isinstance(value, dict):
        return None, 'unsupported'
    if value.get('errCode', 0) != 0:
        return None, 'unsupported'
    response = value.get('data', value)
    if not isinstance(response, dict):
        return None, 'unsupported'
    base = response.get('baseResponse', response.get('BaseResponse'))
    if base is not None and (not isinstance(base, dict) or base.get('Ret', base.get('ret', 0))):
        return None, 'unsupported'
    obj = response.get('object')
    if not isinstance(obj, dict):
        return None, 'unsupported'
    if expected_id is not None and str(obj.get('id')) != str(expected_id):
        return None, 'identity_mismatch'
    feed = storage.decode_feed(json.dumps(obj, ensure_ascii=False).encode(), source, sequence)
    if feed is None:
        return None, 'unsupported'
    feed.updated = created
    feed.historical = deleted or expired
    feed.metadata_source = 'preload'
    state = 'deleted' if deleted else 'expired' if expired else 'active'
    feed.provenance = [{'name': 'preload', 'state': state, 'observed_at_ms': created,
                        'path': str(source), 'record_sequence': sequence}]
    return feed, state



def blob_payload(db_dir):
    """读取 IndexedDB 外置 blob（值变大后 Blink 会把它挪出 LevelDB）。"""
    import cramjam
    blob_dir = db_dir.parent / db_dir.name.replace(".leveldb", ".blob")
    if not blob_dir.is_dir():
        return None
    files = [p for p in blob_dir.rglob("*") if p.is_file()]
    if not files:
        return None
    newest = max(files, key=lambda p: p.stat().st_mtime)
    try:
        data = newest.read_bytes()
    except OSError:
        return None
    if data[:3] == b"\xff\x11\x02":
        try:
            data = bytes(cramjam.snappy.decompress_raw(data[3:]))
        except Exception:
            return None
    start = data.find(b"{\x00")
    if start < 0:
        return None
    try:
        text = data[start:].decode("utf-16-le", "ignore").rstrip("\x00")
        payload = json.loads(text)
    except (ValueError, UnicodeError):
        return None
    return payload if isinstance(payload, dict) else None


def scan(root=storage.DEFAULT_ROOT, include_history=False, now_ms=None):
    result = SourceResult()
    counts = Counter()
    try:
        record = decoder()
    except ImportError:
        return SourceResult(status='unavailable', warnings=[
            'preload: IndexedDB parser missing; run channels.py with uv to prepare its pinned dependencies.'])
    now_ms = now_ms if now_ms is not None else time.time() * 1000
    for profile in storage.profiles(root):
        directory = profile / 'IndexedDB/weixin_xworker_0.indexeddb.leveldb'
        if not directory.is_dir():
            continue
        counts['databases'] += 1
        latest, puts = read_entries(directory, counts)
        names, parsed_keys = {}, {}
        # Resolve numeric database IDs from live metadata; they vary by installation.
        for entry, path in latest.values():
            if entry.deleted:
                continue
            try:
                key = quiet_decode(record.IndexedDbKey.FromBytes, entry.key)
                parsed_keys[entry.key] = key
            except Exception:
                counts['key_parse_errors'] += 1
                continue
            if isinstance(key, record.DatabaseNameKey) and key.database_name in DATABASES:
                try:
                    names[quiet_decode(key.ParseValue, entry.value)] = key.database_name
                except Exception:
                    counts['metadata_parse_errors'] += 1
        for raw_key, (entry, path) in latest.items():
            deleted = entry.deleted
            if deleted:
                if not include_history or raw_key not in puts:
                    continue
                entry, path = puts[raw_key]
            key = parsed_keys.get(raw_key)
            if key is None and deleted:
                try:
                    key = quiet_decode(record.IndexedDbKey.FromBytes, raw_key)
                except Exception:
                    counts['key_parse_errors'] += 1
            if not isinstance(key, record.ObjectStoreDataKey):
                continue
            name = names.get(key.key_prefix.database_id)
            user_key = key.encoded_user_key.value
            native = name == APP_DATABASE and user_key == 'feed_cache_info'
            worker = name == 'finder:feed'
            if not (native or worker):
                continue
            counts['selected_rows'] += 1
            try:
                value = decode_value(key, entry.value)
                if value.blob_size is not None:
                    payload = blob_payload(directory) if native else None
                    if payload is None:
                        counts['external_blobs_unsupported'] += 1
                        continue
                else:
                    payload = parse_json(value.value)
                if native:
                    if not isinstance(payload, dict):
                        counts['unsupported'] += 1
                        continue
                    items = payload.items()
                else:
                    items = [(str(user_key), payload)]
                for fid, wrapper in items:
                    if not str(fid).isdigit():
                        counts['unsupported'] += 1
                        continue
                    if worker:
                        wrapper, error = worker_row(wrapper, str(fid))
                        if error:
                            counts[error] += 1
                            continue
                    feed, state = cached_feed(wrapper, path, entry.sequence, str(fid), deleted,
                                              include_history, now_ms)
                    counts['cache_items'] += 1
                    counts[state] += 1
                    if feed:
                        feed.provenance[0].update(database=name,
                                                  cache_key='feed_cache_info' if native else 'feed')
                        result.feeds.append(feed)
            except OversizedValue:
                counts['oversized_values'] += 1
            except Exception:
                # Never expose library exception text: malformed values can contain credentials.
                counts['value_parse_errors'] += 1
    result.counts = dict(counts)
    if any(counts[k] for k in ('storage_errors', 'oversized_files', 'external_blobs_unsupported',
                              'unsupported', 'identity_mismatch', 'value_parse_errors',
                              'key_parse_errors', 'metadata_parse_errors', 'incomplete_log_files',
                              'oversized_values')):
        result.status = 'partial'
        result.warnings.append('preload: some records were unreadable or unsupported; see --diagnostics counts.')
    elif not counts['databases']:
        result.status = 'absent'
    elif not result.feeds:
        result.status = 'empty'
    return result
