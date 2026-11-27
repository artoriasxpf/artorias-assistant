"""Capture provenance from ordinary project tool results, without retaining file bodies."""
from ..project_analysis import resource_path

def targets(name, args):
    if name == 'project_read_many':
        return [(item['path'], False) for item in args['items']]
    if name in ('project_search', 'project_inventory'):
        if args.get('inventory_id'):
            raise ValueError('持久分析请指定 root，便于恢复并核对读取范围')
        return [(args.get('root') or '.', True)]
    return [(args.get('path', '.'), name != 'project_read')]


def receipts(name, args, value, target):
    paths = targets(name, args)
    rows = []
    for index, (path, directory) in enumerate(paths):
        item = value['items'][index] if name == 'project_read_many' else value
        status = item.get('status', 'permission_denied' if item.get('rejected') else 'ok')
        if directory and item.get('stop_reason') in ('cursor_expired', 'unsupported', 'permission_denied'):
            status = item['stop_reason']
        kind = 'snippet' if name == 'project_search' else ('metadata' if directory else 'content')
        row = dict(target(path, directory), kind=kind, status=status)
        rows.append(row)
        if status != 'ok':
            if item.get('operation_id'):
                row['operation_id'] = item['operation_id']
            continue
        if not directory:
            if 'total_chars' in item:
                row.update(version=item['version'], unit='unicode_character', start=item['offset'],
                           end=item['offset'] + len(item['content']), total=item['total_chars'],
                           next_start=item['next_offset'])
            else:
                for key in ('version', 'unit', 'start', 'end', 'total', 'next_start', 'version_kind'):
                    if key in item:
                        row[key] = item[key]
                if 'end' not in row:
                    row['end'] = row['start'] + item['count']
            continue
        row.update(input_cursor=args.get('cursor'), next_cursor=item.get('next_cursor'),
                   incomplete=bool(item.get('incomplete') or item.get('truncated') or any(item.get('coverage', {}).get('skipped', {}).values())))
        if item.get('snapshot_id'):
            row['snapshot_id'] = item['snapshot_id']
        entries = item.get('entries', item.get('hits', item.get('samples', [])))
        sources = []
        for entry in entries:
            child = entry['path']
            if name != 'project_search' or args.get('snapshot_page'):
                child = resource_path(path, child)
            source = dict(target(child, entry.get('type') == 'directory'), kind=kind)
            if kind == 'snippet':
                source.update(line=entry.get('line'), version=entry.get('observation'))
            sources.append(source)
        row['sources'] = sources
    return rows


def legacy_calls(plan):
    """Replay a pending pre-refactor batch using the same project tool adapters."""
    calls = []
    for spec in plan['reads']:
        path, unit = spec['path'], spec.get('unit', 'byte')
        kind = spec.get('kind', 'range')
        if kind in ('inventory', 'search'):
            args = dict(root=path, snapshot_page=True, cursor=spec.get('cursor'))
            if kind == 'search':
                args['query'] = spec['query']
            calls.append(('project_' + kind, args))
        elif unit == 'byte':
            calls.append(('project_read', dict(path=path, unit=unit, offset=spec['offset'],
                         limit=spec['limit'], expected_version=spec.get('expected_version'))))
        else:
            item = dict(path=path, expected_version=spec.get('expected_version'))
            if unit == 'line':
                item.update(line_start=spec['offset'] or 1, line_count=min(120, spec['limit']))
            else:
                item.update(offset=spec['offset'], limit=min(1500, spec['limit']))
            calls.append(('project_read_many', {'items': [item]}))
    return calls
