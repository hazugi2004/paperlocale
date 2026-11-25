"""按唯一原生 PDF 操作核验、删除和重放行内公式的实线。

支持页面内容流中的水平 m/l/S 路径、灰度或 RGB 描边和轴对齐等比变换。
带裁切、自定义图形状态或可选显示内容的路径不支持移动；只接受逐字段
核验的中性 ExtGState 与仅含元数据的 PlacedPDF。重放在页面内容末尾，
后续像素与固定墨迹检查仍须通过。
"""
from collections import Counter, defaultdict
from decimal import Decimal

import pymupdf as fitz

from .source_watermarks import set_page_stream
from .vector_precision import _float32, _multiply, _operations


_EXTRA = {op.encode() for op in (
    'm l c v y h S s f F f* B B* b b* n W W* w J j M d G RG K '
    'g rg k CS cs SC SCN sc scn ri i sh BMC BDC EMC MP DP BX EX '
    'd0 d1').split()} | {b'INLINE IMAGE'}
_PATH = {b'm', b'l', b'c', b'v', b'y', b'h', b're'}
_PAINT = {b'S', b's', b'f', b'F', b'f*', b'B', b'B*', b'b', b'b*', b'n'}
_FIELDS = ('items', 'rect', 'type', 'color', 'fill', 'width', 'lineCap',
           'lineJoin', 'dashes', 'even_odd', 'closePath', 'fill_opacity',
           'stroke_opacity', 'layer')
_DEFAULTS = {'w': ([1.0], '1 w'), 'J': ([0], '0 J'), 'j': ([0], '0 j'),
             'M': ([10.0], '10 M'), 'd': ([[], 0.0], '[] 0 d'),
             'color': (['gray', 0.0], '0 G')}


# 实证GS1的完整状态，不把SA=true当成可丢弃的默认值。GS0仅OP/op为true，
# 可识别其完整状态供后续GS1显式替换，但绝不允许在overprint状态下选线。
_GS_FIELDS = {'Type': ['name', '/ExtGState'], 'SMask': ['name', '/None'],
              'AIS': ['bool', 'false'], 'BM': ['name', '/Normal'],
              'OPM': ['number', '1'], 'CA': ['number', '1'], 'ca': ['number', '1'],
              'SA': ['bool', 'true'], 'OP': ['bool', 'false'], 'op': ['bool', 'false']}


def _resource_xref(page, category, name):
    if not isinstance(name, str) or not name.startswith('/') or '/' in name[1:]:
        return None
    kind, value = page.parent.xref_get_key(page.xref, 'Resources/'+category+'/'+name[1:])
    return int(value.split()[0]) if kind == 'xref' else None


def _extgstate(page, name):
    ref = _resource_xref(page, 'ExtGState', name)
    if ref is None or set(page.parent.xref_get_keys(ref)) != set(_GS_FIELDS):
        return None
    fields = {k: list(page.parent.xref_get_key(ref, k)) for k in _GS_FIELDS}
    # 原生源CA/ca为real 1，保存后可成为integer 1；只规范同一个精确数值，
    # 不把接近1的不透明度当作1，也不忽略其余字段或布尔/名称类型。
    for key in ('OPM', 'CA', 'ca'):
        kind, value = fields[key]
        if kind in ('int', 'float') and Decimal(value) == 1:
            fields[key] = ['number', '1']
    overprint = fields['OP']
    if (overprint not in (['bool', 'false'], ['bool', 'true'])
            or fields['op'] != overprint
            or any(fields[k] != v for k, v in _GS_FIELDS.items() if k not in ('OP', 'op'))):
        return None
    return {'name': str(name), 'fields': fields}


def _metadata_mark(page, args):
    # 只承认实际资源字典中的Metadata-only PlacedPDF包装。Metadata是非绘制
    # 的制作工具/时间记录，不复制新资源；OC、ActualText、MCID等分组仍拒绝。
    if len(args) != 2 or args[0] != '/PlacedPDF':
        return False
    ref = _resource_xref(page, 'Properties', args[1])
    if ref is None or set(page.parent.xref_get_keys(ref)) != {'Metadata'}:
        return False
    kind, value = page.parent.xref_get_key(ref, 'Metadata')
    if kind != 'xref':
        return False
    metadata = int(value.split()[0])
    return (page.parent.xref_is_stream(metadata)
            and page.parent.xref_get_key(metadata, 'Type') == ('name', '/Metadata')
            and page.parent.xref_get_key(metadata, 'Subtype') == ('name', '/XML'))


def _json(value):
    if isinstance(value, (tuple, list, fitz.Point, fitz.Rect)):
        return [_json(item) for item in value]
    return value


def _freeze(value):
    return tuple(map(_freeze, value)) if isinstance(value, list) else value


def _drawing(drawing):
    return {key: _json(drawing.get(key)) for key in _FIELDS}


def _drawing_key(drawing):
    return tuple(_freeze(_json(drawing.get(key))) for key in _FIELDS)


def _identity(stroke):
    style = stroke['style']
    key = (tuple(map(_float32, stroke['operands'])),
           tuple(map(_float32, stroke['ctm'])),
           tuple(_float32(style[k]) for k in ('width', 'line_cap', 'line_join', 'miter')),
           style['color_space'], tuple(map(_float32, style['color'])))
    gs = style.get('extgstate')
    return key if gs is None else key + ((gs['name'], tuple(
        (name, *value) for name, value in sorted(gs['fields'].items()))),)


def _records(page, data=None):
    """枚举候选原生线段及其字节区间，保留重复项供唯一性校验。"""
    data = page.read_contents() if data is None else data
    state = {'styles': _DEFAULTS.copy(), 'matrices': [], 'blocked': False,
             'clip': False, 'extgstate': None, 'extgstate_raw': None,
             'device_ctm': tuple(page.transformation_matrix)}
    stack, path, records = [], [], []
    marked = []
    for args, op, start, end, ctm in _operations(data, extra=_EXTRA):
        raw = data[start:end].decode('latin1')
        if op == b'q':
            stack.append({**state, 'styles': state['styles'].copy(),
                          'matrices': state['matrices'][:]})
        elif op == b'Q':
            state = stack.pop()
        elif op == b'cm':
            state['matrices'].append(raw)
            # 以页面裁切矩阵为起点逐次应用 cm，复现 MuPDF 每步 float32 取整。
            state['device_ctm'] = _multiply(tuple(map(_float32, args)), state['device_ctm'])
        elif op in (b'w', b'J', b'j', b'M', b'd'):
            values = ([list(map(float, args[0])), float(args[1])] if op == b'd'
                      else list(map(float, args)))
            state['styles'][op.decode()] = values, raw
        elif op in (b'G', b'RG'):
            state['styles']['color'] = (['gray' if op == b'G' else 'rgb',
                                        *map(float, args)], raw)
        elif op in (b'K', b'CS', b'SC', b'SCN'):
            # 后出现的 G/RG 可重设当前描边色彩空间。
            state['styles']['color'] = (['unsupported'], raw)
        elif op == b'gs':
            gs = _extgstate(page, args[0])
            if gs is None:
                state['blocked'] = True
            else:
                # 完整GS1能替换已知GS0的overprint字段，但不能消除ri/i或未知
                # gs留下的独立阻塞。q/Q仍恢复各自原图形状态。
                state['extgstate'] = gs
                state['extgstate_raw'] = raw
        elif op in (b'ri', b'i'):
            state['blocked'] = True
        elif op in (b'BMC', b'BDC'):
            marked.append(op == b'BDC' and _metadata_mark(page, args))
        elif op == b'EMC':
            if not marked:
                raise ValueError('Unbalanced inline marked content')
            marked.pop()
        elif op in (b'W', b'W*'):
            state['clip'] = True
        elif op == b'Tr' and float(args[0]) >= 4:
            state['clip'] = True

        if op in _PATH:
            path.append((op, list(map(float, args)), start, end, tuple(ctm)))
        elif op in _PAINT:
            if op == b'S' and [part[0] for part in path] == [b'm', b'l']:
                styles = {key: value[0] for key, value in state['styles'].items()}
                a, b, c, d, _, _ = ctm
                supported = (not state['blocked'] and not state['clip'] and all(marked)
                             and (state['extgstate'] is None or
                                  state['extgstate']['fields']['OP'] == ['bool', 'false'])
                             and styles['d'] == [[], 0.0]
                             and styles['color'][0] in ('gray', 'rgb')
                             and a == d and a != 0 and b == c == 0
                             and styles['J'][0] in (0, 1, 2)
                             and styles['j'][0] in (0, 1, 2)
                             and styles['w'][0] > 0 and styles['M'][0] > 0
                             and all(part[4] == ctm for part in path))
                operands = path[0][1] + path[1][1]
                # 路径操作之间的图形状态、文字及其他内容不能包含在删除范围内。
                if supported and operands[1] == operands[3] and operands[0] != operands[2]:
                    matrix = fitz.Matrix(*state['device_ctm'])
                    points = [fitz.Point(*map(_float32, operands[i:i+2])) * matrix
                              for i in (0, 2)]
                    if points[0].y == points[1].y:
                        color = styles['color'][1:]
                        color = color * 3 if styles['color'][0] == 'gray' else color
                        width = _float32(_float32(styles['w'][0]) * abs(a))
                        rect = fitz.Rect(points[0], points[1])
                        rect.normalize()
                        drawing = {'items': [['l', list(points[0]), list(points[1])]],
                                   'rect': list(rect), 'type': 's',
                                   'color': list(map(_float32, color)), 'fill': None,
                                   'width': width, 'lineCap': [int(styles['J'][0])] * 3,
                                   # PyMuPDF 的绘图报告将线连接枚举乘以路径缩放。
                                   'lineJoin': _float32(styles['j'][0] * abs(a)), 'dashes': '[] 0',
                                   'even_odd': None, 'closePath': False,
                                   'fill_opacity': None, 'stroke_opacity': 1.0, 'layer': ''}
                        cap = width/2 if styles['J'][0] else 0
                        ink = [rect.x0-cap, rect.y0-width/2, rect.x1+cap, rect.y1+width/2]
                        native_style = {
                            'width': styles['w'][0], 'line_cap': styles['J'][0],
                            'line_join': styles['j'][0], 'miter': styles['M'][0],
                            'color_space': styles['color'][0], 'color': styles['color'][1:]}
                        native_state = [state['styles'][k][1] for k in _DEFAULTS]
                        if state['extgstate'] is not None:
                            native_style['extgstate'] = state['extgstate']
                            native_state.append(state['extgstate_raw'])
                        records.append({'page': page.number+1, 'operands': operands,
                                        'ctm': list(ctm),
                                        'page_matrix': list(page.transformation_matrix),
                                        'device_ctm': list(state['device_ctm']), 'style': native_style,
                                        'commands': {
                                            'state': native_state,
                                            'ctm': state['matrices'][:],
                                            'path': [data[p[2]:p[3]].decode('latin1') for p in path]+[raw]},
                                        'drawing': drawing, 'rect': ink,
                                        '_ranges': [(p[2], p[3]) for p in path]+[(start, end)]})
            path = []
        elif path:
            # m 到 S 之间夹有图形状态或文字，即不能认作独立可移动笔画。
            path.append((op, [], start, end, tuple(ctm)))
    if marked:
        raise ValueError('Unbalanced inline marked content')
    return records


def collect_strokes(page):
    """仅返回在原操作和实际绘图列表中都唯一的实线。"""
    records = _records(page)
    identities = Counter(_identity(record) for record in records)
    geometries = Counter(_drawing_key(record['drawing']) for record in records)
    actual = Counter(_drawing_key(drawing) for drawing in page.get_drawings())
    return [{**{key: value for key, value in record.items() if key != '_ranges'},
             'matched': True}
            for record in records if identities[_identity(record)] == 1
            and geometries[_drawing_key(record['drawing'])] == 1
            and actual[_drawing_key(record['drawing'])] == 1]


def select_strokes(page, drawings):
    """将指定绘图一对一绑定到唯一原生实线。"""
    available = collect_strokes(page)
    selected, used = [], set()
    for drawing in drawings:
        matches = [stroke for stroke in available
                   if _drawing_key(stroke['drawing']) == _drawing_key(drawing)]
        if len(matches) != 1 or _identity(matches[0]) in used:
            raise ValueError('Inline stroke is unsupported, duplicated or ambiguous')
        selected.append(matches[0])
        used.add(_identity(matches[0]))
    return selected


def remove_strokes(pdf_bytes, anchors):
    """从已清除正文的源层删除已验证的笔画，并复核其余绘图和字节。"""
    declared = defaultdict(list)
    for anchor in anchors:
        for stroke in anchor.get('strokes', []):
            if not stroke.get('matched') or stroke['page'] != anchor['page']:
                raise ValueError('Inline stroke lacks its source-page match')
            declared[anchor['page']].append(stroke)
    if not declared:
        return pdf_bytes, []
    evidence, expected = [], {}
    with fitz.open(stream=pdf_bytes, filetype='pdf') as document:
        for number, strokes in declared.items():
            if len({_identity(stroke) for stroke in strokes}) != len(strokes):
                raise ValueError('Duplicate declared inline stroke')
            page = document[number-1]
            data = page.read_contents()
            records = _records(page)
            before = Counter(_drawing_key(drawing) for drawing in page.get_drawings())
            ranges = []
            for stroke in strokes:
                matches = [record for record in records if _identity(record) == _identity(stroke)]
                if len(matches) != 1 or before[_drawing_key(matches[0]['drawing'])] != 1:
                    raise ValueError('Inline source stroke identity is missing or ambiguous')
                record = matches[0]
                # Native operands, effective CTM and style must agree at float32
                # precision. A merely nearby line is never an alternative match.
                if list(page.transformation_matrix) != stroke['page_matrix']:
                    raise ValueError('Inline source stroke page transform changed')
                before.subtract({_drawing_key(record['drawing']): 1})
                ranges.extend(record['_ranges'])
                evidence.append({'page': number, 'source': stroke['drawing'],
                                 'removed_drawing': record['drawing'],
                                 'source_native': {k: stroke[k] for k in ('operands', 'ctm', 'style')},
                                 'removed_native': {k: record[k] for k in ('operands', 'ctm', 'style')},
                                 'operation_ranges': _json(record['_ranges']),
                                 'other_content_bytes_unchanged': True,
                                 'other_drawings_unchanged': True,
                                 'match': 'native-operands-ctm-style-float32', 'removed': True})
            chunks, cursor = [], 0
            for start, end in sorted(ranges):
                chunks.extend((data[cursor:start], b'\n'))
                cursor = end
            changed = b''.join(chunks) + data[cursor:]
            set_page_stream(page, changed)
            expected[number] = changed, +before
        result = document.tobytes(garbage=0, deflate=True)
    with fitz.open(stream=result, filetype='pdf') as persisted:
        for number, (data, drawings) in expected.items():
            page = persisted[number-1]
            if page.read_contents() != data or Counter(
                    _drawing_key(drawing) for drawing in page.get_drawings()) != drawings:
                raise ValueError('Inline stroke removal changed unrelated native graphics')
    for strokes in declared.values():
        for stroke in strokes:
            stroke['removed'] = True
    return result, evidence


def _replay(page, stroke, shift):
    gs = stroke['style'].get('extgstate')
    # 重放原资源名，不复制/覆盖目标资源；同名字段有任何差异就拒绝。
    if gs is not None and (gs['fields']['OP'] != ['bool', 'false']
                           or _extgstate(page, gs['name']) != gs):
        raise ValueError('Inline stroke ExtGState resource is missing or changed')
    inverse = ~page.transformation_matrix
    dx, dy = map(float, shift)
    tx, ty = dx*inverse.a + dy*inverse.c, dx*inverse.b + dy*inverse.d
    def number(value):
        # 仅规范新位移：短double十进制在float32中点附近可能被MuPDF直接
        # 解析到另一邻值。先明确float32，再写其精确有限小数，使原生绘制
        # 与严格校验取同值；不改源path/state字节，也不放宽几何/样式门禁。
        return format(Decimal.from_float(_float32(value)), 'f')
    commands = stroke['commands']
    return ('q\n1 0 0 1 '+number(tx)+' '+number(ty)+' cm\n' +
            '\n'.join(commands['state'] + commands['ctm'] + commands['path']) + '\nQ\n').encode('latin1')


def write_strokes(page, strokes, shift):
    """保留原生数值、状态和路径，按字形的位移重放笔画。"""
    if not strokes:
        return
    if len({_identity(stroke) for stroke in strokes}) != len(strokes):
        raise ValueError('Duplicate inline stroke replay')
    document = page.parent
    refs = []
    # 隔离上一段内容留下的全局图形状态，保留原字节串。
    for data in (b'q\n', b'\nQ\n' + b''.join(_replay(page, stroke, shift) for stroke in strokes)):
        ref = document.get_new_xref()
        document.update_object(ref, '<<>>')
        document.update_stream(ref, data)
        refs.append(ref)
    contents = [refs[0], *page.get_contents(), refs[1]]
    document.xref_set_key(page.xref, 'Contents', '['+' '.join(f'{ref} 0 R' for ref in contents)+']')


def verify_strokes(page, strokes, shift):
    """复核落盘的原生路径、绘制状态和实际平移后的线段。"""
    actual = _records(page)
    drawings = Counter(_drawing_key(drawing) for drawing in page.get_drawings())
    evidence, used = [], set()
    for stroke in strokes:
        expected = _records(page, _replay(page, stroke, shift))
        if len(expected) != 1:
            raise ValueError('Unsupported inline stroke replay metadata')
        expected = expected[0]
        key = _identity(expected)
        matches = [record for record in actual if _identity(record) == key]
        if (key in used or len(matches) != 1
                or _drawing_key(matches[0]['drawing']) != _drawing_key(expected['drawing'])
                or drawings[_drawing_key(expected['drawing'])] != 1):
            raise ValueError('Moved inline stroke is missing, changed or duplicated')
        used.add(key)
        evidence.append({'page': page.number+1, 'source': stroke['drawing'],
                         'shift': list(shift), 'actual': expected['drawing'],
                         'match': 'native-operands-ctm-style-float32'})
    return evidence


def exclude_moved_drawings(missing, anchors):
    """仅将已逐条删除的已知笔画从自动恢复清单移出。"""
    moved = Counter()
    for anchor in anchors:
        for stroke in anchor.get('strokes', []):
            if stroke.get('removed') and stroke.get('matched'):
                moved[_drawing_key(stroke['drawing'])] += 1
    if any(count != 1 for count in moved.values()):
        raise ValueError('Duplicate removed inline stroke declaration')
    remaining = []
    for drawing in missing:
        key = _drawing_key(drawing)
        if moved[key]:
            moved[key] -= 1
        else:
            remaining.append(drawing)
    return remaining
