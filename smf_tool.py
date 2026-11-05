#!/usr/bin/env python3
import argparse
import binascii
import json
import lzma
import os
import struct
import sys
import time
import zlib

SIGNATURE = 8
HEADER_SIZE = 25
MANIFEST_NAME = '.smf_manifest.json'
METHOD_MASK = 0x30
FLAG_OWNER = 4
FLAG_SHARED = 5
RAW_MARGIN = 11


def xor(buf, key):
    n = len(buf)
    if n == 0:
        return b''
    k = (key * (n // 4 + 1))[:n]
    return (int.from_bytes(buf, 'little') ^ int.from_bytes(k, 'little')).to_bytes(n, 'little')


def enc_name(name):
    return name.encode('utf-8', 'surrogateescape')


def dec_name(raw):
    return raw.decode('utf-8', 'surrogateescape')


class Entry:
    def __init__(self, name, offset, csize, usize, crc, flags):
        self.name = name
        self.offset = offset
        self.csize = csize
        self.usize = usize
        self.crc = crc
        self.flags = flags
        self.block = None


class Archive:
    pass


def read_archive(path):
    data = open(path, 'rb').read()
    if len(data) < HEADER_SIZE:
        raise ValueError('файл слишком короткий для SMF')
    a = Archive()
    a.data = data
    a.byte0 = data[0]
    a.sig = struct.unpack_from('<I', data, 1)[0]
    if a.sig != SIGNATURE:
        raise ValueError('не SMF (сигнатура 0x%08x вместо 0x%08x)' % (a.sig, SIGNATURE))
    a.date = data[5:13]
    a.key = data[13:17]
    count = struct.unpack_from('<I', data, 17)[0]
    a.table_end = struct.unpack_from('<I', data, 21)[0]
    if a.table_end < HEADER_SIZE or a.table_end > len(data):
        raise ValueError('размер таблицы %d выходит за файл' % a.table_end)
    table = xor(data[HEADER_SIZE:a.table_end], a.key)
    a.entries = []
    off = 0
    for i in range(count):
        if off + 4 > len(table):
            raise ValueError('таблица оборвана на записи %d из %d' % (i, count))
        ln = struct.unpack_from('<I', table, off)[0]
        off += 4
        if ln == 0 or ln > 4096 or off + ln + 18 > len(table):
            raise ValueError('запись %d: неверная длина имени %d' % (i, ln))
        name = dec_name(table[off:off + ln])
        off += ln
        do, cs, us, crc, fl = struct.unpack_from('<IIIIH', table, off)
        off += 18
        a.entries.append(Entry(name, do, cs, us, crc, fl))
    if off != len(table):
        raise ValueError('в таблице остались лишние %d байт' % (len(table) - off))
    offsets = sorted({e.offset for e in a.entries})
    rank = {o: i for i, o in enumerate(offsets)}
    for e in a.entries:
        e.block = rank[e.offset]
    a.method = a.byte0 & METHOD_MASK
    return a


def blob_of(a, e):
    start = a.table_end + e.offset
    raw = a.data[start:start + e.csize]
    if len(raw) != e.csize:
        raise ValueError('%s: блок выходит за пределы файла' % e.name)
    return xor(raw, a.key)


def decode_blob(method, blob, csize, usize):
    if csize == 0:
        return b'\x00' * usize if usize else b''
    if usize == csize:
        return blob
    if method == 0x00:
        return zlib.decompress(blob)
    if method == 0x10:
        d = blob[0]
        lc = d % 9
        d //= 9
        lp = d % 5
        pb = d // 5
        dict_size = struct.unpack_from('<I', blob, 1)[0]
        filt = [{'id': lzma.FILTER_LZMA1, 'dict_size': dict_size, 'lc': lc, 'lp': lp, 'pb': pb}]
        return lzma.LZMADecompressor(format=lzma.FORMAT_RAW, filters=filt).decompress(blob[5:])[:usize]
    raise ValueError('неизвестный метод сжатия 0x%02x' % method)


def read_entry(a, e):
    blob = blob_of(a, e)
    if binascii.crc32(blob) & 0xFFFFFFFF != e.crc:
        raise ValueError('%s: CRC32 не сошёлся' % e.name)
    out = decode_blob(a.method, blob, e.csize, e.usize)
    if len(out) != e.usize:
        raise ValueError('%s: после распаковки %d байт вместо %d' % (e.name, len(out), e.usize))
    return out


def compress_data(method, data, stored=None):
    if not data:
        return b'', 0, 0
    if method != 0x00:
        raise ValueError('упаковка для метода сжатия 0x%02x не поддерживается (только zlib)' % method)
    comp = zlib.compress(data, 9)
    if stored is None:
        stored = len(comp) == len(data) or len(comp) >= len(data) + RAW_MARGIN
    if stored:
        return data, len(data), len(data)
    return comp, len(comp), len(data)


def table_crc_key(table):
    return struct.pack('<I', binascii.crc32(table) & 0xFFFFFFFF)


def build_archive(head, key, method, items, force_key=None):
    blocks = {}
    order = []
    for it in items:
        b = it['block']
        if b not in blocks:
            blocks[b] = it['blob']
            order.append(b)
    ordered = sorted(order)
    offsets, pos = {}, 0
    for b in ordered:
        offsets[b] = pos
        pos += len(blocks[b])
    table = bytearray()
    for it in items:
        nm = enc_name(it['name'])
        table += struct.pack('<I', len(nm)) + nm
        table += struct.pack('<IIIIH', offsets[it['block']], it['csize'], it['usize'], it['crc'], it['flags'])
    table_end = HEADER_SIZE + len(table)
    key = force_key if force_key is not None else table_crc_key(bytes(table))
    out = bytearray()
    out += head[:13]
    out += key
    out += struct.pack('<II', len(items), table_end)
    out += xor(bytes(table), key)
    for b in ordered:
        out += xor(blocks[b], key)
    return bytes(out)


def make_item(name, plain, method, block, flags, stored=None):
    blob, cs, us = compress_data(method, plain, stored)
    return {'name': name, 'blob': blob, 'csize': cs, 'usize': us,
            'crc': binascii.crc32(blob) & 0xFFFFFFFF, 'flags': flags, 'block': block}


def items_from_archive(a):
    items = []
    for e in a.entries:
        items.append({'name': e.name, 'blob': blob_of(a, e), 'csize': e.csize, 'usize': e.usize,
                      'crc': e.crc, 'flags': e.flags, 'block': e.block})
    return items


def head_of(a):
    return bytes([a.byte0]) + struct.pack('<I', a.sig) + a.date


def safe_join(base, name):
    parts = [p for p in name.replace('\\', '/').split('/') if p not in ('', '.')]
    if any(p == '..' for p in parts):
        raise ValueError('небезопасное имя в архиве: %s' % name)
    return os.path.join(base, *parts)


def build_keep_layout(a, changed):
    out = bytearray(a.data)
    table = bytearray()
    spans = {}
    for i, e in enumerate(a.entries):
        nm = enc_name(e.name)
        if i in changed:
            it = changed[i]
            off = len(out) - a.table_end
            out += it['blob']
            spans[(off, len(it['blob']))] = True
            cs, us, crc, fl = it['csize'], it['usize'], it['crc'], it['flags']
        else:
            off, cs, us, crc, fl = e.offset, e.csize, e.usize, e.crc, e.flags
            spans[(off, cs)] = False
        table += struct.pack('<I', len(nm)) + nm + struct.pack('<IIIIH', off, cs, us, crc, fl)
    if len(table) != a.table_end - HEADER_SIZE:
        raise ValueError('размер таблицы изменился, режим --keep-layout невозможен')
    key = table_crc_key(bytes(table))
    for (off, ln), is_new in spans.items():
        start = a.table_end + off
        plain = bytes(out[start:start + ln]) if is_new else xor(bytes(out[start:start + ln]), a.key)
        out[start:start + ln] = xor(plain, key)
    out[13:17] = key
    out[HEADER_SIZE:a.table_end] = xor(bytes(table), key)
    return bytes(out)


def cmd_list(args):
    a = read_archive(args.archive)
    print('метод сжатия 0x%02x, дата сборки %s, ключ %s, записей %d, таблица %d байт'
          % (a.method, a.date.decode('ascii', 'replace'), a.key.hex(), len(a.entries), a.table_end))
    for e in a.entries:
        if args.filter and args.filter.lower() not in e.name.lower():
            continue
        print('%10d %10d  флаг %d  блок %-5d %s' % (e.csize, e.usize, e.flags, e.block, e.name))


def table_check(a):
    table = xor(a.data[HEADER_SIZE:a.table_end], a.key)
    crc = binascii.crc32(table) & 0xFFFFFFFF
    key = struct.unpack('<I', a.key)[0]
    return key in (crc, (crc + 0x680) & 0xFFFFFFFF), crc, key


def cmd_verify(args):
    a = read_archive(args.archive)
    ok_table, crc, key = table_check(a)
    print('CRC32 таблицы 0x%08x, значение в заголовке 0x%08x: %s' % (crc, key, 'совпадает' if ok_table else 'НЕ СОВПАДАЕТ (игра сочтёт пакет повреждённым)'))
    bad = 0
    used = 0
    seen = {}
    for e in a.entries:
        try:
            read_entry(a, e)
        except Exception as ex:
            bad += 1
            print('ОШИБКА', ex)
        seen.setdefault(e.offset, e.csize)
    used = a.table_end + sum(seen.values())
    print('записей %d, ошибок %d, блоков %d' % (len(a.entries), bad, len(seen)))
    note = ''
    if used > len(a.data):
        note = '  (ФАЙЛ КОРОЧЕ, ЧЕМ НУЖНО ТАБЛИЦЕ)'
    elif used < len(a.data):
        note = '  (в конце %d лишних байт: например, остатки старых блоков после --keep-layout)' % (len(a.data) - used)
    print('размер файла %d, по таблице %d%s' % (len(a.data), used, note))
    return 1 if bad or used > len(a.data) or not ok_table else 0


def cmd_unpack(args):
    a = read_archive(args.archive)
    os.makedirs(args.outdir, exist_ok=True)
    manifest = {'byte0': a.byte0, 'sig': a.sig, 'date': a.date.decode('latin-1'), 'key': a.key.hex(),
                'entries': []}
    cache = {}
    n = 0
    for e in a.entries:
        if args.filter and args.filter.lower() not in e.name.lower():
            continue
        if e.block not in cache:
            cache = {e.block: read_entry(a, e)}
        out = cache[e.block]
        path = safe_join(args.outdir, e.name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as f:
            f.write(out)
        n += 1
    for e in a.entries:
        manifest['entries'].append({'name': e.name, 'flags': e.flags, 'block': e.block,
                                    'stored': e.csize == e.usize and e.usize > 0})
    if not args.filter:
        with open(os.path.join(args.outdir, MANIFEST_NAME), 'w', encoding='utf-8') as f:
            json.dump(manifest, f, ensure_ascii=False)
    print('распаковано файлов: %d в %s' % (n, args.outdir))


def collect_dir(base):
    found = {}
    for root, dirs, files in os.walk(base):
        dirs.sort()
        for fn in sorted(files):
            if fn == MANIFEST_NAME:
                continue
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, base).replace(os.sep, '/')
            found[rel] = full
    return found


def cmd_pack(args):
    files = collect_dir(args.dir)
    mpath = args.manifest or os.path.join(args.dir, MANIFEST_NAME)
    method = 0x00
    if os.path.isfile(mpath):
        m = json.load(open(mpath, encoding='utf-8'))
        byte0, sig = m['byte0'], m['sig']
        date = m['date'].encode('latin-1')
        key = bytes.fromhex(m['key'])
        method = byte0 & METHOD_MASK
        listed = [(x['name'], x['flags'], x['block'], x.get('stored')) for x in m['entries']]
        next_block = max([x[2] for x in listed] + [-1]) + 1
        missing = [x[0] for x in listed if x[0] not in files]
        if missing and not args.allow_missing:
            raise SystemExit('в папке нет %d файлов из манифеста (например %s); --allow-missing чтобы пропустить'
                             % (len(missing), missing[0]))
        listed = [x for x in listed if x[0] in files]
        known = {x[0] for x in listed}
        extra = sorted(n for n in files if n not in known)
    else:
        byte0 = 0x06
        sig = SIGNATURE
        date = (args.date or time.strftime('%Y%m%d')).encode('ascii')
        key = b'\x00\x00\x00\x00'
        listed = []
        next_block = 0
        extra = sorted(files)
    if len(date) != 8:
        raise SystemExit('дата должна быть из 8 символов')
    items = []
    done = {}
    variants = {}
    for name, flags, block, stored in listed:
        plain = open(files[name], 'rb').read()
        known = variants.setdefault(block, {})
        if plain in known:
            b = known[plain]
            if b != block or len(known) > 1:
                flags = FLAG_SHARED
        elif not known:
            b = block
            known[plain] = b
        else:
            b = next_block
            next_block += 1
            known[plain] = b
            flags = FLAG_OWNER
        if b not in done:
            done[b] = make_item(name, plain, method, b, flags, stored)
        it = dict(done[b])
        it['name'], it['flags'] = name, flags
        items.append(it)
    for name in extra:
        plain = open(files[name], 'rb').read()
        items.append(make_item(name, plain, method, next_block, FLAG_OWNER))
        next_block += 1
    head = bytes([byte0]) + struct.pack('<I', sig) + date
    out = build_archive(head, key, method, items)
    with open(args.output, 'wb') as f:
        f.write(out)
    print('собрано %s: записей %d, блоков %d, %d байт (новых файлов: %d)'
          % (args.output, len(items), len({i['block'] for i in items}), len(out), len(extra)))


def cmd_replace(args):
    a = read_archive(args.archive)
    items = items_from_archive(a)
    index = {it['name']: i for i, it in enumerate(items)}
    keep = {}
    changes = {}
    if args.dir:
        for rel, full in collect_dir(args.dir).items():
            changes[rel] = full
    for spec in args.file or []:
        if '=' not in spec:
            raise SystemExit('--file ждёт вид ИМЯ_В_АРХИВЕ=ПУТЬ, получено %r' % spec)
        nm, path = spec.split('=', 1)
        changes[nm.replace('\\', '/')] = path
    deletes = set(n.replace('\\', '/') for n in (args.delete or []))
    next_block = max(it['block'] for it in items) + 1 if items else 0
    shared = {}
    for it in items:
        shared[it['block']] = shared.get(it['block'], 0) + 1
    replaced = added = 0
    for nm, path in sorted(changes.items()):
        plain = open(path, 'rb').read()
        if nm in index:
            old = items[index[nm]]
            old_plain = decode_blob(a.method, old['blob'], old['csize'], old['usize'])
            if old_plain == plain:
                continue
            if shared[old['block']] > 1:
                shared[old['block']] -= 1
                block, flags = next_block, FLAG_OWNER
                next_block += 1
                shared[block] = 1
            else:
                block, flags = old['block'], old['flags']
            items[index[nm]] = make_item(nm, plain, a.method, block, flags)
            keep[index[nm]] = items[index[nm]]
            replaced += 1
        elif args.no_add:
            print('пропущен новый файл (--no-add): %s' % nm)
        else:
            items.append(make_item(nm, plain, a.method, next_block, FLAG_OWNER))
            index[nm] = len(items) - 1
            shared[next_block] = 1
            next_block += 1
            added += 1
    if args.keep_layout:
        if added or deletes:
            raise ValueError('--keep-layout работает только для замены существующих файлов, без добавления и удаления')
        for i, it in keep.items():
            if shared[items[i]['block']] > 1 or a.entries[i].block != items[i]['block']:
                it['flags'] = FLAG_OWNER
        out = build_keep_layout(a, keep)
        with open(args.output, 'wb') as f:
            f.write(out)
        print('готово %s (старые блоки остались на месте, новые дописаны в конец): заменено %d, %d байт'
              % (args.output, replaced, len(out)))
        return
    removed = 0
    if deletes:
        left = []
        for it in items:
            if it['name'] in deletes:
                removed += 1
            else:
                left.append(it)
        unknown = deletes - {it['name'] for it in items}
        for u in sorted(unknown):
            print('для удаления нет такого файла: %s' % u)
        items = left
    out = build_archive(head_of(a), a.key, a.method, items)
    with open(args.output, 'wb') as f:
        f.write(out)
    print('готово %s: заменено %d, добавлено %d, удалено %d, всего записей %d, %d байт'
          % (args.output, replaced, added, removed, len(items), len(out)))


def cmd_probe(args):
    a = read_archive(args.archive)
    os.makedirs(args.outdir, exist_ok=True)
    items = items_from_archive(a)
    shared = {}
    for it in items:
        shared[it['block']] = shared.get(it['block'], 0) + 1
    pick = None
    for i in range(len(items) - 1, -1, -1):
        it = items[i]
        if shared[it['block']] == 1 and it['csize'] != it['usize'] and 2000 <= it['csize'] <= 60000:
            pick = i
            break
    if pick is None:
        raise ValueError('не нашёл подходящую запись для проб')
    it = items[pick]
    plain = decode_blob(a.method, it['blob'], it['csize'], it['usize'])
    new_blob = None
    for lvl in (1, 3, 5, 0):
        cand = zlib.compress(plain, lvl)
        if len(cand) != it['csize']:
            new_blob = cand
            break
    new_item = dict(it)
    new_item.update(blob=new_blob, csize=len(new_blob), crc=binascii.crc32(new_blob) & 0xFFFFFFFF)
    files = []

    def save(name, blob, text):
        with open(os.path.join(args.outdir, name), 'wb') as f:
            f.write(blob)
        files.append((name, text))

    save('E0_identical.smf', a.data, 'точная копия оригинала (контроль: обязана запускаться)')
    save('E1_trailing_bytes.smf', a.data + b'\x00' * 64, 'оригинал + 64 нулевых байта в конце: меняется только размер файла, данные те же')
    head = bytearray(head_of(a))
    head[12] = head[12] ^ 0x01
    save('E3_other_date.smf', build_archive(bytes(head), a.key, a.method, items),
         'изменён один байт даты сборки в заголовке, всё остальное то же')
    changed = list(items)
    changed[pick] = new_item
    save('E4_layout_shift.smf', build_archive(head_of(a), a.key, a.method, changed),
         'один файл (%s) пересжат иначе, содержимое то же, но размер блока другой и все следующие блоки сдвинуты; CRC таблицы в заголовке пересчитан' % it['name'])
    save('E2_old_crc_bug.smf', build_archive(head_of(a), a.key, a.method, changed, force_key=a.key),
         'то же, что E4, но CRC таблицы в заголовке оставлен старым: так работал прежний упаковщик, игра должна считать такой файл повреждённым')
    save('E5_append_only.smf', build_keep_layout(a, {pick: new_item}),
         'тот же файл (%s) пересжат иначе, но записан в конец файла: смещения остальных записей не менялись' % it['name'])
    with open(os.path.join(args.outdir, 'ЧТО_ДЕЛАТЬ.txt'), 'w', encoding='utf-8') as f:
        f.write('Подставляй файлы по одному вместо S3DMain.smf (имя переименуй в S3DMain.smf), запускай игру и записывай результат.\n'
                'Во всех файлах содержимое ресурсов то же, что в оригинале: ломается может только контейнер.\n\n')
        for n, t in files:
            f.write('%s\n    %s\n' % (n, t))
        f.write('\nКак читать результат:\n'
                '  E0 падает            -> дело не в содержимом: место файла, права, подпись APK, а не в упаковке\n'
                '  E0 ок, E1 падает     -> игра проверяет размер или хэш всего файла\n'
                '  E4 ок, E2 падает     -> причина найдена: игра сверяет CRC таблицы с заголовком (E2 нарочно ломает его)\n'
                '  E3 падает            -> игра проверяет дату сборки в заголовке\n'
                '  E4 падает, E5 ок     -> игра зависит от смещений блоков\n'
                '  E4 и E5 падают       -> игра проверяет хэш или размер файла целиком\n'
                '  все ок               -> контейнер ни при чём, дело в содержимом подменённого файла (параметры звука)\n')
    for n, t in files:
        print('%-24s %s' % (n, t))
    print('инструкция: %s' % os.path.join(args.outdir, 'ЧТО_ДЕЛАТЬ.txt'))


def main():
    ap = argparse.ArgumentParser(description='SMF (ShiVa3D STK_PAK): список, проверка, распаковка, упаковка, замена файлов')
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('list', help='показать записи')
    p.add_argument('archive')
    p.add_argument('--filter', help='показывать только имена, содержащие эту строку')
    p.set_defaults(fn=cmd_list)
    p = sub.add_parser('verify', help='проверить CRC, размеры и раскладку')
    p.add_argument('archive')
    p.set_defaults(fn=cmd_verify)
    p = sub.add_parser('unpack', help='распаковать в папку (рядом кладётся манифест для точной обратной упаковки)')
    p.add_argument('archive')
    p.add_argument('outdir')
    p.add_argument('--filter', help='распаковать только имена, содержащие эту строку (манифест не пишется)')
    p.set_defaults(fn=cmd_unpack)
    p = sub.add_parser('pack', help='собрать архив из папки')
    p.add_argument('dir')
    p.add_argument('output')
    p.add_argument('--manifest', help='манифест от unpack; по умолчанию DIR/%s' % MANIFEST_NAME)
    p.add_argument('--allow-missing', action='store_true', help='пропускать файлы из манифеста, которых нет в папке')
    p.add_argument('--date', help='дата сборки из 8 символов (только без манифеста)')
    p.set_defaults(fn=cmd_pack)
    p = sub.add_parser('probe', help='создать набор контрольных файлов, чтобы выяснить, что именно проверяет игра')
    p.add_argument('archive')
    p.add_argument('outdir')
    p.set_defaults(fn=cmd_probe)
    p = sub.add_parser('replace', help='заменить, добавить или удалить файлы в готовом архиве, остальное не трогая')
    p.add_argument('archive')
    p.add_argument('-o', '--output', required=True)
    p.add_argument('--dir', help='папка с файлами: совпавшие по пути заменяются, остальные добавляются')
    p.add_argument('--file', action='append', help='ИМЯ_В_АРХИВЕ=ПУТЬ, можно несколько раз')
    p.add_argument('--delete', action='append', help='удалить запись с этим именем, можно несколько раз')
    p.add_argument('--no-add', action='store_true', help='не добавлять новые файлы, только заменять существующие')
    p.add_argument('--keep-layout', action='store_true', help='не сдвигать остальные блоки: новые данные дописываются в конец файла, смещения прочих записей не меняются')
    p.set_defaults(fn=cmd_replace)
    args = ap.parse_args()
    try:
        rc = args.fn(args)
    except (ValueError, OSError, zlib.error) as ex:
        print('ошибка: %s' % ex)
        sys.exit(1)
    sys.exit(rc or 0)


if __name__ == '__main__':
    main()
