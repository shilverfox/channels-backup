"""Explicit source registry, discovery diagnostics and whole-record selection."""

from dataclasses import dataclass, field

import channels_storage as storage
from channels_source_types import SourceResult, SourceSpec


def session_scan(root, include_history=False):
    feeds, warnings = storage.scan(root, include_history=include_history)
    for feed in feeds:
        feed.metadata_source = 'session'
        feed.provenance = [{'name': 'session', 'state': 'historical' if feed.historical else 'active',
                            'path': str(feed.source), 'record_sequence': feed.sequence,
                            'observed_at_ms': feed.updated}]
    return SourceResult(feeds, warnings, 'partial' if warnings else 'ok' if feeds else 'empty',
                        {'candidates': len(feeds)})


def preload_scan(root, include_history=False):
    from channels_preload import scan
    return scan(root, include_history=include_history)


SOURCES = (
    SourceSpec('session', 'Persisted session work objects', scanner=session_scan, priority=10),
    SourceSpec('preload', 'Native/worker IndexedDB detail preloads', scanner=preload_scan, priority=20,
               note='Verified after opening a self-forwarded card; not every navigation populates it.'),
    SourceSpec('http-cache', 'Unencrypted cached media requests/bodies', kind='media', status='limited',
               note='Use cache_video.py. Cache IDs cannot be merged with work IDs without identity evidence.'),
    SourceSpec('recommendation', 'Currently playing work in recommendation flow', status='research',
               note='No verified general metadata adapter. Do not substitute the newest cached item.'),
)


@dataclass
class Discovery:
    feeds: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    reports: list = field(default_factory=list)


def catalog(registry=None):
    return [s.public() for s in (SOURCES if registry is None else registry)]


def discover(root=storage.DEFAULT_ROOT, include_history=False, source_names=None, registry=None):
    registry = tuple(SOURCES if registry is None else registry)
    names = source_names or ['all']
    by_name = {s.name: s for s in registry}
    if 'all' in names and len(names) != 1:
        raise storage.StorageError('Use --source all alone, or specify individual metadata sources.')
    if names == ['all']:
        selected = [s for s in registry if s.kind == 'metadata' and s.status == 'ready' and s.scanner]
    else:
        selected = []
        for name in dict.fromkeys(names):
            spec = by_name.get(name)
            if not spec or spec.kind != 'metadata' or spec.status != 'ready' or not spec.scanner:
                raise storage.StorageError('Source %s is not an enabled metadata source; run sources --json.' % name)
            selected.append(spec)
    result = Discovery()
    ranked = {}
    origins = {}
    for spec in selected:
        try:
            scan = spec.scanner(root, include_history=include_history)
        except Exception:
            # A broken adapter must not disable the others or print raw parser/network data.
            scan = SourceResult(status='unavailable', warnings=[spec.name + ': source scan failed.'])
        result.warnings.extend(scan.warnings)
        result.reports.append({'name': spec.name, 'status': scan.status,
                               'candidates': len(scan.feeds), 'counts': scan.counts})
        for feed in scan.feeds:
            feed.metadata_source = spec.name
            if not feed.provenance:
                feed.provenance = [{'name': spec.name, 'state': 'historical' if feed.historical else 'active'}]
            origins.setdefault(feed.id, []).extend(feed.provenance)
            # Keep address/token/seed from ONE object, never splice fields across sources.
            rank = (not feed.historical, feed.updated, spec.priority, feed.sequence)
            if feed.id not in ranked or rank > ranked[feed.id][0]:
                ranked[feed.id] = (rank, feed)
    for _, feed in sorted(ranked.values(), key=lambda pair: pair[0], reverse=True):
        selected_origins = list(feed.provenance)
        unique = []
        for origin in origins[feed.id]:
            if origin not in unique:
                unique.append(origin)
        feed.provenance = [dict(origin, selected=origin in selected_origins) for origin in unique]
        result.feeds.append(feed)
    return result
