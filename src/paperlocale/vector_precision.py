"""Retain source numeric precision after MuPDF's float32 PDF rewrite.

Rectangle widths, page boxes and shading coordinates can lose precision in
MuPDF rewrites. Native TJ spacing can also change despite identical glyph
geometry. These changes can alter a second renderer's pixels. Apply this
once after the final MuPDF save, before preservation checks; never rasterize.
"""
from collections import defaultdict
from decimal import Decimal
from io import BytesIO
import math
import struct

from pypdf import PdfReader, PdfWriter
from pypdf.generic import (ArrayObject, ContentStream, DictionaryObject,
                           FloatObject, IndirectObject, NameObject)


def _float32(value):
    value = float(value)
    if not math.isfinite(value):
        raise ValueError('Non-finite PDF graphics operand')
    return struct.unpack('f', struct.pack('f', value))[0]


def _rectangle_key(args):
    x, y, width, height = map(_float32, args)
    return x, y, _float32(x + width), _float32(y + height)


def _multiply(a, b):
    # MuPDF composes float matrices with a rounding step after each product
    # and sum. A double-precision dot product rounded once is a different key.
    def dot(x, y, u, v):
        return _float32(_float32(x*y) + _float32(u*v))
    return (dot(a[0], b[0], a[1], b[2]), dot(a[0], b[1], a[1], b[3]),
            dot(a[2], b[0], a[3], b[2]), dot(a[2], b[1], a[3], b[3]),
            _float32(dot(a[4], b[0], a[5], b[2]) + b[4]),
            _float32(dot(a[4], b[1], a[5], b[3]) + b[5]))


def _operations(data):
    """Use pypdf's parser, including strings/comments/inline images, with offsets.

    Its parser appends each complete operation after consuming it. Recording
    those boundaries preserves original bytes instead of reserializing all text
    and graphics numbers. Inline image data is never searched with a regex.
    """
    stream = BytesIO(data)
    ends = []

    class Operations(list):
        def append(self, operation):
            super().append(operation)
            ends.append(stream.tell())

    parsed = ContentStream(None, None)
    parsed._operations = Operations()
    parsed._parse_content_stream(stream)
    ctm = (1, 0, 0, 1, 0, 0)
    stack = []
    start = 0
    for (args, op), end in zip(parsed.operations, ends):
        if op == b'q':
            stack.append(ctm)
        elif op == b'Q':
            if not stack:
                raise ValueError('Unbalanced graphics state while preserving precision')
            ctm = stack.pop()
        elif op == b'cm':
            ctm = _multiply(tuple(map(_float32, args)), ctm)
        yield args, op, start, end, ctm
        start = end
    if stack:
        raise ValueError('Unbalanced graphics state while preserving precision')


def _rectangles(data):
    for args, op, start, end, ctm in _operations(data):
        if op == b're':
            yield (ctm, _rectangle_key(args)), tuple(map(float, args)), start, end


def _form_calls(data, objects):
    result = defaultdict(set)
    for args, op, _, _, ctm in _operations(data):
        if op == b'Do' and args[0] in objects and objects[args[0]].get_object().get('/Subtype') == '/Form':
            result[ctm].add(args[0])
    return result


def restore_rectangles(source, target):
    """Replace only uniquely identified original rectangle operations.

    Matching includes the effective CTM, not page coordinates or article names.
    q/Q, clipping, paint operators, colors and text remain target bytes. Different
    exact rectangles collapsing to one float32 key are ambiguous: refuse an
    invented choice. Operations already equal to an original need no replacement.
    """
    if source == target:
        return target, 0
    originals = defaultdict(dict)
    for key, values, start, end in _rectangles(source):
        originals[key].setdefault(values, source[start:end])
    replacements = []
    for key, values, start, end in _rectangles(target):
        choices = originals.get(key, {})
        if not choices:
            continue
        if len(choices) != 1:
            raise ValueError('Ambiguous original rectangle precision; preservation stopped')
        if values in choices:
            continue
        replacements.append((start, end, b'\n' + next(iter(choices.values())) + b'\n'))
    chunks = []
    cursor = 0
    for start, end, data in replacements:
        chunks.extend((target[cursor:start], data))
        cursor = end
    chunks.append(target[cursor:])
    return b''.join(chunks), len(replacements)


class _SourceFloat(FloatObject):
    def myrepr(self):
        # pypdf's ordinary float writer also rounds. Keep the source double's
        # shortest round-tripping value, expanded to PDF's non-exponent syntax.
        return format(Decimal(repr(float(self))), 'f')


def _precise_floats(obj, visited):
    if isinstance(obj, IndirectObject):
        if isinstance(obj.get_object(), FloatObject):
            return _SourceFloat(obj.get_object())
        if obj.idnum not in visited:
            visited.add(obj.idnum)
            _precise_floats(obj.get_object(), visited)
    elif isinstance(obj, FloatObject):
        return _SourceFloat(obj)
    elif isinstance(obj, DictionaryObject):
        for key, value in list(obj.items()):
            obj[key] = _precise_floats(value, visited)
    elif isinstance(obj, ArrayObject):
        for index, value in enumerate(obj):
            obj[index] = _precise_floats(value, visited)
    return obj


def _same_resource(a, b, seen=None):
    """Allow only float32 serialization loss, not changed resource semantics."""
    seen = set() if seen is None else seen
    if isinstance(a, IndirectObject) and isinstance(b, IndirectObject):
        pair = (a.idnum, b.idnum)
        if pair in seen:
            return True
        seen.add(pair)
    a, b = a.get_object(), b.get_object()
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return _float32(a) == _float32(b)
    if isinstance(a, DictionaryObject) and isinstance(b, DictionaryObject):
        ignored = set()
        if hasattr(a, 'get_data') or hasattr(b, 'get_data'):
            if not (hasattr(a, 'get_data') and hasattr(b, 'get_data')) or a.get_data() != b.get_data():
                return False
            ignored = {'/Length', '/Filter', '/DecodeParms'}
        keys = set(a) - ignored
        return keys == set(b) - ignored and all(_same_resource(a[k], b[k], seen) for k in keys)
    if isinstance(a, ArrayObject) and isinstance(b, ArrayObject):
        return len(a) == len(b) and all(_same_resource(x, y, seen) for x, y in zip(a, b))
    return a == b


def _form_precision_unchanged(source, target, identity):
    """Prove this candidate needs no edits, without assigning Form identity."""
    if any(source.get(field, identity) != target.get(field, identity)
           for field in ('/BBox', '/Matrix')):
        return False
    resources = source.get('/Resources', {})
    resources = resources.get_object() if isinstance(resources, IndirectObject) else resources
    if '/Shading' in resources:
        return False
    objects = resources.get('/XObject', {})
    objects = objects.get_object() if isinstance(objects, IndirectObject) else objects
    if any(ref.get_object().get('/Subtype') == '/Form' for ref in objects.values()):
        return False
    return restore_rectangles(source.get_data(), target.get_data())[1] == 0


def _text_arrays(data):
    """Conservative identities for first TJ after explicit text positioning.

    Text matrices use MuPDF's float32 arithmetic. Unsupported showing operators
    invalidate the position until a new line/matrix is set; no widths are guessed.
    """
    identity = (1, 0, 0, 1, 0, 0)
    matrix = identity
    state = {'font': None, 'size': 0, 'Tc': 0, 'Tw': 0, 'Tz': 100,
             'Ts': 0, 'Tr': 0, 'TL': 0}
    stack = []
    ready = False
    operations = list(_operations(data))
    for index, (args, op, start, end, ctm) in enumerate(operations):
        if op == b'q':
            stack.append(state.copy())
        elif op == b'Q':
            state = stack.pop()
        elif op == b'gs':
            # ExtGState can replace the active font/widths without a Tf. Do not
            # infer resource identity until an explicit Tf establishes it again.
            state['font'] = None
        elif op == b'BT':
            matrix, ready = identity, True
        elif op == b'ET':
            ready = False
        elif op == b'Tf':
            state['font'], state['size'] = args[0], _float32(args[1])
        elif op in (b'Tc', b'Tw', b'Tz', b'Ts', b'Tr', b'TL'):
            state[op.decode()] = _float32(args[0])
        elif op == b'Tm':
            matrix, ready = tuple(map(_float32, args)), True
        elif op in (b'Td', b'TD', b'T*'):
            tx, ty = (0, -state['TL']) if op == b'T*' else map(_float32, args)
            if op == b'TD':
                state['TL'] = -ty
            matrix = _multiply((1, 0, 0, 1, tx, ty), matrix)
            ready = True
        elif op == b'TJ':
            values = args[0]
            # Only simple ASCII arrays; complex encodings and native replays are
            # intentionally outside this precision repair.
            strings = [v.original_bytes for v in values if hasattr(v, 'original_bytes')]
            numeric = [v for v in values if isinstance(v, (int, float))]
            reset_after = True
            for _, following, *_ in operations[index + 1:]:
                if following in (b'Tm', b'Td', b'TD', b'T*', b'ET', b'BT'):
                    break
                if following in (b'TJ', b'Tj', b"'", b'"'):
                    reset_after = False
                    break
            if ready and reset_after and len(strings) + len(numeric) == len(values) and strings and all(
                    all(32 <= c < 127 for c in value) for value in strings):
                structure = tuple(('text', v.original_bytes) if hasattr(v, 'original_bytes')
                                  else ('number',) for v in values)
                settings = tuple(state[k] for k in ('size', 'Tc', 'Tw', 'Tz', 'Ts', 'Tr'))
                yield (ctm, matrix, settings, structure), state['font'], b''.join(strings).decode('ascii'), tuple(map(float, numeric)), start, end
            ready = False
        elif op in (b"'", b'"'):
            if op == b'"':
                state['Tw'], state['Tc'] = map(_float32, args[:2])
            matrix = _multiply((1, 0, 0, 1, 0, -state['TL']), matrix)
            ready = False
        elif op == b'Tj':
            ready = False


def _protected_text_regions(source_page, target_page, regions):
    """Require whole-box glyph identity; coordinates are proven per complete TJ run."""
    import pymupdf as fitz

    def glyphs(page, rect):
        result = []
        for span in page.get_texttrace():
            style = tuple(span[k] for k in ('font', 'size', 'dir', 'wmode', 'color', 'opacity', 'type'))
            for char in span['chars']:
                if rect.contains(fitz.Rect(char[3])):
                    result.append((char, style))
        return result

    proven = []
    for region in regions:
        if region['kind'] not in {'table', 'figure'}:
            continue
        rect = fitz.Rect(region['rect'])
        before, after = glyphs(source_page, rect), glyphs(target_page, rect)
        if before and [(char[:2], style) for char, style in before] == [(char[:2], style) for char, style in after]:
            proven.append((before, after, tuple(source_page.transformation_matrix)))
    return proven


def _restore_protected_text(source, target, source_fonts, target_fonts, proven):
    """Restore only unique, same-position native TJ arrays inside protected objects."""
    originals = defaultdict(list)
    for key, font, text, values, start, end in _text_arrays(source):
        if font not in source_fonts:
            continue
        base = source_fonts[font].get('/BaseFont')
        originals[(key, base)].append((font, text, values, start, end))
    replacements = []
    for key, font, text, values, start, end in _text_arrays(target):
        if font not in target_fonts:
            continue
        choices = originals.get((key, target_fonts[font].get('/BaseFont')), [])
        if not choices or any(values == item[2] for item in choices):
            continue
        matches = [item for item in choices if _same_resource(source_fonts[item[0]], target_fonts[font])]
        if not matches:
            continue
        # The entire array's glyph run must occur once in a proven box, with the
        # same font. A changed/moved citation or body run cannot satisfy this.
        base = str(target_fonts[font].get('/BaseFont', '')).lstrip('/').split('+')[-1]
        supported = False
        for glyphs, after_glyphs, page_matrix in proven:
            content = ''.join(chr(char[0]) for char, _ in glyphs)
            offset = content.find(text)
            if offset < 0 or content.find(text, offset + 1) >= 0:
                continue
            run = glyphs[offset:offset + len(text)]
            import pymupdf as fitz
            effective = _multiply(key[1], key[0])
            origin = tuple(fitz.Point(effective[4], effective[5]) * fitz.Matrix(page_matrix))
            if (run == after_glyphs[offset:offset + len(text)]
                    and key[2][4] == 0 and run[0][0][2] == origin
                    and run[0][1][2] == (1., 0.)
                    and all(style[0] == base and style == run[0][1]
                            and char[2][1] == run[0][0][2][1] for char, style in run)):
                supported = True
                break
        if not supported:
            continue
        unique = {(item[2], source[item[3]:item[4]]) for item in matches}
        if len({item[0] for item in unique}) != 1:
            raise ValueError('Ambiguous original protected text precision')
        replacement = next(iter(unique))[1]
        replacements.append((start, end, b'\n' + replacement + b'\n'))
    chunks, cursor = [], 0
    for start, end, replacement in replacements:
        chunks.extend((target[cursor:start], replacement))
        cursor = end
    chunks.append(target[cursor:])
    return b''.join(chunks), len(replacements)


def restore_vector_precision(source, candidate, *, protected_regions=()):
    """Append precise source geometry to the final candidate without renumbering.

    Shading resources (including functions/colorspaces) are native source PDF
    objects, cloned recursively. Whole text/Form streams are never copied from source.
    The caller must still run its final text, geometry and pixel checks.
    """
    original = PdfReader(source)
    written = PdfWriter(candidate, incremental=True)
    if len(original.pages) != len(written.pages):
        raise ValueError('Page count changed before precision preservation')
    evidence = {'rectangles_restored': 0, 'shading_resource_sets_restored': 0,
                'form_geometry_restored': 0, 'ambiguous_forms_unchanged': 0,
                'page_geometry_restored': 0, 'protected_text_arrays_restored': 0}
    visited_forms = {}
    precise_seen = set()

    def resources(a, b, source_data, target_data):
        if '/Shading' in a:
            # Redaction may drop unused names; retain the original resource set,
            # but never silently overwrite a new/rebound shading under that name.
            old = a['/Shading']
            current = b.get('/Shading', {})
            current = current.get_object() if isinstance(current, IndirectObject) else current
            if set(current) - set(old):
                raise ValueError('Unexpected shading resources; precision preservation stopped')
            if any(not _same_resource(old[name], current[name]) for name in current):
                raise ValueError('Shading resource changed beyond numeric precision')
            cloned = old.clone(written)
            b[NameObject('/Shading')] = _precise_floats(cloned, precise_seen)
            evidence['shading_resource_sets_restored'] += 1
        a_forms = a.get('/XObject', {})
        b_forms = b.get('/XObject', {})
        a_forms = a_forms.get_object() if isinstance(a_forms, IndirectObject) else a_forms
        b_forms = b_forms.get_object() if isinstance(b_forms, IndirectObject) else b_forms
        source_calls = _form_calls(source_data, a_forms) if a_forms else {}
        target_calls = _form_calls(target_data, b_forms) if b_forms else {}
        for name, ref in b_forms.items():
            y = ref.get_object()
            if y.get('/Subtype') != '/Form':
                continue
            # Resource names are not identities: MuPDF may rename /A to /Fm1
            # even when a different original Form was already named /Fm1.
            names = {n for ctm, calls in target_calls.items() if name in calls
                     for n in source_calls.get(ctm, ())}
            identity = ArrayObject([FloatObject(v) for v in (1, 0, 0, 1, 0, 0)])
            names = {n for n in names
                     if _same_resource(a_forms[n]['/BBox'], y['/BBox'])
                     and _same_resource(a_forms[n].get('/Matrix', identity), y.get('/Matrix', identity))}
            if not names:
                continue  # A new native replay/translation Form has no source counterpart.
            refs = {a_forms.raw_get(n).idnum for n in names}
            if len(refs) != 1:
                # Empty Forms and page rules can share CTM/BBox/Matrix. No
                # identity is needed if every candidate proves there is nothing
                # this pass would change. Any possible edit still needs identity.
                if all(_form_precision_unchanged(a_forms[n], y, identity) for n in names):
                    evidence['ambiguous_forms_unchanged'] += 1
                    continue
                raise ValueError('Ambiguous original Form calls; precision preservation stopped')
            x = a_forms[next(iter(names))].get_object()
            if x.get('/Subtype') != '/Form':
                raise ValueError('Original Form resource identity changed')
            pair = (next(iter(refs)), ref.idnum)
            if pair[1] in visited_forms:
                if visited_forms[pair[1]] != pair[0]:
                    raise ValueError('Ambiguous source Form identity')
                continue
            visited_forms[pair[1]] = pair[0]
            for field in ('/Matrix', '/BBox'):
                before = x.get(field, identity)
                after = y.get(field, identity)
                if before != after:
                    if not _same_resource(before, after):
                        raise ValueError('Form geometry changed beyond numeric precision')
                    y[NameObject(field)] = _precise_floats(before.clone(written), precise_seen)
                    evidence['form_geometry_restored'] += 1
            data, count = restore_rectangles(x.get_data(), y.get_data())
            if count:
                y.set_data(data)
                evidence['rectangles_restored'] += count
            if '/Resources' in x and '/Resources' in y:
                resources(x['/Resources'], y['/Resources'], x.get_data(), y.get_data())

    import pymupdf as fitz
    with fitz.open(source) as before, fitz.open(candidate) as after:
        text_proofs = {number: _protected_text_regions(before[number - 1], after[number - 1],
                          [r for r in protected_regions if r['page'] == number])
                       for number in range(1, len(original.pages) + 1)
                       if before[number - 1].rotation == after[number - 1].rotation}
    for number, (a, b) in enumerate(zip(original.pages, written.pages), 1):
        # Rotation normalization changes the coordinate system deliberately.
        # Restore exact page boxes only when their native float32 values agree.
        if a.rotation == b.rotation:
            for field in ('/MediaBox', '/CropBox', '/BleedBox', '/TrimBox', '/ArtBox'):
                if field in a and field in b and a[field] != b[field] and _same_resource(a[field], b[field]):
                    b[NameObject(field)] = _precise_floats(a[field].clone(written), precise_seen)
                    evidence['page_geometry_restored'] += 1
        src, dst = a.get_contents(), b.get_contents()
        if src is not None and dst is not None:
            data, count = restore_rectangles(src.get_data(), dst.get_data())
            data, text_count = _restore_protected_text(src.get_data(), data,
                a['/Resources'].get('/Font', {}), b['/Resources'].get('/Font', {}),
                text_proofs.get(number, [])) if text_proofs.get(number) else (data, 0)
            evidence['protected_text_arrays_restored'] += text_count
            if count or text_count:
                # set_data invalidates parsed operations without rounding them.
                dst.set_data(data)
                b.replace_contents(dst)
                evidence['rectangles_restored'] += count
        resources(a['/Resources'], b['/Resources'], src.get_data() if src is not None else b'',
                  dst.get_data() if dst is not None else b'')
    if any(evidence[key] for key in ('rectangles_restored', 'shading_resource_sets_restored',
                                     'form_geometry_restored', 'page_geometry_restored', 'protected_text_arrays_restored')):
        output = BytesIO()
        written.write(output)
        candidate.write_bytes(output.getvalue())
    return evidence
