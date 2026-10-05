"""Conservative copper-text obstacles from official KiCad stroke-font rendering.

Research adapter: no glyph-width estimates, SWIG, editor writes or network calls.
Each rendered stroke is enclosed by a rectangle. Unsupported SVG/font geometry
is refused, never silently omitted. These obstacles supplement DSN geometry;
original-board DRC is still mandatory after routing.
"""
from dataclasses import dataclass, replace
import hashlib
import math
from pathlib import Path
import re
import tempfile
import xml.etree.ElementTree as ET

from .candidate import SafeCandidateValidator, context_matches, project_context, read_board
from .copper_obstacles import CopperObstacle, compile_copper_keepouts
from .dsn import DsnInput, file_digest
from .errors import CapabilityError, ValidationError
from .sexpr import children, one, render

_NUMBER = r'[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?'
_TOKEN = re.compile(r'[ML]|' + _NUMBER)
_IDENTITY = re.compile(r'translate\(0(?:\.0*)?[, ]+0(?:\.0*)?\)\s*scale\(1(?:\.0*)?[, ]+1(?:\.0*)?\)')
_PAINT_KEYS = {'fill', 'stroke', 'stroke-width', 'stroke-opacity', 'fill-opacity',
               'opacity', 'stroke-linecap', 'stroke-linejoin'}
# KiCad SVG coordinates and widths are rounded to four decimal millimeters.
# 0.1 um encloses coordinate rounding plus half of stroke-width rounding.
SVG_ENCLOSURE_MM = .0001


def _number(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValidationError('Unsupported copper SVG numeric value.') from None
    if not math.isfinite(result) or abs(result) > 1_000_000:
        raise ValidationError('Copper SVG dimension is outside supported bounds.')
    return result


def _segments(path):
    tokens = _TOKEN.findall(path)
    if re.sub(r'[\s,]', '', _TOKEN.sub('', path)) or len(tokens) > 400_000:
        raise CapabilityError('Copper text requires supported M/L stroke-font SVG paths.')
    current = None
    command = None
    index = 0
    while index < len(tokens):
        if tokens[index] in {'M', 'L'}:
            command = tokens[index]
            index += 1
        if (command is None or index+1 >= len(tokens)
                or tokens[index] in {'M', 'L'} or tokens[index+1] in {'M', 'L'}):
            raise ValidationError('Malformed copper text SVG path.')
        point = (_number(tokens[index]), _number(tokens[index+1]))
        index += 2
        if command == 'L':
            if current is None:
                raise ValidationError('Copper SVG line has no starting point.')
            yield current, point
        current = point
        command = 'L'  # Extra coordinate pairs after M are line segments in SVG.


def stroke_obstacles(svg_text, layer):
    """Enclose actual rendered strokes, including rotations/mirroring baked by KiCad."""
    if len(svg_text.encode('utf-8')) > 8_000_000:
        raise ValidationError('Copper text SVG exceeds bounds.')
    # KiCad emits this fixed declaration. Strip it without fetching the DTD;
    # every other DTD or entity declaration remains unsupported.
    svg_text = re.sub(r'<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN"\s+'
                      r'"http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd">', '', svg_text, count=1)
    if '<!DOCTYPE' in svg_text or '<!ENTITY' in svg_text:
        raise ValidationError('Copper text SVG exceeds bounds or declares entities.')
    try:
        root = ET.fromstring(svg_text)
    except ET.ParseError:
        raise ValidationError('Malformed copper text SVG.') from None
    if root.tag != '{http://www.w3.org/2000/svg}svg':
        raise ValidationError('Expected an SVG document.')
    view = [_number(x) for x in root.get('viewBox', '').split()]
    if len(view) != 4 or view[:2] != [0, 0] or min(view[2:]) <= 0:
        raise CapabilityError('Copper SVG requires an unshifted page at millimeter scale.')
    for key, expected in zip(('width', 'height'), view[2:]):
        value = root.get(key, '')
        if not value.endswith('mm') or abs(_number(value[:-2])-expected) > 1e-8:
            raise CapabilityError('Copper SVG viewport must use millimeters at scale one.')
    result = []

    def visit(node, inherited, transformed=False, depth=0):
        if depth > 64:
            raise ValidationError('Copper SVG nesting exceeds the supported limit.')
        tag = node.tag.removeprefix('{http://www.w3.org/2000/svg}')
        if tag == 'svg' and depth:
            raise CapabilityError('Nested copper SVG viewports are unsupported.')
        style = dict(inherited)
        for key in ('fill', 'stroke', 'stroke-width', 'stroke-opacity', 'fill-opacity',
                    'opacity', 'stroke-linecap', 'stroke-linejoin'):
            if key in node.attrib:
                style[key] = node.attrib[key]
        for item in node.get('style', '').split(';'):
            if item.strip():
                if ':' not in item:
                    raise ValidationError('Malformed copper SVG style.')
                key, value = item.split(':', 1)
                style[key.strip().lower()] = value.strip()
        transform = node.get('transform', '').strip()
        transformed = transformed or bool(transform and not _IDENTITY.fullmatch(transform))
        if tag in {'title', 'desc'}:
            return
        if tag == 'text':
            # KiCad includes an invisible searchable text label beside real strokes.
            if _number(style.get('opacity', '1')) != 0:
                raise CapabilityError('Visible SVG text cannot provide exact copper geometry.')
            return
        if tag not in {'svg', 'g', 'path'}:
            raise CapabilityError('Unsupported copper SVG geometry; no obstacle was guessed.')
        if any(key in node.attrib for key in ('clip-path', 'mask', 'filter')) or any(
                key in style for key in ('clip-path', 'mask', 'filter', 'display', 'visibility', 'vector-effect',
                                         'transform', 'transform-origin', 'translate', 'rotate', 'scale')):
            raise CapabilityError('Unsupported copper SVG display modifier.')
        if tag == 'path':
            if set(style)-_PAINT_KEYS:
                raise CapabilityError('Unsupported copper SVG CSS property.')
            if (transformed or style.get('fill') != 'none' or style.get('stroke') in {None, 'none'}
                    or _number(style.get('opacity', '1')) != 1
                    or _number(style.get('stroke-opacity', '1')) != 1
                    or style.get('stroke-linecap') != 'round' or style.get('stroke-linejoin') != 'round'):
                raise CapabilityError('Only opaque, untransformed round copper strokes are supported.')
            width = _number(style.get('stroke-width'))
            if not 0 < width <= 100:
                raise ValidationError('Copper SVG stroke width is outside supported bounds.')
            radius = width/2 + SVG_ENCLOSURE_MM
            for a, b in _segments(node.get('d', '')):
                result.append(CopperObstacle(layer, min(a[0], b[0])-radius, min(a[1], b[1])-radius,
                                             max(a[0], b[0])+radius, max(a[1], b[1])+radius))
                if len(result) > 10_000:
                    raise CapabilityError('Copper text exceeds the 10,000-obstacle budget.')
        for child in node:
            visit(child, style, transformed, depth+1)

    visit(root, {})
    if not result:
        raise CapabilityError('KiCad did not render supported copper text strokes.')
    return tuple(dict.fromkeys(result))


@dataclass(frozen=True)
class CopperTextEvidence:
    obstacles: tuple[CopperObstacle, ...]
    board_digest: str
    context_digest: str
    tool_version: tuple[int, int, int]
    text_items: int


@dataclass(frozen=True)
class PreparedCopperText:
    """Research input plus original export/context bindings, not write approval."""
    dsn: DsnInput
    original: DsnInput
    evidence: CopperTextEvidence
    context: tuple[tuple[Path, str | None], ...]

    def assert_current(self, cli):
        self.original.assert_unchanged()
        self.dsn.assert_unchanged()
        if (not context_matches(dict(self.context)) or tuple(cli.version) != self.evidence.tool_version
                or self.original.ticket.board_digest != self.evidence.board_digest):
            raise ValidationError('Copper text routing evidence is stale; prepare the candidate again.')


def prepare_copper_text_dsn(dsn, cli, output):
    """Prepare a new bound research export while preserving its SES design name."""
    dsn.assert_unchanged()
    _, context = project_context(dsn.ticket.board_path)
    evidence = extract_copper_text(dsn.ticket.board_path, cli)
    text = compile_copper_keepouts(dsn.path.read_text(encoding='utf-8'), evidence.obstacles)
    dsn.assert_unchanged()
    if evidence.board_digest != dsn.ticket.board_digest or not context_matches(context):
        raise ValidationError('Source changed during copper obstacle preparation.')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    # Freerouting may use the filename for SES base_design; changing it breaks
    # the identity gate even when the DSN PCB root remains byte-for-byte intact.
    path = output/dsn.path.name
    with path.open('x', encoding='utf-8', newline='\n') as handle:
        handle.write(text)
    prepared = PreparedCopperText(replace(dsn, path=path, digest=file_digest(path)), dsn, evidence,
                                  tuple(context.items()))
    prepared.assert_current(cli)
    return prepared


def extract_copper_text(board_path, cli):
    """Render board-level copper text only on private snapshots; preserve originals."""
    board = Path(board_path).resolve(strict=True)
    digest = file_digest(board)
    _, root = read_board(board)
    _, context = project_context(board)
    files = SafeCandidateValidator._context_files(context)
    version = cli.version or cli.check_startup()
    layers = {row[1] for row in one(root, 'layers')[1:] if isinstance(row, list)
              and len(row) > 2 and row[2] in {'signal', 'power', 'mixed', 'jumper'}}
    other_text = list(children(root, 'gr_text_box'))
    for footprint in children(root, 'footprint'):
        other_text.extend(node for name in ('fp_text', 'fp_text_box', 'property')
                          for node in children(footprint, name))
    if any(any(row[1] in layers for row in children(node, 'layer')) for node in other_text):
        raise CapabilityError('Footprint copper text and copper text boxes need a supported geometry adapter.')
    texts = [node for node in children(root, 'gr_text') if one(node, 'layer')[1] in layers]
    if len(texts) > 128:
        raise CapabilityError('Copper text exceeds the 128-item budget.')
    if any('${' in node[1] for node in texts):
        raise CapabilityError('Resolve dynamic variables in copper text before obstacle extraction.')
    header = [node for node in root[1:] if isinstance(node, list) and node[0] in {
        'version', 'generator', 'generator_version', 'general', 'paper', 'layers', 'setup', 'net', 'title_block'}]
    obstacles = []
    with tempfile.TemporaryDirectory(prefix='velatrace-copper-text-', ignore_cleanup_errors=True) as folder:
        folder = Path(folder)
        for index, node in enumerate(texts):
            work = folder/str(index)
            work.mkdir()
            staged = work/board.name
            staged.write_text(render([root[0], *header, node]), encoding='utf-8')
            for name, data in files.items():
                (work/name).write_bytes(data)
            output = work/'copper.svg'
            layer = one(node, 'layer')[1]
            cli._run(['pcb', 'export', 'svg', '--layers', layer, '--exclude-drawing-sheet',
                      '--page-size-mode', '1', '--mode-single', '--output', str(output), str(staged)],
                     work, (0,), cli._export_config_home())
            if not output.is_file() or output.stat().st_size > 8_000_000:
                raise ValidationError('KiCad did not produce a bounded copper text SVG.')
            obstacles.extend(stroke_obstacles(output.read_text(encoding='utf-8'), layer))
            if len(obstacles) > 10_000:
                raise CapabilityError('Copper text exceeds the total obstacle budget.')
    if digest != file_digest(board) or not context_matches(context) or tuple(cli.version) != tuple(version):
        raise ValidationError('Board, project context or KiCad version changed during copper text extraction.')
    context_digest = hashlib.sha256(repr(sorted(
        (name, hashlib.sha256(data).hexdigest()) for name, data in files.items())).encode()).hexdigest()
    return CopperTextEvidence(tuple(dict.fromkeys(obstacles)), digest, context_digest, tuple(version), len(texts))
