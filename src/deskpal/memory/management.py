"""统一整理的显式管理意图；编号仍为真实编号，不引入压缩协议。"""
from typing import Any


def requests(value: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, (list, tuple)) or len(value) > 8:
        raise ValueError('Invalid memory management requests')
    result = []
    for row in value:
        if (not isinstance(row, dict) or set(row) != {'action', 'quote', 'targets'}
                or row['action'] not in ('remember', 'correct', 'forget')
                or not isinstance(row['quote'], str) or not row['quote'].strip()
                or '\x00' in row['quote']
                or len(row['quote'].encode('utf-8')) > 8192
                or not isinstance(row['targets'], list) or len(row['targets']) > 8
                or any(not isinstance(key, str) or not key or len(key) > 256 for key in row['targets'])
                or len(set(row['targets'])) != len(row['targets'])
                or row['action'] == 'remember' and row['targets']):
            raise ValueError('Invalid memory management request')
        result.append(dict(action=row['action'], quote=row['quote'], targets=list(row['targets'])))
    return tuple(result)


def validate_result(data: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
    """模型判断含义，宿主约束本轮请求、实际目标及可证实的操作回执。"""
    wanted = requests(payload.get('memory_requests', []))
    review_ids = [row['id'] for row in data['reviews']]
    if len(review_ids) != len(set(review_ids)):
        raise ValueError('Each memory can only have one operation')
    reports = data.get('management', [])
    if not isinstance(reports, list) or len(reports) != len(wanted):
        raise ValueError('Each memory request needs a management result')
    seen: set[int] = set()
    for report in reports:
        if (not isinstance(report, dict) or set(report) != {'request', 'status', 'reason'}
                or type(report['request']) is not int or not 0 <= report['request'] < len(wanted)
                or report['request'] in seen or report['status'] not in ('applied', 'unchanged', 'clarify')
                or not isinstance(report['reason'], str) or not report['reason'].strip()
                or '\x00' in report['reason']
                or len(report['reason'].encode('utf-8')) > 2000):
            raise ValueError('Invalid memory management result')
        seen.add(report['request'])
        request = wanted[report['request']]
        targets = set(request['targets'])
        operations = [row for row in data['reviews'] if row['id'] in targets and row['decision'] != 'retain']
        if request['action'] != 'remember' and report['status'] == 'applied':
            expected = {'forget'} if request['action'] == 'forget' else {'update', 'adopt'}
            if not targets or {row['id'] for row in operations if row['decision'] in expected} != targets:
                raise ValueError('Management result lacks matching operations')
            if request['action'] == 'correct' and any('user' not in row['sources']
                    or row['quote'] not in payload.get('catalog', {}).get('user', {}).get('content', '') for row in operations):
                raise ValueError('Explicit correction requires current user evidence')
            if request['action'] == 'correct':
                for row in operations:
                    row['sources'] = ['user'] + [key for key in row['sources'] if key != 'user']
        if report['status'] != 'applied' and operations:
            raise ValueError('Unapplied management request must not change its targets')
        if request['action'] == 'remember' and report['status'] == 'applied' and not any(
                'user' in row['sources'] and row['quote'] in payload.get('catalog', {}).get('user', {}).get('content', '')
                for row in data['memories']):
            raise ValueError('Remember result lacks a new understanding')
    allowed_forget = {key for report in reports if report['status'] == 'applied'
                      for request in [wanted[report['request']]] if request['action'] == 'forget'
                      for key in request['targets']}
    forgotten = {row['id'] for row in data['reviews'] if row['decision'] == 'forget'}
    if forgotten != allowed_forget:
        raise ValueError('Forget requires an explicit current request and exact targets')
    if wanted:
        user = payload.get('catalog', {}).get('user', {})
        if (user.get('origin') != 'user_message' or user.get('speaker_id') != 'local:user'
                or user.get('previously_organized') or any(row['quote'] not in user.get('content', '') for row in wanted)):
            raise ValueError('Memory requests require current local user evidence')
    forgotten_sources = {row['original_source_key'] for row in payload.get('understandings', []) if row['id'] in forgotten}
    for row in [*data['memories'], *data['reviews'], *data['habits'], *data.get('followups', [])]:
        if row.get('decision') == 'forget':
            continue
        if (forgotten_sources.intersection(row['sources']) or row.get('target') in forgotten
                or row.get('buffer_id') in forgotten):
            raise ValueError('Forgotten evidence cannot support another operation')
    if data['emotion'] is not None and forgotten_sources.intersection(data['emotion']['sources']):
        raise ValueError('Forgotten evidence cannot support emotion')
    return reports
