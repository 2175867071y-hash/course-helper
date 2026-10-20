"""可核验的 HTML 拖拽匹配与排序；只执行页面中已识别的来源和落点。"""
from dataclasses import dataclass
from browser import ManualRequired
from quiz_ai import QuizDragItem, QuizQuestion


SOURCES = '[draggable=true], [data-drag-id], .drag-item'
TARGETS = '[data-drop-target], [data-drop-id], .drop-zone, .dropzone, .droppable, [aria-dropeffect]:not([aria-dropeffect=none])'
SORTABLE = '[data-sortable], .sortable'


@dataclass
class DragBinding:
    sources: dict
    targets: dict
    kind: str
    container: object = None


def is_drag_group(group):
    return bool(group.locator(SOURCES).count() and
                (group.locator(TARGETS).count() or group.locator(SORTABLE).count()))


def drag_prompt(group):
    return group.evaluate(r"""(e,s)=>{
        const copy=e.cloneNode(true);
        copy.querySelectorAll(s.sources).forEach(n=>n.remove());
        copy.querySelectorAll(s.targets).forEach(n=>n.replaceChildren());
        return (copy.textContent || '').replace(/\s+/g,' ').trim();
    }""", {'sources': SOURCES, 'targets': TARGETS})


def _stable_key(node):
    key = node.get_attribute('id') or node.get_attribute('data-drag-id')
    if not key:
        raise ManualRequired('拖拽选项没有稳定编号，暂不能在移动后可靠核验。')
    return key


def read_drag(group, index, context, mapping):
    from quiz_media import references
    candidates = group.locator(SOURCES).all()
    if any(not source.is_visible() for source in candidates):
        raise ManualRequired('拖拽题还有隐藏选项，无法确认完整题目。')
    if not candidates:
        raise ManualRequired('没有找到可拖动的题目选项。')
    keyed = sorted(((_stable_key(node), node) for node in candidates), key=lambda pair: pair[0])
    if len({key for key, _ in keyed}) != len(keyed):
        raise ManualRequired('拖拽选项编号重复，不能唯一对应答案。')
    sources, source_bindings = [], {}
    for number, (_, node) in enumerate(keyed):
        identity = f'q{index}_s{number}'
        text = node.inner_text().strip() or node.get_attribute('aria-label') or ''
        refs = references(node, mapping)
        if refs:
            text += ' [图片：' + ', '.join(refs) + ']'
        if not text:
            raise ManualRequired('拖拽选项没有可读取的文字或图片。')
        sources.append(QuizDragItem(identity, text))
        source_bindings[identity] = node.element_handle()
    target_nodes = group.locator(TARGETS).all()
    sortable = group.locator(SORTABLE)
    kind = 'drag'
    target_bindings, targets, container = {}, [], None
    if not target_nodes and sortable.count() == 1:
        kind = 'sort'
        container = sortable.element_handle()
        if not all(container.evaluate('(e,n)=>e.contains(n)', node) for node in source_bindings.values()):
            raise ManualRequired('排序来源不在同一排序区域。')
        targets = [QuizDragItem(f'q{index}_t{i}', f'第 {i+1} 个位置') for i in range(len(sources))]
    else:
        for number, node in enumerate(target_nodes):
            if not node.is_visible() or not node.is_enabled():
                raise ManualRequired('拖拽落点尚未显示或不可操作。')
            identity = f'q{index}_t{number}'
            label = node.get_attribute('aria-label') or node.get_attribute('data-label')
            if not label:
                label = node.evaluate("""(e,s)=>{const c=e.cloneNode(true);
                    c.querySelectorAll(s).forEach(n=>n.remove());return (c.textContent||'').trim();}""", SOURCES)
            refs = references(node, mapping, exclude_sources=True)
            if refs:
                label = (label or '') + ' [图片：' + ', '.join(refs) + ']'
            if not label:
                raise ManualRequired('拖拽落点缺少可识别的含义，不能按位置猜测。')
            targets.append(QuizDragItem(identity, label))
            target_bindings[identity] = node.element_handle()
    if not 1 <= len(targets) <= len(sources) <= 100:
        raise ManualRequired('拖拽来源与落点数量不匹配，当前支持每个落点放一个不同选项。')
    prompt = drag_prompt(group) or ('按题意排序。' if kind == 'sort' else '将选项匹配到对应落点。')
    return QuizQuestion(str(index), kind, prompt, [], context=context,
        drag_sources=sources, drag_targets=targets), DragBinding(source_bindings, target_bindings, kind, container)


def verify_drag(binding, placements):
    expected_targets = (set(binding.targets) if binding.kind == 'drag' else
                        {f'{next(iter(binding.sources)).split("_s")[0]}_t{i}' for i in range(len(binding.sources))})
    if (set(placements) != expected_targets or any(source not in binding.sources for source in placements.values())
            or len(set(placements.values())) != len(placements)):
        raise ManualRequired('拖拽答案与页面来源、落点不一致，不会提交。')
    if binding.kind == 'sort':
        actual = binding.container.evaluate('(e,s)=>[...e.querySelectorAll(s)].map(n=>n.id || n.dataset.dragId)', SOURCES)
        expected = [binding.sources[placements[f'{next(iter(binding.sources)).split("_s")[0]}_t{i}']].evaluate(
            'e=>e.id || e.dataset.dragId') for i in range(len(binding.sources))]
        if actual != expected:
            raise ManualRequired('网页未保留拖拽排序结果，不会自动提交。')
    else:
        for target_id, source_id in placements.items():
            target, source = binding.targets[target_id], binding.sources[source_id]
            if not target.evaluate('(e,s)=>e.isConnected && s.isConnected && e.contains(s)', source):
                raise ManualRequired('网页未将拖拽选项放入预期落点，不会自动提交。')
            if target.evaluate('(e,s)=>e.querySelectorAll(s).length', SOURCES) != 1:
                raise ManualRequired('拖拽落点包含多个选项，结果不明确，不会自动提交。')


def _move(page, source, target, *, before=False):
    source.scroll_into_view_if_needed(timeout=5000)
    target.scroll_into_view_if_needed(timeout=5000)
    src, dst = source.bounding_box(), target.bounding_box()
    if not src or not dst:
        raise ManualRequired('拖拽选项或落点不可见。')
    # 浏览器正常鼠标事件同时适用于原生 HTML 拖拽和部分基于 pointer 的拖拽组件。
    page.mouse.move(src['x'] + src['width']/2, src['y'] + src['height']/2)
    page.mouse.down()
    try:
        page.mouse.move(src['x'] + src['width']/2 + 8, src['y'] + src['height']/2 + 8, steps=3)
        x = dst['x'] + dst['width']/2
        y = dst['y'] + (min(4, dst['height']/4) if before else dst['height']/2)
        page.mouse.move(x, y, steps=8)
        page.mouse.move(x, y, steps=2)
    finally:
        page.mouse.up()


def fill_drag(binding, placements, check, page):
    # 操作前验证映射；防止错误的映射只完成一半。
    if (len(placements) != (len(binding.targets) if binding.kind == 'drag' else len(binding.sources))
            or any(source not in binding.sources for source in placements.values())
            or len(set(placements.values())) != len(placements)):
        raise ManualRequired('AI 拖拽答案不完整或存在重复选项。')
    if binding.kind == 'drag':
        if set(placements) != set(binding.targets):
            raise ManualRequired('AI 拖拽落点编号无效。')
        for target_id, source_id in placements.items():
            check()
            source, target = binding.sources[source_id], binding.targets[target_id]
            if not target.evaluate('(e,s)=>e.contains(s)', source):
                _move(page, source, target)
            check()
    else:
        prefix = next(iter(binding.sources)).split('_s')[0]
        if set(placements) != {f'{prefix}_t{i}' for i in range(len(binding.sources))}:
            raise ManualRequired('AI 排序位置编号无效。')
        for index in range(len(binding.sources)):
            check()
            source = binding.sources[placements[f'{prefix}_t{index}']]
            current = binding.container.query_selector_all(SOURCES)
            if not source.evaluate('(e,s)=>e===s', current[index]):
                _move(page, source, current[index], before=True)
            check()
    verify_drag(binding, placements)
