"""Read Chromium Session Storage without opening or changing its LevelDB."""

import json
import struct
from dataclasses import dataclass, field
from pathlib import Path


DEFAULT_ROOT = (
    Path.home() / 'Library/Containers/com.tencent.xinWeChat/Data/Documents'
    / 'app_data/radium/web/profiles'
)
TABLE_MAGIC = bytes.fromhex('57fb808b247547db')
MAX_BLOCK = 64 * 1024 * 1024


def _crc_table():
    result = []
    for value in range(256):
        for _ in range(8):
            value = (value >> 1) ^ (0x82f63b78 if value & 1 else 0)
        result.append(value)
    return result


CRC_TABLE = _crc_table()


def masked_crc(data):
    crc = 0xffffffff
    for byte in data:
        crc = CRC_TABLE[(crc ^ byte) & 255] ^ (crc >> 8)
    crc ^= 0xffffffff
    return (((crc >> 15) | (crc << 17)) + 0xa282ead8) & 0xffffffff


class StorageError(ValueError):
    pass


@dataclass
class Entry:
    key: bytes
    value: bytes
    sequence: int
    deleted: bool = False


@dataclass
class Feed:
    id: str
    nickname: str
    description: str
    media: dict
    updated: float
    source: Path
    sequence: int
    historical: bool = False
    metadata_source: str = ''
    provenance: list = field(default_factory=list, repr=False)

    def public(self):
        result = {
            'id': self.id, 'nickname': self.nickname,
            'description': self.description,
            'cache_state': 'historical' if self.historical else 'active',
            'duration_seconds': self.media.get('videoPlayLen'),
            'formats': [
                {key: spec[key] for key in (
                    'fileFormat', 'codingFormat', 'width', 'height',
                    'videoBitrate', 'vfps', 'durationMs',
                ) if key in spec}
                for spec in self.media.get('spec', []) if isinstance(spec, dict)
            ],
        }
        if self.metadata_source:
            result['metadata_source'] = self.metadata_source
            result['sources'] = self.provenance
        return result


def varint(data, pos=0):
    value = 0
    for shift in range(0, 70, 7):
        if pos >= len(data):
            raise StorageError('Truncated varint')
        byte = data[pos]
        pos += 1
        value |= (byte & 127) << shift
        if byte < 128:
            return value, pos
    raise StorageError('Oversized varint')


def block_handle(data, pos=0):
    offset, pos = varint(data, pos)
    length, pos = varint(data, pos)
    return offset, length, pos


def read_block(data, offset, length):
    if length > MAX_BLOCK or offset < 0 or offset + length + 5 > len(data):
        raise StorageError('Invalid table block bounds')
    payload = data[offset:offset + length]
    compression = data[offset + length]
    checksum = struct.unpack_from('<I', data, offset + length + 1)[0]
    if masked_crc(data[offset:offset + length + 1]) != checksum:
        raise StorageError('Table block checksum mismatch')
    if compression == 0:
        return payload
    if compression == 1:
        try:
            import cramjam
        except ImportError as exc:
            raise StorageError('Missing cramjam; run with uv or install requirements.txt') from exc
        try:
            size = cramjam.snappy.decompress_raw_len(payload)
            if size > MAX_BLOCK:
                raise StorageError('Decompressed block is too large')
            return bytes(cramjam.snappy.decompress_raw(payload))
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError('Invalid Snappy table block') from exc
    raise StorageError('Unsupported table compression: %s' % compression)


def block_entries(data):
    if len(data) < 4:
        raise StorageError('Missing table restart array')
    count = struct.unpack_from('<I', data, len(data) - 4)[0]
    limit = len(data) - 4 - count * 4
    if limit < 0:
        raise StorageError('Invalid table restart array')
    pos, previous = 0, b''
    while pos < limit:
        shared, pos = varint(data, pos)
        nonshared, pos = varint(data, pos)
        size, pos = varint(data, pos)
        if shared > len(previous) or pos + nonshared + size > limit:
            raise StorageError('Invalid table entry bounds')
        key = previous[:shared] + data[pos:pos + nonshared]
        pos += nonshared
        value = data[pos:pos + size]
        pos += size
        previous = key
        yield key, value


def table_entries(data):
    if len(data) < 48 or data[-8:] != TABLE_MAGIC:
        raise StorageError('Invalid LevelDB table footer')
    _, _, pos = block_handle(data, len(data) - 48)
    offset, length, _ = block_handle(data, pos)
    for _, handle in block_entries(read_block(data, offset, length)):
        offset, length, _ = block_handle(handle)
        for key, value in block_entries(read_block(data, offset, length)):
            if len(key) < 8:
                raise StorageError('Missing internal sequence')
            tag = int.from_bytes(key[-8:], 'little')
            if tag & 255 not in (0, 1):
                raise StorageError('Invalid table entry type')
            yield Entry(key[:-8], value, tag >> 8, tag & 255 == 0)


def log_records(data):
    """Reassemble LevelDB 32 KiB physical fragments before parsing JSON."""
    pending = None
    for base in range(0, len(data), 32768):
        end = min(base + 32768, len(data))
        pos = base
        while pos + 7 <= end:
            checksum, length, kind = struct.unpack_from('<IHB', data, pos)
            pos += 7
            if kind == 0 and length == 0:
                break
            if pos + length > end:
                pending = None
                break
            fragment = data[pos:pos + length]
            pos += length
            if masked_crc(bytes([kind]) + fragment) != checksum:
                raise StorageError('Log fragment checksum mismatch')
            if kind == 1:
                pending = None
                yield fragment
            elif kind == 2:
                pending = bytearray(fragment)
            elif kind == 3 and pending is not None:
                pending.extend(fragment)
            elif kind == 4 and pending is not None:
                pending.extend(fragment)
                yield bytes(pending)
                pending = None
            else:
                pending = None


def batch_entries(data):
    if len(data) < 12:
        raise StorageError('Truncated write batch')
    sequence, count = struct.unpack_from('<QI', data)
    pos = 12
    if count > len(data):
        raise StorageError('Invalid write batch count')
    for index in range(count):
        if pos >= len(data):
            raise StorageError('Truncated write batch entry')
        kind = data[pos]
        pos += 1
        if kind not in (0, 1):
            raise StorageError('Unsupported write batch entry')
        size, pos = varint(data, pos)
        if pos + size > len(data):
            raise StorageError('Truncated write batch key')
        key = data[pos:pos + size]
        pos += size
        value = b''
        if kind == 1:
            size, pos = varint(data, pos)
            if pos + size > len(data):
                raise StorageError('Truncated write batch value')
            value = data[pos:pos + size]
            pos += size
        yield Entry(key, value, sequence + index, kind == 0)


def decode_feed(value, source, sequence):
    candidates = [value]
    if value[:1] in (b'\x00', b'\x01'):
        candidates.append(value[1:])
    for payload in candidates:
        for encoding in ('utf-16le', 'utf-8', 'latin-1'):
            try:
                item = json.loads(payload.decode(encoding).lstrip('\ufeff'))
            except (UnicodeError, ValueError, RecursionError):
                continue
            if not isinstance(item, dict):
                continue
            desc = item.get('objectDesc')
            if not isinstance(desc, dict) or not str(item.get('id', '')).isdigit():
                continue
            items = desc.get('media')
            if not isinstance(items, list):
                continue
            media = next((m for m in items
                          if isinstance(m, dict) and m.get('url')
                          and (m.get('videoPlayLen') or m.get('spec'))), None)
            if media is None:
                continue
            contact = item.get('contact') or {}
            if not isinstance(contact, dict):
                contact = {}
            times = []
            for key in ('reqTime', 'lastUpdateMediaTime'):
                try:
                    times.append(float(item.get(key) or 0))
                except (ValueError, TypeError):
                    pass
            return Feed(str(item['id']), str(contact.get('nickname') or ''),
                        str(desc.get('description') or ''), media,
                        max(times, default=0), source, sequence)
    return None


def profiles(root=DEFAULT_ROOT):
    root = Path(root).expanduser()
    if root.name == 'Session Storage':
        return [root.parent]
    if (root / 'Session Storage').is_dir() or (root / 'IndexedDB').is_dir():
        return [root]
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir()
                  and ((p / 'Session Storage').is_dir() or (p / 'IndexedDB').is_dir()))


def scan(root=DEFAULT_ROOT, include_history=False):
    root = Path(root).expanduser()
    if root.name == 'Session Storage':
        directories = [root]
    else:
        directories = [p / 'Session Storage' for p in profiles(root)
                       if (p / 'Session Storage').is_dir()]
    feeds, warnings = {}, []
    ranks = {}
    if not directories:
        raise StorageError('No Session Storage found. Play the video in desktop WeChat first; '
                           'use --profiles-root if WeChat stores data elsewhere.')
    for directory in directories:
        latest = {}
        history = {}
        for source in sorted(directory.iterdir()):
            if source.suffix not in ('.log', '.ldb', '.sst'):
                continue
            try:
                data = source.read_bytes()
                if source.suffix == '.log':
                    records = []
                    for record in log_records(data):
                        records.extend(batch_entries(record))
                else:
                    records = list(table_entries(data))
                for entry in records:
                    # Keep deletions too: an older table must not resurrect a deleted key.
                    if not entry.key.startswith(b'map-'):
                        continue
                    previous = latest.get(entry.key)
                    snapshot = (entry, source, source.stat().st_mtime)
                    if previous is None or entry.sequence > previous[0].sequence:
                        latest[entry.key] = snapshot
                    if include_history and not entry.deleted:
                        previous = history.get(entry.key)
                        if previous is None or entry.sequence > previous[0].sequence:
                            history[entry.key] = snapshot
            except (OSError, StorageError, struct.error) as exc:
                warnings.append('%s: %s' % (source, exc))
        for key, (entry, source, mtime) in latest.items():
            historical = entry.deleted
            if historical:
                if not include_history or key not in history:
                    continue
                # A removed session is not proof that its CDN token has expired.
                entry, source, mtime = history[key]
            feed = decode_feed(entry.value, source, entry.sequence)
            if feed is None:
                continue
            feed.historical = historical
            previous = feeds.get(feed.id)
            rank = (not historical, feed.updated, mtime, feed.sequence)
            if previous is None or rank > ranks[feed.id]:
                feeds[feed.id] = feed
                ranks[feed.id] = rank
    return sorted(feeds.values(), key=lambda f: ranks[f.id], reverse=True), warnings
