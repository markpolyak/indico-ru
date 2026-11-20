#!/usr/bin/env python3
"""Инструменты русского перевода Indico.

Перевод хранится как *слой* (i18n/ru_RU.po): только строки, которые мы перевели или исправили.
При сборке образа слой накладывается поверх свежего перевода с Transifex (`merge`).

Команды:
  fetch    скачать текущий перевод ru_RU с Transifex в i18n/work/transifex.po (токен: ~/.indico-tx-token)
  status   сводка: сколько переведено, сколько в слое, сколько ждёт вычитки
  check    проверить слой (или --all: весь итоговый каталог) — плейсхолдеры, разметка, формы
           множественного числа, глоссарий и стиль (правила в i18n/rules.tsv)
  extract  выгрузить порцию строк в .po для работы (фильтр по модулю и состоянию)
  apply    внести переведённую порцию в слой (с пометкой needs-review)
  merge    наложить слой на каталог (используется в Dockerfile)
  export   собрать файл для загрузки на Transifex: каталог Transifex + вычитанные строки слоя
  prune    убрать из слоя строки, которые уже совпадают с Transifex
  review-items  выгрузить строки, ждущие вычитки, в JSON для страницы вычитки
  review-apply  применить отметки со страницы вычитки (каталог с JSON-документами reviews/*.json)

Осознанное исключение из правил глоссария/стиля — комментарий переводчика `# rutool: ignore`
у строки в слое (проверки плейсхолдеров и разметки всё равно выполняются).

Для `merge` нужен только babel; остальное — стандартная библиотека.
"""

import argparse
import collections
import copy
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from babel.messages.pofile import read_po, write_po

HERE = Path(__file__).resolve().parent
OVERLAY = HERE / 'ru_RU.po'
WORK = HERE / 'work'
TRANSIFEX = WORK / 'transifex.po'
RULES = HERE / 'rules.tsv'
REVIEW_FLAG = 'needs-review'
TX_RESOURCE = 'o:indico:p:indico:r:core-messages-all'
TX_LANGUAGE = 'l:ru_RU'


# ---------------------------------------------------------------- каталоги

def load(path):
    with open(path, 'rb') as f:
        return read_po(f, abort_invalid=True)


def save(catalog, path):
    catalog.fuzzy = False  # иначе babel пометит заголовок fuzzy и compile пропустит каталог
    buf = io.BytesIO()
    write_po(buf, catalog, width=0, include_previous=False)
    Path(path).write_bytes(buf.getvalue())


def clone(catalog):
    """Глубокая копия каталога (copy.deepcopy не работает из-за часового пояса в заголовке)."""
    buf = io.BytesIO()
    write_po(buf, catalog, width=0, include_previous=False)
    buf.seek(0)
    return read_po(buf)


def key(msg):
    return (msg.id, msg.context)


def strings(msg):
    return msg.string if isinstance(msg.string, tuple) else (msg.string,)


def ids(msg):
    return msg.id if isinstance(msg.id, tuple) else (msg.id,)


def is_translated(msg):
    return bool(msg.id) and all(strings(msg)) and not msg.fuzzy


def module_of(msg):
    """Модуль Indico по первому месту использования: events/abstracts, rb, web, ..."""
    mods = []
    for loc, _ in msg.locations:
        parts = loc.split('/')
        if len(parts) > 3 and parts[1] == 'modules':
            sub = parts[3] if parts[2] == 'events' and len(parts) > 4 and '.' not in parts[3] else ''
            mods.append(f'{parts[2]}/{sub}' if sub else parts[2])
        elif len(parts) > 1:
            mods.append(parts[1])
    return sorted(mods)[0] if mods else '?'


def empty_overlay(base):
    cat = copy.copy(base)
    cat._messages = collections.OrderedDict()
    cat.obsolete = collections.OrderedDict()
    return cat


def put(cat, msg):
    """Добавить/заменить строку (babel при совпадении ключа не заменяет перевод, а сливает)."""
    if cat.get(msg.id, msg.context) is not None:
        cat.delete(msg.id, msg.context)
    cat[msg.id] = msg


def sort_like(cat, ref):
    """Упорядочить строки cat как в ref — для стабильных диффов."""
    order = {key(m): i for i, m in enumerate(ref)}
    cat._messages = collections.OrderedDict(
        sorted(cat._messages.items(), key=lambda kv: order.get(key(kv[1]), 10 ** 9)))


def load_overlay(base=None):
    if OVERLAY.exists():
        return load(OVERLAY)
    if base is None:
        sys.exit(f'Нет {OVERLAY} и не передан базовый каталог')
    return empty_overlay(base)


def need_transifex():
    if not TRANSIFEX.exists():
        sys.exit(f'Нет {TRANSIFEX.relative_to(HERE.parent)} — сначала `rutool.py fetch`')
    return load(TRANSIFEX)


def apply_overlay(base, overlay, only_reviewed=False):
    """Наложить строки слоя на base (in place). Возвращает (применено, отсутствуют в base)."""
    applied, missing = 0, []
    for msg in overlay:
        if not msg.id:
            continue
        if only_reviewed and REVIEW_FLAG in msg.flags:
            continue
        target = base.get(msg.id, msg.context)
        if target is None:
            missing.append(msg)
            continue
        if len(strings(msg)) != len(strings(target)) and isinstance(target.id, tuple):
            n = base.num_plurals
            target.string = tuple(list(strings(msg)) + [''] * n)[:n]
        else:
            target.string = msg.string
        target.flags.discard('fuzzy')
        applied += 1
    return applied, missing


# ---------------------------------------------------------------- проверки

PY_FMT = re.compile(r'%(?:\([^)]+\))?[-#0 +]*\d*(?:\.\d+)?[sdifr%]')
BRACE = re.compile(r'\{[A-Za-z0-9_.]*\}')
TAG = re.compile(r'</?([A-Za-z][A-Za-z0-9]*)\b[^>]*?/?>')


def placeholders(s):
    return collections.Counter([p for p in PY_FMT.findall(s) if p != '%%'] + BRACE.findall(s))


def tags(s):
    return collections.Counter(m.group(0).split()[0].rstrip('>').lower() for m in TAG.finditer(s))


def load_rules():
    rules = []
    for n, line in enumerate(RULES.read_text().splitlines(), 1):
        if not line.strip() or line.startswith('#'):
            continue
        parts = line.split('\t')
        if len(parts) != 4:
            sys.exit(f'{RULES.name}:{n}: нужно 4 поля через TAB (уровень, regex перевода, regex оригинала, текст)')
        level, ru, en, text = parts
        rules.append((level, re.compile(ru), re.compile(en, re.I) if en.strip() else None, text))
    return rules


def text_outside_tags(s):
    return TAG.sub(' ', s)


def check_message(msg, nplurals, rules):
    """Список (уровень, текст) для одной переведённой строки."""
    out = []
    src = ids(msg)
    dst = strings(msg)
    plural = isinstance(msg.id, tuple)
    if plural and len(dst) != nplurals:
        out.append(('error', f'форм множественного числа {len(dst)}, нужно {nplurals}'))
    for i, s in enumerate(dst):
        if not s:
            continue
        ref = src[0] if not plural or i == 0 and nplurals == 1 else src[-1]
        want, got = placeholders(ref), placeholders(s)
        if plural:
            # формы могут опускать числовой плейсхолдер только если он единственный и это не форма 0
            # (форма 0 используется и для 21, 31, ... — без числа нельзя)
            want_set, got_set = set(want), set(got)
            if not got_set <= set(placeholders(src[0])) | want_set:
                out.append(('error', f'форма {i}: лишние плейсхолдеры {sorted(got_set - want_set)}'))
            if want_set - got_set:
                out.append(('error', f'форма {i}: нет плейсхолдеров {sorted(want_set - got_set)}'))
        elif want != got:
            out.append(('error', f'плейсхолдеры: в оригинале {sorted(want.elements())}, в переводе {sorted(got.elements())}'))
        if tags(ref) != tags(s):
            out.append(('error', f'разметка различается: {sorted(tags(ref).elements())} → {sorted(tags(s).elements())}'))
        if ref[:1].isspace() != s[:1].isspace() or ref[-1:].isspace() != s[-1:].isspace() \
                or ref.endswith('\n') != s.endswith('\n'):
            out.append(('warn', 'пробелы/переводы строк в начале или конце не совпадают с оригиналом'))
        if re.search(r'[A-Za-z]{4}', text_outside_tags(s)) and s.strip() == ref.strip() and re.search('[a-z]{3} [a-z]{3}', s):
            out.append(('warn', 'перевод совпадает с английским оригиналом'))
        if any('rutool: ignore' in c for c in msg.user_comments):
            continue  # осознанное исключение из правил глоссария/стиля
        for level, ru, en, text in rules:
            if ru.search(text_outside_tags(s)) and (en is None or en.search(ref)):
                out.append((level, text))
    seen, uniq = set(), []
    for item in out:
        if item not in seen:
            seen.add(item)
            uniq.append(item)
    return uniq


def msgfmt_check(catalog):
    """msgfmt -c — проверка форматных строк самим gettext. Возвращает список строк ошибок."""
    with tempfile.TemporaryDirectory() as tmp:
        po = Path(tmp) / 'c.po'
        save(catalog, po)
        r = subprocess.run(['msgfmt', '-c', '-o', os.devnull, str(po)], capture_output=True, text=True)
        # {x}…{/x} — синтаксис react-jsx-i18n, msgfmt его не понимает
        lines = [ln for ln in r.stderr.splitlines()
                 if 'fatal error' not in ln and "'/' cannot start a field name" not in ln]
        if not lines:
            return []
        # сопоставить номера строк с msgid
        text = po.read_text().splitlines()
        res = []
        for ln in lines:
            m = re.match(r'.*?:(\d+): (.*)', ln)
            if not m:
                res.append(ln)
                continue
            n = int(m.group(1)) - 1
            while n > 0 and not text[n].startswith('msgid '):
                n -= 1
            res.append(f'{text[n][6:80]}: {m.group(2)}')
        return res


# ---------------------------------------------------------------- команды

def cmd_fetch(args):
    token = Path(os.path.expanduser('~/.indico-tx-token')).read_text().strip()
    hdr = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/vnd.api+json'}
    body = {'data': {'type': 'resource_translations_async_downloads',
                     'attributes': {'content_encoding': 'text', 'file_type': 'default', 'mode': 'default'},
                     'relationships': {
                         'resource': {'data': {'type': 'resources', 'id': TX_RESOURCE}},
                         'language': {'data': {'type': 'languages', 'id': TX_LANGUAGE}}}}}
    req = urllib.request.Request('https://rest.api.transifex.com/resource_translations_async_downloads',
                                 data=json.dumps(body).encode(), headers=hdr, method='POST')
    job = json.load(urllib.request.urlopen(req))['data']['id']
    url = f'https://rest.api.transifex.com/resource_translations_async_downloads/{job}'

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **kw):
            return None

    opener = urllib.request.build_opener(NoRedirect)
    for _ in range(60):
        try:
            resp = opener.open(urllib.request.Request(url, headers=hdr))
            status = json.load(resp)['data']['attributes']['status']
            if status == 'failed':
                sys.exit('Transifex: выгрузка не удалась')
        except urllib.error.HTTPError as e:
            if e.code == 303:
                data = urllib.request.urlopen(e.headers['Location']).read()
                break
            raise
        time.sleep(2)
    else:
        sys.exit('Transifex: не дождались выгрузки')
    WORK.mkdir(exist_ok=True)
    TRANSIFEX.write_bytes(data)
    cat = load(TRANSIFEX)
    n = sum(1 for m in cat if m.id)
    tr = sum(1 for m in cat if is_translated(m))
    print(f'{TRANSIFEX.relative_to(HERE.parent)}: {tr} из {n} строк переведено')


def cmd_status(args):
    tx = need_transifex()
    ov = load_overlay(tx)
    total = [m for m in tx if m.id]
    tx_done = sum(is_translated(m) for m in total)
    in_ov = [m for m in ov if m.id]
    review = sum(REVIEW_FLAG in m.flags for m in in_ov)
    merged = clone(tx)
    _, missing = apply_overlay(merged, ov)
    done = sum(is_translated(m) for m in merged if m.id)
    print(f'Строк в каталоге:            {len(total)}')
    print(f'Переведено на Transifex:     {tx_done} ({100 * tx_done / len(total):.1f}%)')
    print(f'Строк в слое:                {len(in_ov)}  (ждут вычитки: {review}, вычитано: {len(in_ov) - review})')
    print(f'Переведено с учётом слоя:    {done} ({100 * done / len(total):.1f}%)')
    if missing:
        print(f'В слое строк, которых больше нет на Transifex: {len(missing)} (см. `prune`)')
    if args.modules:
        stat = collections.defaultdict(lambda: [0, 0, 0])
        ovk = {key(m) for m in in_ov}
        for m in merged:
            if not m.id:
                continue
            s = stat[module_of(m)]
            s[0] += 1
            s[1] += is_translated(m)
            s[2] += key(m) in ovk
        print(f'\n{"модуль":32} {"строк":>6} {"готово":>7} {"в слое":>7}')
        for mod, (n, d, o) in sorted(stat.items(), key=lambda x: -(x[1][0] - x[1][1])):
            print(f'{mod:32} {n:6} {d:7} {o:7}')


def cmd_check(args):
    tx = load(TRANSIFEX) if TRANSIFEX.exists() else None
    ov = load_overlay(tx)
    if args.file:
        cat = load(args.file)
        scope = 'файл ' + args.file
    elif args.all:
        if tx is None:
            need_transifex()
        cat = clone(tx)
        apply_overlay(cat, ov)
        scope = 'весь каталог (Transifex + слой)'
    else:
        cat = ov
        scope = 'слой ' + OVERLAY.name
    rules = load_rules()
    nplurals = (tx or cat).num_plurals
    counts = collections.Counter()
    by_text = collections.defaultdict(list)
    for msg in cat:
        if not msg.id or not any(strings(msg)):
            continue
        if args.module and not module_of(msg).startswith(args.module):
            continue
        for level, text in check_message(msg, nplurals, rules):
            counts[level] += 1
            by_text[(level, text)].append(msg)
    print(f'Проверка: {scope}')
    order = sorted(by_text.items(), key=lambda kv: (kv[0][0] != 'error', -len(kv[1])))
    for (level, text), msgs in order:
        if level == 'warn' and args.errors_only:
            continue
        print(f'\n[{level}] {text} — {len(msgs)}')
        for m in msgs[:args.examples]:
            print(f'    {ids(m)[0][:70]!r}\n      → {strings(m)[0][:90]!r}')
        if len(msgs) > args.examples:
            print(f'    … ещё {len(msgs) - args.examples}')
    fmt = msgfmt_check(cat)
    if fmt:
        print(f'\n[error] msgfmt -c — {len(fmt)}')
        for ln in fmt[:args.examples * 3]:
            print('    ' + ln)
        counts['error'] += len(fmt)
    print(f'\nИтого: ошибок {counts["error"]}, предупреждений {counts["warn"]}')
    sys.exit(1 if counts['error'] else 0)


def cmd_extract(args):
    tx = need_transifex()
    ov = load_overlay(tx)
    merged = clone(tx)
    apply_overlay(merged, ov)
    ovk = {key(m): m for m in ov if m.id}
    out = empty_overlay(tx)
    n = 0
    for m in merged:
        if not m.id:
            continue
        if args.module and not any(module_of(m).startswith(x) for x in args.module):
            continue
        state = 'overlay' if key(m) in ovk else 'translated' if is_translated(m) else 'untranslated'
        if args.state != 'all' and state != args.state:
            continue
        mm = copy.deepcopy(m)
        if key(m) in ovk:
            mm.flags = set(ovk[key(m)].flags)
        put(out, mm)
        n += 1
        if args.limit and n >= args.limit:
            break
    save(out, args.output)
    print(f'{args.output}: {n} строк')


def cmd_apply(args):
    tx = need_transifex()
    ov = load_overlay(tx)
    batch = load(args.file)
    rules = load_rules()
    added = changed = same = bad = 0
    for m in batch:
        if not m.id or not all(strings(m)):
            continue
        base = tx.get(m.id, m.context)
        if base is None:
            print(f'нет на Transifex, пропущено: {ids(m)[0][:70]!r}')
            continue
        errors = [t for lvl, t in check_message(m, tx.num_plurals, rules) if lvl == 'error']
        if errors and not args.force:
            bad += 1
            print(f'ошибка, пропущено: {ids(m)[0][:70]!r}: {"; ".join(errors)}')
            continue
        cur = ov.get(m.id, m.context)
        if is_translated(base) and strings(base) == strings(m):
            if cur is not None:
                ov.delete(m.id, m.context)
            same += 1
            continue
        if cur is not None and strings(cur) == strings(m):
            if args.reviewed:
                cur.flags.discard(REVIEW_FLAG)
            same += 1
            continue
        new = copy.deepcopy(base)
        new.string = m.string
        new.flags = {f for f in base.flags if f != 'fuzzy'}
        if not args.reviewed:
            new.flags.add(REVIEW_FLAG)
        if cur is None:
            added += 1
        else:
            changed += 1
        put(ov, new)
    sort_like(ov, tx)
    save(ov, OVERLAY)
    print(f'слой: добавлено {added}, изменено {changed}, без изменений {same}, с ошибками {bad}')


def cmd_merge(args):
    base = load(args.catalog)
    ov = load(args.overlay)
    applied, missing = apply_overlay(base, ov)
    save(base, args.catalog)
    print(f'ru_RU: из слоя применено {applied} строк; нет в каталоге: {len(missing)}')


def cmd_export(args):
    tx = need_transifex()
    ov = load_overlay(tx)
    cat = clone(tx)
    if args.module:
        ov = clone(ov)
        for m in list(ov):
            if m.id and not module_of(m).startswith(args.module):
                ov.delete(m.id, m.context)
    applied, _ = apply_overlay(cat, ov, only_reviewed=True)
    save(cat, args.output)
    print(f'{args.output}: каталог Transifex + {applied} вычитанных строк из слоя. '
          'Загрузка: Transifex → indico → core-messages-all → Russian (Russia) → Upload file.')


def cmd_prune(args):
    tx = need_transifex()
    ov = load_overlay(tx)
    gone = 0
    for m in list(ov):
        if not m.id:
            continue
        base = tx.get(m.id, m.context)
        if base is None and args.missing or base is not None and is_translated(base) \
                and strings(base) == strings(m):
            ov.delete(m.id, m.context)
            gone += 1
    save(ov, OVERLAY)
    print(f'из слоя убрано {gone} строк')


def item_id(msg):
    import hashlib
    return hashlib.sha1(((msg.context or '') + '\x04' + ids(msg)[0]).encode()).hexdigest()[:16]


def cmd_review_items(args):
    tx = need_transifex()
    ov = load_overlay(tx)
    items = []
    for m in ov:
        if not m.id or REVIEW_FLAG not in m.flags:
            continue
        if args.module and not module_of(m).startswith(args.module):
            continue
        base = tx.get(m.id, m.context)
        old = list(strings(base)) if base is not None and is_translated(base) else None
        items.append({'id': item_id(m), 'mod': module_of(m),
                      'locs': sorted({loc.replace('indico/modules/', '') for loc, _ in m.locations})[:4],
                      'ctx': m.context, 'en': list(ids(m)), 'ru': list(strings(m)), 'old': old,
                      'plural': isinstance(m.id, tuple)})
    Path(args.output).write_text(json.dumps({'batch': args.batch, 'items': items}, ensure_ascii=False))
    print(f'{args.output}: {len(items)} строк ({sum(i["old"] is None for i in items)} новых)')


def cmd_review_apply(args):
    tx = need_transifex()
    ov = load_overlay(tx)
    by_id = {item_id(m): m for m in ov if m.id}
    rules = load_rules()
    stat = collections.Counter()
    questions = []
    for f in sorted(Path(args.dir).rglob('*.json')):
        doc = json.loads(f.read_text())
        doc = doc.get('data', doc)
        m = by_id.get(f.stem)
        if m is None:
            stat['нет в слое'] += 1
            continue
        if list(doc.get('shown') or []) != list(strings(m)):
            stat['перевод изменился после отметки — пропущено'] += 1
            continue
        st = doc.get('status')
        if st == 'ok':
            if REVIEW_FLAG in m.flags:
                m.flags.discard(REVIEW_FLAG)
                stat['принято'] += 1
        elif st == 'fix':
            new = copy.deepcopy(m)
            new.string = tuple(doc['ru']) if isinstance(m.id, tuple) else doc['ru'][0]
            errs = [t for lvl, t in check_message(new, tx.num_plurals, rules) if lvl == 'error']
            if errs and not args.force:
                questions.append((m, 'исправление не прошло проверку: ' + '; '.join(errs) + f' — {doc["ru"]}'))
                stat['исправление с ошибкой — пропущено'] += 1
                continue
            m.string = new.string
            m.flags.discard(REVIEW_FLAG)
            if doc.get('note'):
                questions.append((m, 'комментарий к исправлению: ' + doc['note']))
            stat['исправлено'] += 1
        elif st == 'question':
            questions.append((m, doc.get('note', '')))
            stat['вопрос'] += 1
    save(ov, OVERLAY)
    for k, v in stat.items():
        print(f'{k}: {v}')
    for m, q in questions:
        print(f'\n? {ids(m)[0][:80]!r}\n  → {strings(m)[0][:100]!r}\n  {q}')


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='cmd', required=True)
    sub.add_parser('fetch').set_defaults(func=cmd_fetch)
    s = sub.add_parser('status')
    s.add_argument('--modules', action='store_true', help='разбивка по модулям')
    s.set_defaults(func=cmd_status)
    s = sub.add_parser('check')
    s.add_argument('--all', action='store_true', help='весь каталог (Transifex + слой)')
    s.add_argument('--file', help='проверить произвольный .po')
    s.add_argument('--module', help='только модуль (префикс), напр. events/abstracts')
    s.add_argument('--errors-only', action='store_true')
    s.add_argument('--examples', type=int, default=3)
    s.set_defaults(func=cmd_check)
    s = sub.add_parser('extract')
    s.add_argument('-o', '--output', required=True)
    s.add_argument('--module', action='append', help='префикс модуля (можно несколько)')
    s.add_argument('--state', choices=['untranslated', 'translated', 'overlay', 'all'], default='all')
    s.add_argument('--limit', type=int)
    s.set_defaults(func=cmd_extract)
    s = sub.add_parser('apply')
    s.add_argument('file')
    s.add_argument('--reviewed', action='store_true', help='строки вычитаны (без пометки needs-review)')
    s.add_argument('--force', action='store_true', help='вносить даже с ошибками проверки')
    s.set_defaults(func=cmd_apply)
    s = sub.add_parser('merge')
    s.add_argument('overlay')
    s.add_argument('catalog')
    s.set_defaults(func=cmd_merge)
    s = sub.add_parser('export')
    s.add_argument('-o', '--output', required=True)
    s.add_argument('--module', help='только модуль (префикс)')
    s.set_defaults(func=cmd_export)
    s = sub.add_parser('prune')
    s.add_argument('--missing', action='store_true', help='также строки, которых больше нет на Transifex')
    s.set_defaults(func=cmd_prune)
    s = sub.add_parser('review-items')
    s.add_argument('-o', '--output', required=True)
    s.add_argument('--batch', default='Вычитка')
    s.add_argument('--module')
    s.set_defaults(func=cmd_review_items)
    s = sub.add_parser('review-apply')
    s.add_argument('dir')
    s.add_argument('--force', action='store_true')
    s.set_defaults(func=cmd_review_apply)
    args = p.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
