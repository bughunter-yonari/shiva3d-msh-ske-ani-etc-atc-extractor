#!/usr/bin/env python3
import argparse
import itertools
import json
import math
import os
import re
import struct
import sys
import zlib

VERT_START = 0x4C
TEX_EXTS = ('.atc', '.etc', '.ktx', '.pvr', '.tex', '.dds')


def pad4(b, fill=b'\x00'):
    return b + fill * ((-len(b)) % 4)


def make_png(w, h, rgba, with_alpha=True):
    if with_alpha:
        ch, ctype, raw_px = 4, 6, rgba
    else:
        rgb = bytearray(w * h * 3)
        rgb[0::3], rgb[1::3], rgb[2::3] = rgba[0::4], rgba[1::4], rgba[2::4]
        ch, ctype, raw_px = 3, 2, rgb
    row = w * ch
    raw = b''.join(b'\x00' + bytes(raw_px[y * row:(y + 1) * row]) for y in range(h))

    def chunk(t, data):
        c = struct.pack('>I', len(data)) + t + data
        return c + struct.pack('>I', zlib.crc32(t + data) & 0xffffffff)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, ctype, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(raw, 6)) + chunk(b'IEND', b''))


def qmul(a, b):
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (aw * bx + ax * bw + ay * bz - az * by, aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw, aw * bw - ax * bx - ay * by - az * bz)


def qrot(q, v):
    x, y, z, w = q
    t = (2 * (y * v[2] - z * v[1]), 2 * (z * v[0] - x * v[2]), 2 * (x * v[1] - y * v[0]))
    return (v[0] + w * t[0] + (y * t[2] - z * t[1]), v[1] + w * t[1] + (z * t[0] - x * t[2]),
            v[2] + w * t[2] + (x * t[1] - y * t[0]))


def qnorm(q):
    l = math.sqrt(sum(c * c for c in q)) or 1.0
    return tuple(c / l for c in q)


def qmat(q):
    x, y, z, w = q
    return [[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]]


def short_name(n):
    n = re.sub(r'_id$', '', n)
    return re.sub(r'^bone_', '', n)


class Mesh:
    pass


VERTEX_LAYOUTS = [('float', 0, 40, True), ('float', 0, 32, False),
                  ('packed', 16, 30, True), ('packed', 16, 22, False),
                  ('packed', 32, 34, True), ('packed', 32, 26, False)]


def parse_msh(path):
    d = open(path, 'rb').read()
    if len(d) < 0x50:
        raise ValueError('файл слишком короткий')
    u32 = lambda o: struct.unpack_from('<I', d, o)[0]
    FF = b'\xff\xff\xff\xff'
    m = Mesh()
    m.version, m.tag = struct.unpack_from('<II', d, 0)
    classic = (m.tag & 0xFFFFFF) == 0x586350
    if classic:
        m.radius = struct.unpack_from('<f', d, 0x18)[0]
        m.bbox_min = struct.unpack_from('<3f', d, 0x1C)
        m.bbox_max = struct.unpack_from('<3f', d, 0x28)
    flag_sizes = (3, 2) if classic else (2, 3)
    nsub = u32(0x34)
    if not 1 <= nsub <= 64:
        raise ValueError('странное число подмешей: %d' % nsub)
    m.submeshes = nsub
    pos, nor, uv, wts, bones, tris = [], [], [], [], [], []
    m.bone_ids = []
    gmap = {}
    m.stride = None
    m.layout = None

    def rest_ok(p, skin):
        if skin:
            if p + 4 > len(d):
                return False
            pc = u32(p)
            if pc > 4096:
                return False
            p += 4 + pc * 4
        p += 24
        if p > len(d):
            return False
        if p == len(d) or d[p + 8:p + 12] == FF:
            return True
        return p + 4 <= len(d) and p + 4 + u32(p) == len(d)

    o = 0x38
    for sidx in range(nsub):
        o += 8
        if d[o:o + 4] != FF:
            raise ValueError('подмеш %d: нет маркера FF FF FF FF по смещению 0x%X' % (sidx, o))
        n = u32(o + 5)
        if not 0 < n < 1000000:
            raise ValueError('подмеш %d: странное число вершин %d' % (sidx, n))
        found = None
        for indexed in (True, False):
            for fb in flag_sizes:
                vs = o + 9 + fb
                for kind, hdr, st, skin in VERTEX_LAYOUTS:
                    end = vs + hdr + n * st
                    if end > len(d):
                        continue
                    if indexed:
                        if end + 10 > len(d) or d[end:end + 4] != FF:
                            continue
                        cnt, isz = u32(end + 5), d[end + 9]
                        if isz in (2, 4) and cnt % 3 == 0 and end + 10 + cnt * isz <= len(d):
                            found = (kind, hdr, st, skin, vs, end, cnt, isz, True)
                    elif rest_ok(end, skin):
                        found = (kind, hdr, st, skin, vs, end, 0, 0, False)
                    if found:
                        break
                if found:
                    break
            if found:
                break
        if found is None:
            raise ValueError('подмеш %d: не нашёл конец блока вершин: %d вершин, данные не сходятся ни с одним '
                             'известным форматом (stride %s). Первые байты после заголовка: %s'
                             % (sidx, n, '/'.join(str(l[2]) for l in VERTEX_LAYOUTS), d[o + 9:o + 41].hex(' ')))
        kind, hdr, st, skin, vs, e, cnt, isz, indexed = found
        m.stride = m.stride or st
        m.layout = m.layout or kind
        if indexed:
            idx = struct.unpack_from('<%d%s' % (cnt, 'H' if isz == 2 else 'I'), d, e + 10)
            if cnt and max(idx) >= n:
                raise ValueError('подмеш %d: индекс за пределами вершин' % sidx)
            t = e + 10 + cnt * isz
        else:
            idx = tuple(range(n - n % 3))
            cnt = len(idx)
            t = e
        ids = ()
        if skin:
            pc = u32(t)
            if pc > 4096 or t + 4 + pc * 4 + 24 > len(d):
                raise ValueError('подмеш %d: палитра костей не сошлась (%d)' % (sidx, pc))
            ids = struct.unpack_from('<%dI' % pc, d, t + 4)
            for bid in ids:
                if bid not in gmap:
                    gmap[bid] = len(m.bone_ids)
                    m.bone_ids.append(bid)
            o = t + 4 + pc * 4 + 24
        else:
            o = t + 24
        base = len(pos)
        if kind == 'packed':
            u0, v0, u1, v1 = struct.unpack_from('<4f', d, vs)
            if u1 < u0 or v1 < v0:
                u1, v1 = u0 + u1, v0 + v1
        for i in range(n):
            q = vs + hdr + i * st
            pos.append(struct.unpack_from('<3f', d, q))
            if kind == 'float':
                nor.append(struct.unpack_from('<3f', d, q + 12))
                uv.append(struct.unpack_from('<2f', d, q + 24))
                w0 = q + 32
            else:
                nx, ny, nz = struct.unpack_from('<3h', d, q + 12)
                nor.append(tuple(max(-1.0, c / 32767.0) for c in (nx, ny, nz)))
                ru, rv = struct.unpack_from('<2H', d, q + 18)
                uv.append((u0 + (u1 - u0) * ru / 65535.0, v0 + (v1 - v0) * rv / 65535.0))
                w0 = q + 18 + 4 * (hdr // 16)
            if skin:
                wts.append(tuple(d[w0:w0 + 4]))
                bones.append(tuple(gmap[ids[b_]] if b_ < len(ids) else 0 for b_ in d[w0 + 4:w0 + 8]))
        tris += [tuple(base + v for v in idx[k:k + 3]) for k in range(0, cnt, 3)]
    if wts and len(wts) != len(pos):
        raise ValueError('часть подмешей со скелетом, часть без: такое сочетание пока не поддерживается')
    m.pos, m.nor, m.uv, m.wts, m.bones, m.tris = pos, nor, uv, wts, bones, tris
    if not classic:
        m.bbox_min = tuple(min(p[k] for p in pos) for k in range(3))
        m.bbox_max = tuple(max(p[k] for p in pos) for k in range(3))
        m.radius = max(math.dist(m.bbox_min, m.bbox_max) / 2, 1e-6)
    m.skeleton = None
    try:
        ln = u32(o)
        if o + 4 + ln == len(d):
            m.skeleton = d[o + 4:].decode('ascii', 'replace')
    except struct.error:
        pass
    if m.submeshes > 1:
        m.note = '%d подмешей слиты в один, палитры костей объединены (%d костей)' % (nsub, len(m.bone_ids))
    return m


class Bone:
    pass


def parse_ske(path):
    d = open(path, 'rb').read()
    version = struct.unpack_from('<I', d, 0)[0]
    n = struct.unpack_from('<I', d, 12)[0]
    if not 0 < n < 4096:
        raise ValueError('странное число костей: %d' % n)
    o = 16
    bones = {}
    for _ in range(n):
        sz = struct.unpack_from('<I', d, o)[0]
        r = d[o + 4:o + 4 + sz]
        b = Bone()
        b.id = struct.unpack_from('<I', r, 0)[0]
        f = struct.unpack_from('<15f', r, 4)
        b.pos, b.quat, b.extra = f[0:3], f[3:7], f[7:]
        ln = struct.unpack_from('<I', r, 64)[0]
        b.name = r[68:68 + ln].decode('ascii', 'replace')
        bones[b.id] = b
        o += 4 + sz
    if o != len(d):
        print('  предупреждение: в .ske осталось %d непрочитанных байт' % (len(d) - o))
    return version, bones


_LIMB_CHAINS = {
    'arm': ['clavicle', 'shoulder', 'upperarm', 'arm', 'forearm', 'elbow', 'hand', 'wrist', 'palm', 'finger'],
    'leg': ['thigh', 'upleg', 'knee', 'leg', 'calf', 'shin', 'horselink', 'foot', 'ankle', 'toe'],
}


def name_expected_parents(bones, ids):
    info = {}
    for bid in ids:
        nm = short_name(bones[bid].name).lower()
        toks = [t for t in re.split(r'[_\W]+|(?<=[a-z])(?=\d)', nm) if t]
        side = 'l' if ('l' in toks or 'left' in toks or nm.startswith('left')) else \
            ('r' if ('r' in toks or 'right' in toks or nm.startswith('right')) else '')
        info[bid] = (nm, toks, side)
    exp = {}
    groups = {}
    for bid, (nm, toks, side) in info.items():
        for chain, seq in _LIMB_CHAINS.items():
            hit = [seq.index(t) for t in toks if t in seq]
            if hit:
                rank = min(hit) + (0.5 if 'end' in toks else 0)
                groups.setdefault((chain, side), []).append((rank, bid))
                break
    for members in groups.values():
        members.sort()
        for (r0, b0), (r1, b1) in zip(members, members[1:]):
            if r1 > r0:
                exp.setdefault(b1, b0)
    num = {}
    for bid, (nm, toks, side) in info.items():
        base = re.sub(r'\d+', '', nm)
        m_ = re.search(r'(\d+)(?:_[a-z]+)*$', nm)
        num.setdefault(base, []).append((int(m_.group(1)) if m_ else None, bid))
    for members in num.values():
        nums = [n_ for n_, _ in members if n_ is not None]
        none_as = 0 if 1 in nums else 1
        seq = sorted(((none_as if n_ is None else n_), bid) for n_, bid in members)
        for (n0, b0), (n1, b1) in zip(seq, seq[1:]):
            same_place = all(abs(x - y) < 1e-3 for x, y in zip(bones[b0].pos, bones[b1].pos))
            if n1 == n0 + 1 and not same_place:
                exp.setdefault(b1, b0)
    return exp


def infer_hierarchy(mesh, bones, qmode='auto', beam=100, log=print):
    pal = mesh.bone_ids
    for b in pal:
        if b not in bones:
            raise ValueError('кость с id %08x есть в .msh, но нет в .ske' % b)
    pal_set = set(pal)
    missing = [b for b in bones if b not in pal_set]
    per = [[] for _ in pal]
    strong = [[] for _ in pal]
    for v, (bs, ws) in enumerate(zip(mesh.bones, mesh.wts)):
        for s in range(4):
            if ws[s] > 0 and bs[s] < len(pal):
                per[bs[s]].append(v)
                if ws[s] > 100:
                    strong[bs[s]].append(v)
    samples = []
    for k in range(len(pal)):
        vs = strong[k] or per[k]
        if len(vs) > 60:
            step = len(vs) / 60.0
            vs = [vs[int(i * step)] for i in range(60)]
        samples.append([mesh.pos[v] for v in vs])
    by_id = {bid: samples[k] for k, bid in enumerate(pal)}
    co = set()
    for bs, ws in zip(mesh.bones, mesh.wts):
        act = sorted({bs[s_] for s_ in range(4) if ws[s_] > 0 and bs[s_] < len(pal)})
        for i_ in range(len(act)):
            for j_ in range(i_ + 1, len(act)):
                co.add((pal[act[i_]], pal[act[j_]]))
    has_verts = {bid for k, bid in enumerate(pal) if samples[k]}

    def no_share(a_, b_):
        if a_ not in has_verts or b_ not in has_verts:
            return 0.0
        return 0.0 if ((a_, b_) in co or (b_, a_) in co) else 1.0

    twin = {}
    for k, bid in enumerate(pal):
        t = bones[bid].pos
        if max(abs(c) for c in t) < 1e-3:
            continue
        for k2 in range(k):
            t2 = bones[pal[k2]].pos
            if all(abs(t[i] - t2[i]) < 1e-4 for i in range(3)):
                twin[k] = (pal[k2], 0.2)
                break
            neg = [i for i in range(3) if abs(t[i] + t2[i]) < 0.01 and abs(t[i]) > 1e-3]
            if len(neg) == 1 and all(abs(t[i] - t2[i]) < 0.01 for i in range(3) if i != neg[0]):
                twin[k] = (pal[k2], 0.05)
                break

    def local(mode):
        out = {}
        for bid, b in bones.items():
            q = b.quat
            if mode == 'conj':
                q = (-q[0], -q[1], -q[2], q[3])
            out[bid] = (tuple(b.pos), qnorm(q))
        return out

    def fit(k, p, s=None):
        s = samples[k] if s is None else s
        if not s:
            return 0.0
        best = 1e18
        for v in s:
            dx, dy, dz = p[0] - v[0], p[1] - v[1], p[2] - v[2]
            d2 = dx * dx + dy * dy + dz * dz
            if d2 < best:
                best = d2
        return math.sqrt(best)

    expected = name_expected_parents(bones, pal)
    MAXINS = min(len(missing), 6)
    SHARE_W = 0.15 * max(1.0, math.dist(mesh.bbox_min, mesh.bbox_max) / 5.0)
    NAME_W = 0.25 * max(1.0, math.dist(mesh.bbox_min, mesh.bbox_max) / 5.0)

    def run(LT, width):
        states = [(0.0, (), None, tuple(missing))]
        for k, bid in enumerate(pal):
            allst = list(states)
            cur = states
            for _ in range(MAXINS):
                nxt = []
                for cost, stk, lk, rem in cur:
                    for mi, mb in enumerate(rem):
                        t, q = LT[mb]
                        rem2 = rem[:mi] + rem[mi + 1:]
                        if not stk:
                            nxt.append((cost + 0.001, ((mb, (t, q)),), (mb, None, lk), rem2))
                            continue
                        for p in range(len(stk)):
                            pid, (pt, pq) = stk[p]
                            wt = tuple(a_ + b_ for a_, b_ in zip(pt, qrot(pq, t)))
                            nxt.append((cost + 0.001 + 0.003 * (len(stk) - 1 - p),
                                        stk[:p + 1] + ((mb, (wt, qmul(pq, q))),), (mb, pid, lk), rem2))
                nxt.sort(key=lambda s_: s_[0])
                cur = nxt[:width]
                allst += cur
            allst.sort(key=lambda s_: s_[0])
            states = allst[:width]
            t, q = LT[bid]
            new = []
            tw, tww = twin.get(k, (None, 0.0))
            for cost, stk, lk, rem in states:
                twp = None
                if tw is not None:
                    l = lk
                    while l:
                        if l[0] == tw:
                            twp = l[1]
                            break
                        l = l[2]
                if not stk:
                    new.append((cost + fit(k, t), ((bid, (t, q)),), (bid, None, lk), rem))
                    continue
                for p in range(len(stk)):
                    pid, (pt, pq) = stk[p]
                    wt = tuple(a_ + b_ for a_, b_ in zip(pt, qrot(pq, t)))
                    c = cost + fit(k, wt) + 0.5 * fit(0, wt, by_id.get(pid)) + 0.003 * (len(stk) - 1 - p) + SHARE_W * no_share(pid, bid) - (NAME_W if expected.get(bid) == pid else 0.0)
                    if twp is not None and pid != twp:
                        c += tww
                    new.append((c, stk[:p + 1] + ((bid, (wt, qmul(pq, q))),), (bid, pid, lk), rem))
            new.sort(key=lambda s_: s_[0])
            states = new[:width]
        states = [(c + 0.05 * len(rem), stk, lk, rem) for c, stk, lk, rem in states]
        states.sort(key=lambda s_: s_[0])
        cost, stk, lk, rem = states[0]
        seq = []
        parents = {}
        l = lk
        while l:
            parents[l[0]] = l[1]
            seq.append(l[0])
            l = l[2]
        seq.reverse()
        root = seq[0]
        for mb in rem:
            parents[mb] = root
            seq.append(mb)
        alts = {}
        for c2, _, l2, _r in states:
            if c2 > cost + 0.03:
                break
            while l2:
                alts.setdefault(l2[0], set()).add(l2[1])
                l2 = l2[2]
        run.alts = {b_: s_ for b_, s_ in alts.items() if len(s_) > 1}
        return cost, parents, seq

    modes = [qmode] if qmode in ('conj', 'plain') else ['conj', 'plain']
    best = None
    for mode in modes:
        cost, parents, seq = run(local(mode), beam)
        if best is None or cost < best[0]:
            best = (cost, mode, parents, seq, dict(run.alts))
    _, mode, parents, seq, alts = best
    infer_hierarchy.alts = alts
    return parents, seq, mode


def skeleton_worlds(bones, parents, order, mode):
    LT = {}
    for bid in order:
        q = bones[bid].quat
        if mode == 'conj':
            q = (-q[0], -q[1], -q[2], q[3])
        LT[bid] = (tuple(bones[bid].pos), qnorm(q))
    W = {}

    def g(b):
        if b in W:
            return W[b]
        t, q = LT[b]
        p = parents[b]
        if p is None:
            W[b] = (t, q)
        else:
            pt, pq = g(p)
            W[b] = (tuple(x + y for x, y in zip(pt, qrot(pq, t))), qmul(pq, q))
        return W[b]
    for b in order:
        g(b)
    return LT, W


def template_biped(mesh, bones, qmode='auto', log=print):
    names = {bid: re.sub(r'^bone_', '', re.sub(r'_id$', '', b.name)) for bid, b in bones.items()}
    if sum(1 for n in names.values() if re.match(r'Bip\d+', n)) < len(bones) * 0.6:
        return None
    byname = {n: bid for bid, n in names.items()}
    root = next((n for n in names.values() if re.fullmatch(r'Bip\d+', n)), None)
    if root is None:
        return None
    has = lambda n: n in byname

    def first(*cands):
        for c in cands:
            if c and has(c):
                return c
        return root

    spines = sorted((n for n in names.values() if re.fullmatch(root + r'_Spine\d*', n)),
                    key=lambda n: int(re.search(r'(\d*)$', n).group(1) or 0))
    last_spine = spines[-1] if spines else first(root + '_Pelvis')
    necks = sorted((n for n in names.values() if re.fullmatch(root + r'_Neck\d*', n)),
                   key=lambda n: int(re.search(r'(\d*)$', n).group(1) or 0))
    last_neck = necks[-1] if necks else last_spine

    def parent_of(n, thigh_p, tail_p):
        if n == root:
            return None
        m_ = re.fullmatch(root + r'_(.+)', n)
        if not m_:
            return root
        r = m_.group(1)
        if r == 'Pelvis':
            return root
        if r.startswith('Spine'):
            k = int(r[5:] or 0) if r != 'Spine' else 0
            if r == 'Spine':
                return first(root + '_Pelvis')
            return first(root + ('_Spine%d' % (k - 1) if k > 1 else '_Spine'))
        if re.fullmatch(r'Neck\d*', r):
            k = int(r[4:] or 0)
            return last_spine if k <= 1 and r in ('Neck', 'Neck1') and not (r == 'Neck1' and has(root + '_Neck')) \
                else first(root + ('_Neck%d' % (k - 1) if k > 1 else '_Neck'))
        if r == 'Head':
            return last_neck
        m2 = re.fullmatch(r'Ponytail(\d+)(Nub)?', r)
        if m2:
            k = int(m2.group(1))
            if m2.group(2):
                return first(root + '_Ponytail%d' % k)
            return first(root + '_Ponytail%d' % (k - 1)) if k > 1 else first(root + '_Head')
        m2 = re.fullmatch(r'([LR])_(.+)', r)
        if m2:
            side, part = m2.groups()
            pre = root + '_' + side + '_'
            if part == 'Clavicle':
                return last_neck
            if part == 'UpperArm':
                return first(pre + 'Clavicle', last_neck)
            if part == 'Forearm':
                return first(pre + 'UpperArm')
            if part == 'Hand':
                return first(pre + 'Forearm')
            m3 = re.fullmatch(r'Finger(\d)(\d?)(Nub)?', part)
            if m3:
                f, seg, nub = m3.groups()
                if nub:
                    return first(pre + 'Finger' + f + seg)
                if seg:
                    return first(pre + 'Finger' + f + (str(int(seg) - 1) if int(seg) > 1 else ''))
                return first(pre + 'Hand')
            if part == 'Thigh':
                return thigh_p
            if part == 'Calf':
                return first(pre + 'Thigh')
            if part == 'HorseLink':
                return first(pre + 'Calf')
            if part == 'Foot':
                return first(pre + 'HorseLink', pre + 'Calf')
            m3 = re.fullmatch(r'Toe(\d)(\d?)(Nub)?', part)
            if m3:
                f, seg, nub = m3.groups()
                if nub or seg:
                    return first(pre + 'Toe' + f + (seg if nub and seg else ''), pre + 'Foot')
                return first(pre + 'Foot')
            return first(pre + 'Hand')
        m2 = re.fullmatch(r'Tail(\d*)', r)
        if m2:
            k = int(m2.group(1) or 0)
            return tail_p if k == 0 else first(root + ('_Tail%d' % (k - 1) if k > 1 else '_Tail'))
        return root

    pal = mesh.bone_ids
    pal_index = {b: k for k, b in enumerate(pal)}
    samples = {}
    for k, bid in enumerate(pal):
        vs = [mesh.pos[v] for v in range(len(mesh.pos)) for s in range(4)
              if mesh.bones[v][s] == k and mesh.wts[v][s] > 100]
        samples[bid] = vs[::max(1, len(vs) // 60)]
    best = None
    cands_t = [c for c in (root + '_Pelvis', last_spine, spines[0] if spines else None, root) if c and has(c)]
    cands_t = list(dict.fromkeys(cands_t))
    for mode in ([qmode] if qmode in ('conj', 'plain') else ['conj', 'plain']):
        for th in cands_t:
            for tl in cands_t:
                parents = {bid: (byname[parent_of(n, th, tl)] if parent_of(n, th, tl) else None)
                           for bid, n in names.items()}
                order, seen = [], set()

                def vis(x):
                    if x in seen:
                        return
                    if parents[x] is not None:
                        vis(parents[x])
                    seen.add(x)
                    order.append(x)
                for x in sorted(parents, key=lambda b_: (pal_index.get(b_, -1), names[b_])):
                    vis(x)
                _, W = skeleton_worlds(bones, parents, order, mode)
                fits = [min(math.dist(W[b_][0], p) for p in samples[b_]) for b_ in pal if samples[b_]]
                score = sum(fits) / max(1, len(fits))
                if best is None or score < best[0]:
                    best = (score, mode, parents, order, th, tl)
    score, mode, parents, order, th, tl = best
    log('  шаблон Biped: бёдра на %s, хвост на %s' % (th, tl))
    return parents, order, mode


class Skeleton:
    pass


def build_skeleton(mesh, bones, qmode='auto', beam=100, overrides=None, log=print, use_template=True):
    tpl = template_biped(mesh, bones, qmode, log) if use_template else None
    method = 'шаблон Biped (3ds Max)' if tpl else 'подбор по геометрии'
    parents, order, mode = tpl if tpl else infer_hierarchy(mesh, bones, qmode, beam, log)
    if overrides:
        byname = {}
        for bid, b in bones.items():
            byname[b.name] = bid
            byname[short_name(b.name)] = bid
        for child, par in overrides.items():
            if child not in byname or (par is not None and par not in byname):
                log('  предупреждение: в --parents неизвестная кость: %s -> %s' % (child, par))
                continue
            parents[byname[child]] = byname[par] if par is not None else None
        seen, out = set(), []

        def visit(b):
            if b in seen:
                return
            if parents[b] is not None:
                visit(parents[b])
            seen.add(b)
            out.append(b)
        for b in order:
            visit(b)
        order = out
    LT, W = skeleton_worlds(bones, parents, order, mode)
    sk = Skeleton()
    sk.bones, sk.parents, sk.order, sk.mode, sk.method = bones, parents, order, mode, method
    sk.local, sk.world = LT, W
    sk.palette = list(mesh.bone_ids)
    sk.ambiguous = {} if tpl else {b: sorted(p) for b, p in getattr(infer_hierarchy, 'alts', {}).items()}
    pos = mesh.pos
    sk.fit = {}
    for k, bid in enumerate(sk.palette):
        vs = [pos[v] for v in range(len(pos)) for s in range(4)
              if mesh.bones[v][s] == k and mesh.wts[v][s] > 100]
        if vs:
            p = W[bid][0]
            sk.fit[bid] = min(math.dist(p, v) for v in vs)
    return sk


def skeleton_tree(sk):
    kids = {}
    for b in sk.order:
        kids.setdefault(sk.parents[b], []).append(b)
    lines = []

    def rec(b, depth):
        f = sk.fit.get(b)
        lines.append('%s%s%s' % ('  ' * depth, short_name(sk.bones[b].name),
                                 '' if f is None else '  (посадка %.3f)' % f))
        for c in kids.get(b, []):
            rec(c, depth + 1)
    for r in kids.get(None, []):
        rec(r, 0)
    return '\n'.join(lines)


def _c565(c):
    return ((c >> 11) & 31) * 255 // 31, ((c >> 5) & 63) * 255 // 63, (c & 31) * 255 // 31


def _c555(c):
    return ((c >> 10) & 31) * 255 // 31, ((c >> 5) & 31) * 255 // 31, (c & 31) * 255 // 31


def _atc_color(blk):
    c0, c1, idx = struct.unpack('<HHI', blk)
    e1 = _c565(c1)
    if c0 & 0x8000:
        e0 = _c555(c0 & 0x7fff)
        pal = [(0, 0, 0), tuple(max(0, e0[k] - e1[k] // 4) for k in range(3)), e0, e1]
    else:
        e0 = _c555(c0)
        pal = [e0, tuple((5 * e0[k] + 3 * e1[k]) // 8 for k in range(3)),
               tuple((3 * e0[k] + 5 * e1[k]) // 8 for k in range(3)), e1]
    return [pal[(idx >> (2 * i)) & 3] for i in range(16)]


def _dxt5_alpha(blk):
    a0, a1 = blk[0], blk[1]
    bits = int.from_bytes(blk[2:8], 'little')
    if a0 > a1:
        tab = [a0, a1] + [((8 - i) * a0 + (i - 1) * a1) // 7 for i in range(2, 8)]
    else:
        tab = [a0, a1] + [((6 - i) * a0 + (i - 1) * a1) // 5 for i in range(2, 6)] + [0, 255]
    return [tab[(bits >> (3 * i)) & 7] for i in range(16)]


def decode_atc(data, w, h, kind):
    px = bytearray(w * h * 4)
    bsz = 8 if kind == 'rgb' else 16
    o = 0
    for by in range(h // 4):
        for bx in range(w // 4):
            if kind == 'rgb':
                al, col = [255] * 16, _atc_color(data[o:o + 8])
            elif kind == 'explicit':
                ab = struct.unpack_from('<Q', data, o)[0]
                al, col = [((ab >> (4 * i)) & 15) * 17 for i in range(16)], _atc_color(data[o + 8:o + 16])
            else:
                al, col = _dxt5_alpha(data[o:o + 8]), _atc_color(data[o + 8:o + 16])
            o += bsz
            for i in range(16):
                p = ((by * 4 + (i >> 2)) * w + bx * 4 + (i & 3)) * 4
                c = col[i]
                px[p], px[p + 1], px[p + 2], px[p + 3] = c[0], c[1], c[2], al[i]
    return px


_ETC_MOD = [[2, 8], [5, 17], [9, 29], [13, 42], [18, 60], [24, 80], [33, 106], [47, 183]]


def decode_etc1(data, w, h):
    px = bytearray(w * h * 4)
    px[3::4] = b'\xff' * (w * h)
    o = 0
    for by in range(0, h, 4):
        for bx in range(0, w, 4):
            b = data[o:o + 8]
            o += 8
            diff, flip = (b[3] >> 1) & 1, b[3] & 1
            cw = ((b[3] >> 5) & 7, (b[3] >> 2) & 7)
            if diff:
                r1, g1, b1 = b[0] >> 3, b[1] >> 3, b[2] >> 3

                def sx(v):
                    return v - 8 if v >= 4 else v
                r2, g2, b2 = (r1 + sx(b[0] & 7)) & 31, (g1 + sx(b[1] & 7)) & 31, (b1 + sx(b[2] & 7)) & 31
                e5 = lambda v: (v << 3) | (v >> 2)
                cols = ((e5(r1), e5(g1), e5(b1)), (e5(r2), e5(g2), e5(b2)))
            else:
                e4 = lambda v: (v << 4) | v
                cols = ((e4(b[0] >> 4), e4(b[1] >> 4), e4(b[2] >> 4)),
                        (e4(b[0] & 15), e4(b[1] & 15), e4(b[2] & 15)))
            msb, lsb = (b[4] << 8) | b[5], (b[6] << 8) | b[7]
            for x in range(4):
                for y in range(4):
                    i = x * 4 + y
                    idx = (((msb >> i) & 1) << 1) | ((lsb >> i) & 1)
                    sub = (0 if x < 2 else 1) if not flip else (0 if y < 2 else 1)
                    t = _ETC_MOD[cw[sub]]
                    mod = (t[0], t[1], -t[0], -t[1])[idx]
                    c = cols[sub]
                    p = ((by + y) * w + bx + x) * 4
                    px[p] = min(255, max(0, c[0] + mod))
                    px[p + 1] = min(255, max(0, c[1] + mod))
                    px[p + 2] = min(255, max(0, c[2] + mod))
    return px


def decode_raw(data, w, h, kind):
    px = bytearray(w * h * 4)
    n = w * h
    if kind == 'rgba8':
        px[:] = data[:n * 4]
    elif kind == 'rgb8':
        px[0::4], px[1::4], px[2::4], px[3::4] = data[0:n * 3:3], data[1:n * 3:3], data[2:n * 3:3], b'\xff' * n
    elif kind == 'rgb565':
        for i in range(n):
            c = _c565(struct.unpack_from('<H', data, i * 2)[0])
            px[i * 4:i * 4 + 4] = bytes((c[0], c[1], c[2], 255))
    return px


class TextureError(Exception):
    pass


def _tex_ktx(d):
    if struct.unpack_from('<I', d, 12)[0] != 0x04030201:
        raise TextureError('KTX с big-endian не поддерживается')
    gl_type, _, gl_fmt, gl_int, _, w, h = struct.unpack_from('<7I', d, 16)
    kv = struct.unpack_from('<I', d, 60)[0]
    o = 64 + kv
    size = struct.unpack_from('<I', d, o)[0]
    data = d[o + 4:o + 4 + size]
    table = {0x8C92: ('ATC RGB', lambda: decode_atc(data, w, h, 'rgb')),
             0x8C93: ('ATC RGBA explicit', lambda: decode_atc(data, w, h, 'explicit')),
             0x87EE: ('ATC RGBA interpolated', lambda: decode_atc(data, w, h, 'interp')),
             0x8D64: ('ETC1', lambda: decode_etc1(data, w, h)),
             0x1908: ('RGBA8', lambda: decode_raw(data, w, h, 'rgba8')),
             0x1907: ('RGB8', lambda: decode_raw(data, w, h, 'rgb8'))}
    if gl_int not in table:
        if gl_int == 0 and gl_fmt in table and gl_type == 0x1401:
            gl_int = gl_fmt
        else:
            raise TextureError('KTX: неизвестный glInternalFormat 0x%X' % gl_int)
    name, fn = table[gl_int]
    return w, h, fn(), 'KTX / ' + name


def _tex_pvr2(d):
    hs, h, w, mips, flags, dsz, bpp = struct.unpack_from('<7I', d, 0)
    ptype = flags & 0xff
    data = d[hs:hs + dsz]
    table = {0x36: ('ETC1', lambda: decode_etc1(data, w, h)),
             0x12: ('RGBA8888', lambda: decode_raw(data, w, h, 'rgba8')),
             0x15: ('RGB888', lambda: decode_raw(data, w, h, 'rgb8')),
             0x13: ('RGB565', lambda: decode_raw(data, w, h, 'rgb565'))}
    if ptype not in table:
        raise TextureError('PVR v2: неподдерживаемый тип пикселей 0x%02X (flags 0x%X, %dx%d, %d bpp)'
                           % (ptype, flags, w, h, bpp))
    name, fn = table[ptype]
    return w, h, fn(), 'PVR v2 / ' + name


def _tex_pvr3(d):
    ver, flags, pf, cs, ct, h, w, depth, nsurf, nfaces, mips, meta = struct.unpack_from('<IIQIIIIIIIII', d, 0)
    data = d[52 + meta:]
    table = {6: ('ETC1', lambda: decode_etc1(data, w, h))}
    if pf not in table:
        names = {0: 'PVRTC 2bpp RGB', 1: 'PVRTC 2bpp RGBA', 2: 'PVRTC 4bpp RGB', 3: 'PVRTC 4bpp RGBA',
                 7: 'DXT1', 9: 'DXT3', 11: 'DXT5', 22: 'ETC2 RGB', 23: 'ETC2 RGBA', 24: 'ETC2 RGB A1'}
        raise TextureError('PVR v3: формат пикселей %d (%s) пока не поддерживается, %dx%d'
                           % (pf, names.get(pf, 'неизвестный'), w, h))
    name, fn = table[pf]
    return w, h, fn(), 'PVR v3 / ' + name


TEXTURE_FORMATS = [
    (lambda d: d[1:7] == b'KTX 11', _tex_ktx),
    (lambda d: len(d) > 52 and d[44:48] == b'PVR!', _tex_pvr2),
    (lambda d: d[:4] == b'PVR\x03', _tex_pvr3),
    (lambda d: d[:4] == b'DDS ', lambda d: _tex_dds(d)),
]


class RawImage:
    def __init__(self, mime, data, has_alpha=False):
        self.mime, self.data, self.has_alpha = mime, data, has_alpha


def _image_size(d):
    if d[:8] == b'\x89PNG\r\n\x1a\n' and d[12:16] == b'IHDR':
        w, h = struct.unpack_from('>II', d, 16)
        return w, h, 'image/png', d[25] in (4, 6) or b'tRNS' in d[:4096]
    if d[:2] == b'\xff\xd8':
        o = 2
        while o + 9 < len(d):
            if d[o] != 0xFF:
                o += 1
                continue
            mk = d[o + 1]
            if mk in (0xD8, 0x01) or 0xD0 <= mk <= 0xD7:
                o += 2
                continue
            ln = struct.unpack_from('>H', d, o + 2)[0]
            if 0xC0 <= mk <= 0xCF and mk not in (0xC4, 0xC8, 0xCC):
                h, w = struct.unpack_from('>HH', d, o + 5)
                return w, h, 'image/jpeg', False
            o += 2 + ln
    return None


def _mask_info(mask):
    if not mask:
        return 0, 0
    shift = 0
    while not (mask >> shift) & 1:
        shift += 1
    return shift, mask >> shift


def _dds_color_block(blk, four_color):
    c0, c1, idx = struct.unpack('<HHI', blk)
    r0, g0, b0 = _c565(c0)
    r1, g1, b1 = _c565(c1)
    if c0 > c1 or four_color:
        pal = [(r0, g0, b0, 255), (r1, g1, b1, 255),
               ((2 * r0 + r1) // 3, (2 * g0 + g1) // 3, (2 * b0 + b1) // 3, 255),
               ((r0 + 2 * r1) // 3, (g0 + 2 * g1) // 3, (b0 + 2 * b1) // 3, 255)]
    else:
        pal = [(r0, g0, b0, 255), (r1, g1, b1, 255),
               ((r0 + r1) // 2, (g0 + g1) // 2, (b0 + b1) // 2, 255), (0, 0, 0, 0)]
    return [pal[(idx >> (2 * i)) & 3] for i in range(16)]


def decode_dxt(data, w, h, kind):
    px = bytearray(w * h * 4)
    bsz = 8 if kind in ('dxt1', 'bc4') else 16
    bw, bh = (w + 3) // 4, (h + 3) // 4
    o = 0
    for by in range(bh):
        for bx in range(bw):
            blk = data[o:o + bsz]
            o += bsz
            if len(blk) < bsz:
                return px
            if kind == 'dxt1':
                cells = _dds_color_block(blk, False)
            elif kind == 'dxt3':
                ab = struct.unpack_from('<Q', blk, 0)[0]
                cells = [c[:3] + (((ab >> (4 * i)) & 15) * 17,) for i, c in enumerate(_dds_color_block(blk[8:], True))]
            elif kind == 'dxt5':
                al = _dxt5_alpha(blk[:8])
                cells = [c[:3] + (al[i],) for i, c in enumerate(_dds_color_block(blk[8:], True))]
            elif kind == 'bc4':
                cells = [(v, v, v, 255) for v in _dxt5_alpha(blk)]
            else:
                rr, gg = _dxt5_alpha(blk[:8]), _dxt5_alpha(blk[8:])
                cells = []
                for i in range(16):
                    x, y = rr[i] / 127.5 - 1.0, gg[i] / 127.5 - 1.0
                    z = math.sqrt(max(0.0, 1.0 - x * x - y * y))
                    cells.append((rr[i], gg[i], int((z * 0.5 + 0.5) * 255), 255))
            for i in range(16):
                x, y = bx * 4 + (i & 3), by * 4 + (i >> 2)
                if x < w and y < h:
                    p = (y * w + x) * 4
                    px[p:p + 4] = bytes(cells[i])
    return px


def decode_dds_uncompressed(data, w, h, bpp, masks, pitch, luminance, alpha_only):
    px = bytearray(w * h * 4)
    nb = bpp // 8
    chans = [_mask_info(m_) for m_ in masks]
    for y in range(h):
        row = y * pitch
        for x in range(w):
            v = int.from_bytes(data[row + x * nb:row + x * nb + nb], 'little')
            out = []
            for k, (sh, mx) in enumerate(chans):
                out.append(((v >> sh) & mx) * 255 // mx if mx else (255 if k == 3 else 0))
            p = (y * w + x) * 4
            if luminance:
                px[p:p + 4] = bytes((out[0], out[0], out[0], out[3] if masks[3] else 255))
            elif alpha_only:
                px[p:p + 4] = bytes((255, 255, 255, out[3]))
            else:
                px[p:p + 4] = bytes((out[0], out[1], out[2], out[3] if masks[3] else 255))
    return px


_DXGI_DXT = {70: 'dxt1', 71: 'dxt1', 72: 'dxt1', 73: 'dxt3', 74: 'dxt3', 75: 'dxt3', 76: 'dxt5', 77: 'dxt5', 78: 'dxt5',
             79: 'bc4', 80: 'bc4', 81: 'bc4', 82: 'bc5', 83: 'bc5', 84: 'bc5'}
_DXGI_RAW = {27: (32, (0xFF, 0xFF00, 0xFF0000, 0xFF000000)), 28: (32, (0xFF, 0xFF00, 0xFF0000, 0xFF000000)), 29: (32, (0xFF, 0xFF00, 0xFF0000, 0xFF000000)),
             87: (32, (0xFF0000, 0xFF00, 0xFF, 0xFF000000)), 90: (32, (0xFF0000, 0xFF00, 0xFF, 0xFF000000)),
             91: (32, (0xFF0000, 0xFF00, 0xFF, 0xFF000000)), 88: (32, (0xFF0000, 0xFF00, 0xFF, 0)),
             92: (32, (0xFF0000, 0xFF00, 0xFF, 0)), 61: (8, (0xFF, 0, 0, 0))}


def _tex_dds(d):
    if len(d) < 128 or struct.unpack_from('<I', d, 4)[0] != 124:
        raise TextureError('DDS: неверный заголовок')
    flags, h, w, pitch_or_size = struct.unpack_from('<IIII', d, 8)
    pf_flags, fourcc, bpp, rm, gm, bm, am = struct.unpack_from('<I4sIIIII', d, 80)
    data_at = 128
    fourcc = fourcc.rstrip(b'\x00')
    if fourcc == b'DX10':
        fmt = struct.unpack_from('<I', d, 128)[0]
        data_at = 148
        if fmt in _DXGI_DXT:
            return w, h, decode_dxt(d[data_at:], w, h, _DXGI_DXT[fmt]), 'DDS / DX10 ' + _DXGI_DXT[fmt].upper(), True
        if fmt in _DXGI_RAW:
            bpp_, masks = _DXGI_RAW[fmt]
            pitch = (w * bpp_ + 7) // 8
            return w, h, decode_dds_uncompressed(d[data_at:], w, h, bpp_, masks, pitch, False, False), \
                'DDS / DX10 формат %d' % fmt, True
        raise TextureError('DDS: DX10-формат %d не поддерживается (BC6H/BC7 и другие)' % fmt)
    if pf_flags & 0x4:
        table = {b'DXT1': 'dxt1', b'DXT2': 'dxt3', b'DXT3': 'dxt3', b'DXT4': 'dxt5', b'DXT5': 'dxt5',
                 b'ATI1': 'bc4', b'BC4U': 'bc4', b'ATI2': 'bc5', b'BC5U': 'bc5'}
        if fourcc not in table:
            raise TextureError('DDS: неподдерживаемый fourCC %r (%dx%d)' % (fourcc, w, h))
        return w, h, decode_dxt(d[data_at:], w, h, table[fourcc]), 'DDS / ' + fourcc.decode('ascii'), True
    if bpp not in (8, 16, 24, 32):
        raise TextureError('DDS: неподдерживаемая глубина %d бит' % bpp)
    luminance, alpha_only = bool(pf_flags & 0x20000), bool(pf_flags & 0x2) and not pf_flags & 0x40
    pitch = pitch_or_size if flags & 0x8 and pitch_or_size else (w * bpp + 7) // 8
    if not (pf_flags & 0x40 or luminance or alpha_only):
        raise TextureError('DDS: неизвестный формат пикселей (флаги 0x%X)' % pf_flags)
    if len(d) - data_at < pitch * h:
        raise TextureError('DDS: данных меньше, чем нужно для %dx%d' % (w, h))
    px = decode_dds_uncompressed(d[data_at:], w, h, bpp, (rm, gm, bm, am), pitch, luminance, alpha_only)
    return w, h, px, 'DDS / %d бит' % bpp, True


def decode_png(d):
    if d[:8] != b'\x89PNG\r\n\x1a\n':
        raise TextureError('это не PNG')
    o, idat, plte, trns, hdr = 8, [], None, None, None
    while o + 8 <= len(d):
        ln, tp = struct.unpack_from('>I4s', d, o)
        body = d[o + 8:o + 8 + ln]
        if tp == b'IHDR':
            hdr = struct.unpack('>IIBBBBB', body)
        elif tp == b'PLTE':
            plte = body
        elif tp == b'tRNS':
            trns = body
        elif tp == b'IDAT':
            idat.append(body)
        elif tp == b'IEND':
            break
        o += 12 + ln
    if hdr is None:
        raise TextureError('PNG без IHDR')
    w, h, bd, ct, _, _, interlace = hdr
    if interlace:
        raise TextureError('PNG с чересстрочной развёрткой не поддерживается')
    ch = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(ct)
    if ch is None or bd not in (1, 2, 4, 8, 16):
        raise TextureError('PNG: неподдерживаемый тип %d / %d бит' % (ct, bd))
    raw = zlib.decompress(b''.join(idat))
    bits = ch * bd
    bpp = max(1, bits // 8)
    rowlen = (w * bits + 7) // 8
    out = bytearray(h * rowlen)
    prev = bytearray(rowlen)
    p = 0
    for y in range(h):
        ft = raw[p]
        row = bytearray(raw[p + 1:p + 1 + rowlen])
        p += 1 + rowlen
        if ft == 1:
            for i in range(bpp, rowlen):
                row[i] = (row[i] + row[i - bpp]) & 255
        elif ft == 2:
            for i in range(rowlen):
                row[i] = (row[i] + prev[i]) & 255
        elif ft == 3:
            for i in range(rowlen):
                left = row[i - bpp] if i >= bpp else 0
                row[i] = (row[i] + ((left + prev[i]) >> 1)) & 255
        elif ft == 4:
            for i in range(rowlen):
                a_ = row[i - bpp] if i >= bpp else 0
                b_ = prev[i]
                c_ = prev[i - bpp] if i >= bpp else 0
                pp = a_ + b_ - c_
                pa, pb, pc = abs(pp - a_), abs(pp - b_), abs(pp - c_)
                pr = a_ if pa <= pb and pa <= pc else (b_ if pb <= pc else c_)
                row[i] = (row[i] + pr) & 255
        elif ft != 0:
            raise TextureError('PNG: неизвестный фильтр %d' % ft)
        out[y * rowlen:(y + 1) * rowlen] = row
        prev = row
    px = bytearray(w * h * 4)
    maxv = (1 << bd) - 1

    def sample(row_off, idx):
        if bd == 8:
            return out[row_off + idx]
        if bd == 16:
            return (out[row_off + idx * 2] << 8 | out[row_off + idx * 2 + 1])
        per = 8 // bd
        byte = out[row_off + idx // per]
        return (byte >> (8 - bd * (idx % per + 1))) & maxv

    def to8(v):
        return v if bd == 8 else (v >> 8 if bd == 16 else v * 255 // maxv)
    tr_key = None
    if trns is not None and ct in (0, 2):
        tr_key = tuple(struct.unpack('>%dH' % ch, trns[:2 * ch]))
    for y in range(h):
        ro = y * rowlen
        for x in range(w):
            q = (y * w + x) * 4
            if ct == 3:
                i_ = sample(ro, x)
                px[q:q + 3] = plte[i_ * 3:i_ * 3 + 3]
                px[q + 3] = trns[i_] if trns is not None and i_ < len(trns) else 255
            elif ct == 0:
                v = sample(ro, x)
                px[q] = px[q + 1] = px[q + 2] = to8(v)
                px[q + 3] = 0 if tr_key is not None and v == tr_key[0] else 255
            elif ct == 4:
                px[q] = px[q + 1] = px[q + 2] = to8(sample(ro, x * 2))
                px[q + 3] = to8(sample(ro, x * 2 + 1))
            elif ct == 2:
                rgb = (sample(ro, x * 3), sample(ro, x * 3 + 1), sample(ro, x * 3 + 2))
                px[q], px[q + 1], px[q + 2] = to8(rgb[0]), to8(rgb[1]), to8(rgb[2])
                px[q + 3] = 0 if tr_key is not None and rgb == tr_key else 255
            else:
                px[q], px[q + 1], px[q + 2] = to8(sample(ro, x * 4)), to8(sample(ro, x * 4 + 1)), to8(sample(ro, x * 4 + 2))
                px[q + 3] = to8(sample(ro, x * 4 + 3))
    return w, h, px


_ZIGZAG = [0, 1, 8, 16, 9, 2, 3, 10, 17, 24, 32, 25, 18, 11, 4, 5, 12, 19, 26, 33, 40, 48, 41, 34, 27, 20, 13, 6, 7,
           14, 21, 28, 35, 42, 49, 56, 57, 50, 43, 36, 29, 22, 15, 23, 30, 37, 44, 51, 58, 59, 52, 45, 38, 31, 39,
           46, 53, 60, 61, 54, 47, 55, 62, 63]
_IDCT_B = [[(math.sqrt(0.5) if u == 0 else 1.0) * 0.5 * math.cos((2 * x + 1) * u * math.pi / 16) for u in range(8)]
           for x in range(8)]


def _idct_block(coef):
    if not any(coef[1:]):
        v = coef[0] * _IDCT_B[0][0] * _IDCT_B[0][0] + 128
        v = 0 if v < 0 else 255 if v > 255 else int(v + 0.5)
        return [v] * 64
    tmp = [0.0] * 64
    for v in range(8):
        row = coef[v * 8:v * 8 + 8]
        if not any(row):
            continue
        for x in range(8):
            bx = _IDCT_B[x]
            tmp[v * 8 + x] = sum(bx[u] * row[u] for u in range(8) if row[u])
    out = [0] * 64
    for x in range(8):
        col = [tmp[v * 8 + x] for v in range(8)]
        for y in range(8):
            by = _IDCT_B[y]
            val = sum(by[v] * col[v] for v in range(8)) + 128
            out[y * 8 + x] = 0 if val < 0 else 255 if val > 255 else int(val + 0.5)
    return out


def decode_jpeg(d):
    if d[:2] != b'\xff\xd8':
        raise TextureError('это не JPEG')
    o = 2
    qt, hdc, hac = {}, {}, {}
    comps, frame, restart, adobe = None, None, 0, None
    prog = False
    coefs = {}
    scans = 0

    def build_huff(counts, syms):
        table, code, k = {}, 0, 0
        for ln in range(1, 17):
            for _ in range(counts[ln - 1]):
                table[(ln, code)] = syms[k]
                k += 1
                code += 1
            code <<= 1
        return table

    while o + 4 <= len(d):
        if d[o] != 0xFF:
            o += 1
            continue
        mk = d[o + 1]
        if mk == 0xFF:
            o += 1
            continue
        if mk in (0xD8, 0x01) or 0xD0 <= mk <= 0xD7 or mk == 0x00:
            o += 2
            continue
        if mk == 0xD9:
            break
        ln = struct.unpack_from('>H', d, o + 2)[0]
        seg = d[o + 4:o + 2 + ln]
        if mk == 0xDB:
            p = 0
            while p < len(seg):
                pq, tq = seg[p] >> 4, seg[p] & 15
                p += 1
                if pq:
                    qt[tq] = list(struct.unpack_from('>64H', seg, p))
                    p += 128
                else:
                    qt[tq] = list(seg[p:p + 64])
                    p += 64
        elif mk in (0xC0, 0xC1, 0xC2):
            prog = mk == 0xC2
            prec, h, w, nc = struct.unpack_from('>BHHB', seg, 0)
            if prec != 8:
                raise TextureError('JPEG: поддерживается только 8 бит на канал')
            comps = []
            for i in range(nc):
                cid, hv, tq = seg[6 + i * 3:9 + i * 3]
                comps.append({'id': cid, 'h': hv >> 4, 'v': hv & 15, 'tq': tq})
            frame = (w, h)
        elif mk in (0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            raise TextureError('JPEG: lossless/арифметический режим не поддерживается')
        elif mk == 0xC4:
            p = 0
            while p < len(seg):
                tc, th = seg[p] >> 4, seg[p] & 15
                counts = list(seg[p + 1:p + 17])
                total = sum(counts)
                syms = list(seg[p + 17:p + 17 + total])
                (hac if tc else hdc)[th] = build_huff(counts, syms)
                p += 17 + total
        elif mk == 0xDD:
            restart = struct.unpack_from('>H', seg, 0)[0]
        elif mk == 0xEE and seg[:5] == b'Adobe':
            adobe = seg[11] if len(seg) > 11 else None
        elif mk == 0xDA:
            if comps is None:
                raise TextureError('JPEG: SOS до SOF')
            ns = seg[0]
            sc = []
            for i in range(ns):
                cid, tt = seg[1 + i * 2], seg[2 + i * 2]
                comp = next(c_ for c_ in comps if c_['id'] == cid)
                sc.append((comp, tt >> 4, tt & 15))
            ss, se, ahal = seg[1 + ns * 2], seg[2 + ns * 2], seg[3 + ns * 2]
            p = o + 2 + ln
            segments, cur = [], bytearray()
            while p < len(d):
                b_ = d[p]
                if b_ == 0xFF:
                    nx = d[p + 1] if p + 1 < len(d) else 0xD9
                    if nx == 0x00:
                        cur.append(0xFF)
                        p += 2
                        continue
                    if 0xD0 <= nx <= 0xD7:
                        segments.append(cur)
                        cur = bytearray()
                        p += 2
                        continue
                    if nx == 0xFF:
                        p += 1
                        continue
                    break
                cur.append(b_)
                p += 1
            segments.append(cur)
            _jpeg_scan(frame, comps, sc, segments, restart, hdc, hac, coefs, prog, ss, se, ahal >> 4, ahal & 15)
            scans += 1
            o = p
            continue
        o += 2 + ln
    if frame is None or not scans:
        raise TextureError('JPEG: нет данных изображения')
    w, h = frame
    nc = len(comps)
    if nc not in (1, 3):
        raise TextureError('JPEG: %d компонент не поддерживается' % nc)
    hmax = max(c_['h'] for c_ in comps)
    vmax = max(c_['v'] for c_ in comps)
    full = []
    for c_ in comps:
        blocks, bwid = coefs[c_['id']]
        q = qt[c_['tq']]
        stride = bwid * 8
        rows = len(blocks) // bwid
        pl = bytearray(stride * rows * 8)
        for bi, blk in enumerate(blocks):
            deq = [0] * 64
            for k in range(64):
                if blk[k]:
                    deq[_ZIGZAG[k]] = blk[k] * q[k]
            pix = _idct_block(deq)
            bx, by = bi % bwid, bi // bwid
            for yy in range(8):
                off = (by * 8 + yy) * stride + bx * 8
                pl[off:off + 8] = bytes(pix[yy * 8:yy * 8 + 8])
        cw, ch = -(-w * c_['h'] // hmax), -(-h * c_['v'] // vmax)
        full.append(_upsample(pl, stride, cw, ch, hmax // c_['h'] if hmax % c_['h'] == 0 else 0,
                              vmax // c_['v'] if vmax % c_['v'] == 0 else 0, w, h, c_['h'], c_['v'], hmax, vmax))
    px = bytearray(w * h * 4)
    px[3::4] = b'\xff' * (w * h)
    rgb_direct = nc == 3 and (adobe == 0 or [c_['id'] for c_ in comps] == [82, 71, 66])
    if nc == 1:
        px[0::4] = px[1::4] = px[2::4] = full[0]
        return w, h, px
    if rgb_direct:
        px[0::4], px[1::4], px[2::4] = full[0], full[1], full[2]
        return w, h, px
    yp, cbp, crp = full
    rt = [1.402 * (i - 128) for i in range(256)]
    gb = [0.344136 * (i - 128) for i in range(256)]
    gr = [0.714136 * (i - 128) for i in range(256)]
    bt = [1.772 * (i - 128) for i in range(256)]
    for i in range(w * h):
        y, cb, cr = yp[i], cbp[i], crp[i]
        r = y + rt[cr]
        g = y - gb[cb] - gr[cr]
        b = y + bt[cb]
        q = i * 4
        px[q] = 0 if r < 0 else 255 if r > 255 else int(r + 0.5)
        px[q + 1] = 0 if g < 0 else 255 if g > 255 else int(g + 0.5)
        px[q + 2] = 0 if b < 0 else 255 if b > 255 else int(b + 0.5)
    return w, h, px


def _upsample(pl, stride, cw, ch, fx, fy, w, h, ci, cj, hmax, vmax):
    out = bytearray(w * h)
    if fx == 1 and fy == 1:
        for y in range(h):
            out[y * w:(y + 1) * w] = pl[y * stride:y * stride + w]
        return out
    if fx in (1, 2) and fy in (1, 2):
        rows = [pl[y * stride:y * stride + cw] for y in range(ch)]
        for y in range(h):
            row = bytearray(w)
            if fy == 2:
                yi = min(y >> 1, ch - 1)
                far = min(max(yi + (1 if y & 1 else -1), 0), ch - 1)
                near_row, far_row = rows[yi], rows[far]
                col = [3 * near_row[x] + far_row[x] for x in range(cw)]
            else:
                col = rows[min(y, ch - 1)]
            for x in range(w):
                if fx == 2:
                    xi = min(x >> 1, cw - 1)
                    nb = min(max(xi + (1 if x & 1 else -1), 0), cw - 1)
                else:
                    xi = nb = min(x, cw - 1)
                if fx == 2 and fy == 2:
                    v = (3 * col[xi] + col[nb] + (7 if x & 1 else 8)) >> 4
                elif fx == 2:
                    v = (3 * col[xi] + col[nb] + (2 if x & 1 else 1)) >> 2
                else:
                    v = (col[xi] + (2 if y & 1 else 1)) >> 2
                row[x] = 255 if v > 255 else v
            out[y * w:(y + 1) * w] = row
        return out
    for y in range(h):
        base = min(y * cj // vmax, ch - 1) * stride
        for x in range(w):
            out[y * w + x] = pl[base + min(x * ci // hmax, cw - 1)]
    return out


def _jpeg_scan(frame, comps, sc, segments, restart, hdc, hac, coefs, prog, ss, se, ah, al):
    w, h = frame
    hmax = max(c_['h'] for c_ in comps)
    vmax = max(c_['v'] for c_ in comps)
    mcux = -(-w // (8 * hmax))
    mcuy = -(-h // (8 * vmax))
    for c_ in comps:
        if c_['id'] not in coefs:
            bwid = mcux * c_['h']
            coefs[c_['id']] = ([[0] * 64 for _ in range(bwid * mcuy * c_['v'])], bwid)
    if len(sc) == 1:
        c0 = sc[0][0]
        cw, chh = -(-w * c0['h'] // hmax), -(-h * c0['v'] // vmax)
        bw, bh = -(-cw // 8), -(-chh // 8)
        total = bw * bh
    else:
        total = mcux * mcuy
    per_seg = restart if restart else total
    mcu = 0
    for seg in segments:
        if mcu >= total:
            break
        data = seg
        nbits = len(data) * 8
        pos = [0]
        pred = {c_[0]['id']: 0 for c_ in sc}
        eobrun = [0]

        def bit():
            p = pos[0]
            pos[0] = p + 1
            if p >= nbits:
                return 0
            return (data[p >> 3] >> (7 - (p & 7))) & 1

        def huff(table):
            code = 0
            for ln in range(1, 17):
                code = (code << 1) | bit()
                s_ = table.get((ln, code))
                if s_ is not None:
                    return s_
            raise TextureError('JPEG: повреждённые данные')

        def bits(n):
            v = 0
            for _ in range(n):
                v = (v << 1) | bit()
            return v

        def receive(n):
            v = bits(n)
            return v if v >= (1 << (n - 1)) else v - (1 << n) + 1

        for _ in range(per_seg):
            if mcu >= total:
                break
            if len(sc) == 1:
                units = [(sc[0], mcu % bw, mcu // bw)]
            else:
                mr, mc = divmod(mcu, mcux)
                units = [(s_, mc * s_[0]['h'] + hh, mr * s_[0]['v'] + vv)
                         for s_ in sc for vv in range(s_[0]['v']) for hh in range(s_[0]['h'])]
            for (comp, td, ta), bx, by in units:
                blocks, bwid = coefs[comp['id']]
                blk = blocks[by * bwid + bx]
                if not prog:
                    t = huff(hdc[td])
                    pred[comp['id']] += receive(t) if t else 0
                    blk[0] = pred[comp['id']]
                    k = 1
                    while k < 64:
                        rs = huff(hac[ta])
                        r, s_ = rs >> 4, rs & 15
                        if s_ == 0:
                            if r == 15:
                                k += 16
                                continue
                            break
                        k += r
                        if k > 63:
                            break
                        blk[k] = receive(s_)
                        k += 1
                elif ss == 0:
                    if ah == 0:
                        t = huff(hdc[td])
                        pred[comp['id']] += receive(t) if t else 0
                        blk[0] = pred[comp['id']] << al
                    elif bit():
                        blk[0] |= 1 << al
                elif ah == 0:
                    if eobrun[0] > 0:
                        eobrun[0] -= 1
                        continue
                    k = ss
                    while k <= se:
                        rs = huff(hac[ta])
                        r, s_ = rs >> 4, rs & 15
                        if s_ == 0:
                            if r < 15:
                                eobrun[0] = (1 << r) + (bits(r) if r else 0) - 1
                                break
                            k += 16
                            continue
                        k += r
                        if k > 63:
                            break
                        blk[k] = receive(s_) * (1 << al)
                        k += 1
                else:
                    p1, m1 = 1 << al, -1 << al
                    k = ss
                    if eobrun[0] <= 0:
                        while k <= se:
                            rs = huff(hac[ta])
                            r, s_ = rs >> 4, rs & 15
                            val = 0
                            if s_:
                                val = p1 if bit() else m1
                            elif r != 15:
                                eobrun[0] = (1 << r) + (bits(r) if r else 0)
                                break
                            while k <= se:
                                cf = blk[k]
                                if cf:
                                    if bit() and not cf & p1:
                                        blk[k] = cf + (p1 if cf >= 0 else m1)
                                else:
                                    r -= 1
                                    if r < 0:
                                        break
                                k += 1
                            if val and k <= se:
                                blk[k] = val
                            k += 1
                    if eobrun[0] > 0:
                        while k <= se:
                            cf = blk[k]
                            if cf and bit() and not cf & p1:
                                blk[k] = cf + (p1 if cf >= 0 else m1)
                            k += 1
                        eobrun[0] -= 1
            mcu += 1


def flip_rows(px, w, h):
    row = w * 4
    out = bytearray(len(px))
    for y in range(h):
        out[y * row:(y + 1) * row] = px[(h - 1 - y) * row:(h - y) * row]
    return out


def resample(px, w, h, nw, nh):
    if (w, h) == (nw, nh):
        return px
    out = bytearray(nw * nh * 4)
    for y in range(nh):
        fy = (y + 0.5) * h / nh - 0.5
        y0 = max(0, min(h - 1, int(math.floor(fy))))
        y1 = min(h - 1, y0 + 1)
        wy = min(1.0, max(0.0, fy - y0))
        for x in range(nw):
            fx = (x + 0.5) * w / nw - 0.5
            x0 = max(0, min(w - 1, int(math.floor(fx))))
            x1 = min(w - 1, x0 + 1)
            wx = min(1.0, max(0.0, fx - x0))
            q = (y * nw + x) * 4
            for c in range(4):
                a_ = px[(y0 * w + x0) * 4 + c] * (1 - wx) + px[(y0 * w + x1) * 4 + c] * wx
                b_ = px[(y1 * w + x0) * 4 + c] * (1 - wx) + px[(y1 * w + x1) * 4 + c] * wx
                out[q + c] = int(a_ * (1 - wy) + b_ * wy + 0.5)
    return out


def tex_to_topdown_rgba(w, h, px, topdown):
    if isinstance(px, RawImage):
        data = px.data
        w, h, px = decode_png(data) if data[:8] == b'\x89PNG\r\n\x1a\n' else decode_jpeg(data)
        topdown = True
    return w, h, (px if topdown else flip_rows(px, w, h))


def blend_textures(base, over, mode='normal', opacity=1.0):
    bw, bh, bpx = base
    ow, oh, opx = over
    opx = resample(opx, ow, oh, bw, bh)
    out = bytearray(len(bpx))
    for i in range(0, len(bpx), 4):
        br, bg, bb, ba = bpx[i], bpx[i + 1], bpx[i + 2], bpx[i + 3]
        orr, og, ob, oa = opx[i], opx[i + 1], opx[i + 2], opx[i + 3]
        a = oa / 255.0 * opacity
        if mode == 'normal':
            fa = ba / 255.0
            oa_ = a + fa * (1 - a)
            if oa_ <= 0:
                continue
            k = fa * (1 - a)
            out[i] = int((orr * a + br * k) / oa_ + 0.5)
            out[i + 1] = int((og * a + bg * k) / oa_ + 0.5)
            out[i + 2] = int((ob * a + bb * k) / oa_ + 0.5)
            out[i + 3] = int(oa_ * 255 + 0.5)
            continue
        if mode == 'multiply':
            tr, tg, tb = br * orr / 255.0, bg * og / 255.0, bb * ob / 255.0
        elif mode == 'add':
            tr, tg, tb = min(255, br + orr), min(255, bg + og), min(255, bb + ob)
        else:
            tr = 255 - (255 - br) * (255 - orr) / 255.0
            tg = 255 - (255 - bg) * (255 - og) / 255.0
            tb = 255 - (255 - bb) * (255 - ob) / 255.0
        out[i] = int(br + (tr - br) * a + 0.5)
        out[i + 1] = int(bg + (tg - bg) * a + 0.5)
        out[i + 2] = int(bb + (tb - bb) * a + 0.5)
        out[i + 3] = ba
    return bw, bh, out


def load_texture(path):
    d = open(path, 'rb').read()
    info = _image_size(d)
    if info:
        w, h, mime, alpha = info
        return w, h, RawImage(mime, d, alpha), mime.split('/')[1].upper(), True
    for sniff, fn in TEXTURE_FORMATS:
        if sniff(d):
            res = fn(d)
            return (res + (False,)) if len(res) == 4 else res
    raise TextureError('неизвестный формат текстуры, первые байты: %s' % d[:32].hex(' '))


def bleed_colors(px, w, h, passes=8):
    unknown = [i for i in range(w * h) if px[i * 4 + 3] < 128]
    if not unknown or len(unknown) == w * h:
        return
    known = bytearray(w * h)
    for i in range(w * h):
        if px[i * 4 + 3] >= 128:
            known[i] = 1
    for _ in range(passes):
        upd, rest = [], []
        for i in unknown:
            x, y = i % w, i // w
            r = g = b = c = 0
            for dy in (-1, 0, 1):
                yy = y + dy
                if 0 <= yy < h:
                    for dx in (-1, 0, 1):
                        xx = x + dx
                        if 0 <= xx < w and known[yy * w + xx]:
                            p = (yy * w + xx) * 4
                            r += px[p]
                            g += px[p + 1]
                            b += px[p + 2]
                            c += 1
            if c:
                upd.append((i, r // c, g // c, b // c))
            else:
                rest.append(i)
        if not upd:
            break
        for i, r, g, b in upd:
            px[i * 4], px[i * 4 + 1], px[i * 4 + 2] = r, g, b
            known[i] = 1
        unknown = rest


def find_fur_triangles(m):
    weld, wid = {}, []
    for p in m.pos:
        wid.append(weld.setdefault((round(p[0], 4), round(p[1], 4), round(p[2], 4)), len(weld)))
    edges = {}
    for a, b, c in m.tris:
        a, b, c = wid[a], wid[b], wid[c]
        for u, v in ((a, b), (b, c), (c, a)):
            k = (u, v) if u < v else (v, u)
            edges[k] = edges.get(k, 0) + 1
    fur = set()
    for ti, (a, b, c) in enumerate(m.tris):
        a, b, c = wid[a], wid[b], wid[c]
        for u, v in ((a, b), (b, c), (c, a)):
            k = (u, v) if u < v else (v, u)
            if edges[k] == 1:
                fur.add(ti)
                break
    return fur, wid


def fur_texture(size=128):
    px = bytearray(size * size * 4)
    for y in range(size):
        v = (y + 0.5) / size
        for x in range(size):
            u = (x + 0.5) / size
            a = 0.0
            for c in (0.17, 0.5, 0.83):
                hw = 0.16 * (1.0 - v) + 0.012
                a = max(a, min(1.0, (hw - abs(u - c)) / 0.02))
            g = 0.45 + 0.35 * v
            o = (y * size + x) * 4
            px[o], px[o + 1], px[o + 2] = int(255 * min(1, g + 0.03)), int(255 * g), int(255 * (g - 0.03))
            px[o + 3] = 255 if a > 0.5 else 0
    return make_png(size, size, px)


TICK_RATE = 60.0
_Q_SCALE = 32767 * math.sqrt(2)


_POS_FORMATS = {2: (14, 2), 1: (13, 1)}
_ROT_FORMATS = {10: (8, 2), 9: (7, 1)}


def parse_ani(path):
    d = open(path, 'rb').read()
    u32 = lambda o: struct.unpack_from('<I', d, o)[0]
    n = u32(8)
    o = 12
    tracks = {}
    for _ in range(n):
        rid, ln = u32(o), u32(o + 4)
        name = d[o + 8:o + 8 + ln].decode('ascii', 'replace')
        sz = u32(o + 8 + ln)
        p = o + 12 + ln
        nch = u32(p)
        q = p + 4
        ch = {}
        for _c in range(nch):
            cid, csz = u32(q), u32(q + 4)
            ch[cid] = d[q + 8:q + 8 + csz]
            q += 8 + csz
        if q != p + sz:
            raise ValueError('запись %s: размеры каналов не сошлись' % name)
        o = p + sz
        pos, rot = [], []
        b = ch.get(1)
        if b:
            nbytes, fmt = struct.unpack_from('<I', b, 1)[0], b[5]
            if b[0] != 1 or fmt not in _POS_FORMATS:
                raise ValueError('%s: неизвестный формат канала позиции (тип %d, формат %d)' % (name, b[0], fmt))
            ks, ts = _POS_FORMATS[fmt]
            if nbytes % ks or 6 + nbytes > len(b):
                raise ValueError('%s: размер канала позиции не делится на размер ключа %d' % (name, ks))
            for i in range(nbytes // ks):
                at = 6 + i * ks
                pos.append((int.from_bytes(b[at:at + ts], 'little'), struct.unpack_from('<3f', b, at + ts)))
        b = ch.get(2)
        if b:
            nbytes, fmt = struct.unpack_from('<I', b, 1)[0], b[5]
            if b[0] != 2 or fmt not in _ROT_FORMATS:
                raise ValueError('%s: неизвестный формат канала поворота (тип %d, формат %d)' % (name, b[0], fmt))
            ks, ts = _ROT_FORMATS[fmt]
            if nbytes % ks or 6 + nbytes > len(b):
                raise ValueError('%s: размер канала поворота не делится на размер ключа %d' % (name, ks))
            for i in range(nbytes // ks):
                at = 6 + i * ks
                t = int.from_bytes(b[at:at + ts], 'little')
                a_, bb, c = struct.unpack_from('<3h', b, at + ts)
                idx = ((bb & 1) << 1) | (c & 1)
                v = [a_ / _Q_SCALE, bb / _Q_SCALE, c / _Q_SCALE]
                m = math.sqrt(max(0.0, 1.0 - sum(x * x for x in v)))
                qq, j = [0.0] * 4, 0
                for k in range(4):
                    if k == idx:
                        qq[k] = m
                    else:
                        qq[k] = v[j]
                        j += 1
                rot.append((t, tuple(qq)))
        tracks[rid] = (name, pos, rot)
    if o != len(d):
        print('  предупреждение: в .ani осталось %d непрочитанных байт' % (len(d) - o))
    return tracks


def _sample(keys, t, rot):
    import bisect
    ts = [k[0] for k in keys]
    i = bisect.bisect_right(ts, t) - 1
    if i < 0:
        return keys[0][1]
    if i >= len(keys) - 1:
        return keys[-1][1]
    (ta, a), (tb, b) = keys[i], keys[i + 1]
    f = (t - ta) / max(1, tb - ta)
    if rot:
        if sum(x * y for x, y in zip(a, b)) < 0:
            b = tuple(-x for x in b)
        q = tuple(x + (y - x) * f for x, y in zip(a, b))
        return qnorm(q)
    return tuple(x + (y - x) * f for x, y in zip(a, b))


def anim_duration(tracks):
    return max([k[0] for _, p, r in tracks.values() for k in (p + r)] or [0])


def auto_clips(tracks, threshold=0.5):
    dense = [r for _, p, r in tracks.values() if len(r) > 200]
    times = sorted({k[0] for _, p, r in tracks.values() for k in (p + r)})
    cuts = []
    for a, b in zip(times, times[1:]):
        tot = 0.0
        for r in dense:
            qa, qb = _sample(r, a, True), _sample(r, b, True)
            tot += 2 * math.acos(min(1.0, abs(sum(x * y for x, y in zip(qa, qb)))))
        if dense and tot / len(dense) >= threshold:
            cuts.append((a, b))
    bounds, clips, start = [], [], times[0] if times else 0
    for a, b in cuts:
        if a > start:
            clips.append((start, a))
        start = b
    if times and times[-1] > start:
        clips.append((start, times[-1]))
    return clips


def parse_lua_clips(path):
    text = open(path, encoding='utf-8', errors='replace').read()
    out = {}
    cur, end_label = None, None
    for line in text.splitlines():
        mm = re.search(r'getModelName"\]\(R\d+\)\s*==\s*"([^"]+)"\)\s*then goto (L\d+)', line)
        if mm and 'not' in line:
            cur, end_label = mm.group(1), mm.group(2)
            out.setdefault(cur, {'fps': None, 'clips': []})
            continue
        ml = re.match(r'\s*::(L\d+)::', line)
        if ml and ml.group(1) == end_label:
            cur, end_label = None, None
            continue
        ent = out.setdefault(cur, {'fps': None, 'clips': []})
        mf = re.search(r'"onSetFrameFactor",\s*([\d.]+)', line)
        if mf:
            ent['fps'] = float(mf.group(1))
        ma = re.search(r'"onAdd",\s*"([^"]+)",\s*([\d.]+),\s*([\d.]+),\s*(true|false)', line)
        if ma:
            ent['clips'].append((ma.group(1), float(ma.group(2)), float(ma.group(3)), ma.group(4) == 'true'))
    return {k: v for k, v in out.items() if v['clips']}


def norm_weights(ws, bs):
    out_w, out_b = [], []
    for w, b in zip(ws, bs):
        s = sum(w)
        if s == 255:
            out_w.append(tuple(w))
        elif s == 0:
            out_w.append((255, 0, 0, 0))
        else:
            nw = [round(x * 255 / s) for x in w]
            nw[nw.index(max(nw))] += 255 - sum(nw)
            out_w.append(tuple(nw))
        out_b.append(tuple(b))
    return out_w, out_b


def build_glb(m, fur_mode='strands', tex=None, sk=None, flip_v=False, name='mesh', anim=None, clips=None,
              tick_rate=TICK_RATE):
    n = len(m.pos)
    pos, nor = list(m.pos), list(m.nor)
    topdown = tex is not None and tex[3]
    flip_v = flip_v != topdown
    uvs = [(u, 1.0 - v) if flip_v else (u, v) for u, v in m.uv]
    skinned = sk is not None and m.wts
    if skinned:
        wts, jns = norm_weights(m.wts, m.bones)
        if max(max(b) for b in jns) >= len(sk.palette):
            raise ValueError('индекс кости в вершинах больше размера палитры')
    body = list(range(len(m.tris)))
    fur_idx = []
    root = set()
    if tex is None and fur_mode in ('strands', 'remove'):
        fur, wid = find_fur_triangles(m)
        body = [i for i in range(len(m.tris)) if i not in fur]
        if fur_mode == 'strands':
            for i in body:
                for v in m.tris[i]:
                    root.add(wid[v])
            fur_tris = []
            for i in sorted(fur):
                (fur_tris if any(wid[v] not in root for v in m.tris[i]) else body).append(i)
            body.sort()
            for i in fur_tris:
                tri = m.tris[i]
                tips = [k for k in range(3) if wid[tri[k]] not in root]
                roots = [k for k in range(3) if wid[tri[k]] in root]
                if len(tips) == 1:
                    uvf = {tips[0]: (0.5, 1.0), roots[0]: (0.0, 0.0), roots[1]: (1.0, 0.0)}
                elif len(tips) == 2:
                    uvf = {roots[0]: (0.5, 0.0), tips[0]: (0.0, 1.0), tips[1]: (1.0, 1.0)}
                else:
                    uvf = {0: (0.0, 0.0), 1: (1.0, 0.0), 2: (0.5, 1.0)}
                for k in range(3):
                    vi = tri[k]
                    pos.append(m.pos[vi])
                    nor.append(m.nor[vi])
                    uvs.append(uvf[k])
                    if skinned:
                        wts.append(wts[vi])
                        jns.append(jns[vi])
                    fur_idx.append(len(pos) - 1)
    body_idx = [v for i in body for v in m.tris[i]]
    cnt = len(pos)

    views = []

    def add_view(data, target=None):
        views.append((data, target))
        return len(views) - 1

    def f32(rows, k):
        return struct.pack('<%df' % (len(rows) * k), *[c for r in rows for c in r])
    acc = []
    attrs = {}

    def add_acc(view, ctype, count, typ, **kw):
        a = {"bufferView": view, "componentType": ctype, "count": count, "type": typ}
        a.update(kw)
        acc.append(a)
        return len(acc) - 1
    mn = [min(p[k] for p in pos) for k in range(3)]
    mx = [max(p[k] for p in pos) for k in range(3)]
    attrs["POSITION"] = add_acc(add_view(f32(pos, 3), 34962), 5126, cnt, "VEC3", min=mn, max=mx)
    attrs["NORMAL"] = add_acc(add_view(f32(nor, 3), 34962), 5126, cnt, "VEC3")
    attrs["TEXCOORD_0"] = add_acc(add_view(f32(uvs, 2), 34962), 5126, cnt, "VEC2")
    if skinned:
        attrs["JOINTS_0"] = add_acc(add_view(b''.join(bytes(j) for j in jns), 34962), 5121, cnt, "VEC4")
        attrs["WEIGHTS_0"] = add_acc(add_view(b''.join(bytes(w) for w in wts), 34962), 5121, cnt, "VEC4",
                                     normalized=True)
    prims = [{"attributes": attrs, "material": 0,
              "indices": add_acc(add_view(struct.pack('<%dI' % len(body_idx), *body_idx), 34963),
                                 5125, len(body_idx), "SCALAR")}]
    j = {"asset": {"version": "2.0", "generator": "msh2glb.py"}, "scene": 0}
    mats, textures, images = [], [], []
    pbr = {"metallicFactor": 0, "roughnessFactor": 1}
    if tex is not None:
        w, h, px = tex[:3]
        if isinstance(px, RawImage):
            has_alpha, img_bytes, img_mime = px.has_alpha, px.data, px.mime
        else:
            has_alpha = any(a < 255 for a in px[3::4])
            img_bytes, img_mime = make_png(w, h, px, with_alpha=has_alpha), 'image/png'
        images.append({"bufferView": add_view(img_bytes), "mimeType": img_mime})
        textures.append({"source": 0, "sampler": 0})
        mat = {"name": name, "doubleSided": True, "pbrMetallicRoughness": dict(pbr, baseColorTexture={"index": 0})}
        if has_alpha:
            mat.update(alphaMode="MASK", alphaCutoff=0.5)
        mats.append(mat)
    else:
        mats.append({"name": "body", "doubleSided": True,
                     "pbrMetallicRoughness": dict(pbr, baseColorFactor=[0.62, 0.6, 0.57, 1])})
        if fur_idx:
            images.append({"bufferView": add_view(fur_texture()), "mimeType": "image/png"})
            textures.append({"source": 0, "sampler": 0})
            mats.append({"name": "fur", "doubleSided": True, "alphaMode": "MASK", "alphaCutoff": 0.5,
                         "pbrMetallicRoughness": dict(pbr, baseColorTexture={"index": 0})})
            prims.append({"attributes": attrs, "material": 1,
                          "indices": add_acc(add_view(struct.pack('<%dI' % len(fur_idx), *fur_idx), 34963),
                                             5125, len(fur_idx), "SCALAR")})
    nodes = [{"name": name, "mesh": 0}]
    scene_nodes = [0]
    if skinned:
        nid = {b: 1 + k for k, b in enumerate(sk.order)}
        for b in sk.order:
            t, q = sk.local[b]
            nodes.append({"name": short_name(sk.bones[b].name), "translation": list(t), "rotation": list(q)})
        roots = []
        for b in sk.order:
            p = sk.parents[b]
            if p is None:
                roots.append(nid[b])
            else:
                nodes[nid[p]].setdefault("children", []).append(nid[b])
        ibm = bytearray()
        for b in sk.palette:
            t, q = sk.world[b]
            R = qmat(q)
            Rt = [[R[c][r] for c in range(3)] for r in range(3)]
            tt = [-sum(Rt[r][c] * t[c] for c in range(3)) for r in range(3)]
            M = [Rt[0] + [tt[0]], Rt[1] + [tt[1]], Rt[2] + [tt[2]], [0, 0, 0, 1]]
            ibm += struct.pack('<16f', *[M[r][c] for c in range(4) for r in range(4)])
        ia = add_acc(add_view(bytes(ibm)), 5126, len(sk.palette), "MAT4")
        nodes[0]["skin"] = 0
        j["skins"] = [{"joints": [nid[b] for b in sk.palette], "skeleton": roots[0], "inverseBindMatrices": ia}]
        scene_nodes = [0] + roots
        if clips and (anim or any(len(c_) > 4 for c_ in clips)):
            conj_q = sk.mode == 'conj'
            prep_cache = {}

            def prep(tracks):
                if id(tracks) in prep_cache:
                    return prep_cache[id(tracks)]
                out = {}
                for b in sk.order:
                    tr = tracks.get(b)
                    if not tr:
                        continue
                    rk = []
                    for t, q in tr[2]:
                        if conj_q:
                            q = (-q[0], -q[1], -q[2], q[3])
                        q = qnorm(q)
                        if rk and sum(x * y for x, y in zip(rk[-1][1], q)) < 0:
                            q = tuple(-x for x in q)
                        rk.append((t, q))
                    out[b] = (tr[1], rk)
                prep_cache[id(tracks)] = out
                return out
            j["animations"] = []
            for cl in clips:
                cname, t0, t1 = cl[0], cl[1], cl[2]
                prepared = prep(cl[4] if len(cl) > 4 else anim)
                chans, samps = [], []
                for b in sk.order:
                    if b not in prepared:
                        continue
                    for path, keys, rot in (('translation', prepared[b][0], False), ('rotation', prepared[b][1], True)):
                        if not keys:
                            continue
                        sel = [(t, v) for t, v in keys if t0 <= t <= t1]
                        if not sel or sel[0][0] > t0:
                            sel.insert(0, (t0, _sample(keys, t0, rot)))
                        if sel[-1][0] < t1:
                            sel.append((t1, _sample(keys, t1, rot)))
                        ts = [(t - t0) / tick_rate for t, _ in sel]
                        ia = add_acc(add_view(struct.pack('<%df' % len(ts), *ts)), 5126, len(ts), "SCALAR",
                                     min=[ts[0]], max=[ts[-1]])
                        flat = [c for _, v in sel for c in v]
                        oa = add_acc(add_view(struct.pack('<%df' % len(flat), *flat)), 5126, len(sel),
                                     "VEC4" if rot else "VEC3")
                        samps.append({"input": ia, "output": oa, "interpolation": "LINEAR"})
                        chans.append({"sampler": len(samps) - 1, "target": {"node": nid[b], "path": path}})
                an = {"name": cname, "channels": chans, "samplers": samps}
                if len(cl) > 3 and cl[3] is not None:
                    an["extras"] = {"loop": bool(cl[3])}
                j["animations"].append(an)
    blob, bv = b'', []
    for data, target in views:
        e = {"buffer": 0, "byteOffset": len(blob), "byteLength": len(data)}
        if target:
            e["target"] = target
        bv.append(e)
        blob += pad4(data)
    j.update({"scenes": [{"nodes": scene_nodes}], "nodes": nodes, "meshes": [{"primitives": prims}],
              "materials": mats, "buffers": [{"byteLength": len(blob)}], "bufferViews": bv, "accessors": acc})
    if textures:
        j["textures"], j["images"] = textures, images
        j["samplers"] = [{"magFilter": 9729, "minFilter": 9987, "wrapS": 33071, "wrapT": 33071}]
    js = pad4(json.dumps(j).encode(), b' ')
    glb = (struct.pack('<III', 0x46546C67, 2, 12 + 8 + len(js) + 8 + len(blob))
           + struct.pack('<II', len(js), 0x4E4F534A) + js + struct.pack('<II', len(blob), 0x004E4942) + blob)
    return glb, len(body), len(fur_idx) // 3


def write_obj(m, path):
    with open(path, 'w') as f:
        for p in m.pos:
            f.write('v %.6f %.6f %.6f\n' % p)
        for u in m.uv:
            f.write('vt %.6f %.6f\n' % u)
        for q in m.nor:
            f.write('vn %.6f %.6f %.6f\n' % q)
        for a, b, c in m.tris:
            f.write('f %d/%d/%d %d/%d/%d %d/%d/%d\n' % (a + 1, a + 1, a + 1, b + 1, b + 1, b + 1, c + 1, c + 1, c + 1))


def _listdir_ci(folder):
    try:
        return {f.lower(): f for f in os.listdir(folder)}
    except OSError:
        return {}


def find_skeleton(msh_path, mesh):
    folder = os.path.dirname(os.path.abspath(msh_path))
    files = _listdir_ci(folder)
    if mesh.skeleton and (mesh.skeleton + '.ske').lower() in files:
        return os.path.join(folder, files[(mesh.skeleton + '.ske').lower()])
    ske = [f for l, f in files.items() if l.endswith('.ske')]
    return os.path.join(folder, ske[0]) if len(ske) == 1 else None


def find_texture(msh_path, mesh):
    folder = os.path.dirname(os.path.abspath(msh_path))
    files = _listdir_ci(folder)
    stems = []
    base = os.path.splitext(os.path.basename(msh_path))[0]
    for s in (base, re.sub(r'_geom\w*$', '', base),
              re.sub(r'_?skeleton\d*$', '', mesh.skeleton or '', flags=re.I) if mesh.skeleton else None):
        if s and s not in stems:
            stems.append(s)
    for s in stems:
        for ext in TEX_EXTS:
            if (s + ext).lower() in files:
                return os.path.join(folder, files[(s + ext).lower()]), None
    for s_ in stems:
        pre = [f for l, f in sorted(files.items()) if l.endswith(TEX_EXTS) and l.startswith(s_.lower() + '_')]
        if len(pre) == 1:
            return os.path.join(folder, pre[0]), None
    allt = [f for l, f in files.items() if l.endswith(TEX_EXTS)]
    if len(allt) == 1:
        return os.path.join(folder, allt[0]), None
    return None, allt


def model_prefixes(msh_path, mesh):
    base = os.path.splitext(os.path.basename(msh_path))[0]
    out = []
    for p in (re.sub(r'_geom\w*$', '', base), base,
              re.sub(r'_?skeleton\d*$', '', mesh.skeleton or '', flags=re.I) if mesh.skeleton else None):
        if p and p not in out:
            out.append(p)
    return sorted(out, key=len, reverse=True)


def find_anims(msh_path, mesh):
    folder = os.path.dirname(os.path.abspath(msh_path))
    return [os.path.join(folder, f) for l, f in sorted(_listdir_ci(folder).items()) if l.endswith('.ani')]


def anim_label(ani_path, prefixes):
    stem = os.path.splitext(os.path.basename(ani_path))[0]
    for p in prefixes:
        if stem.lower() == p.lower():
            return None
        if stem.lower().startswith(p.lower() + '_') and len(stem) > len(p) + 1:
            return stem[len(p) + 1:]
    return stem


def main():
    ap = argparse.ArgumentParser(description='ShiVa3D .msh/.ske/.ani/текстуры -> .glb/.obj')
    ap.add_argument('files', nargs='+', help='.msh файлы')
    ap.add_argument('--ske', help='скелет (.ske); по умолчанию ищется рядом по имени из .msh')
    ap.add_argument('--tex', help='основная текстура (ETC, ATC, PVR, DDS, JPEG, PNG); по умолчанию ищется рядом')
    ap.add_argument('--tex2', help='вторая текстура любого из этих форматов, накладывается на основную')
    ap.add_argument('--over', type=int, choices=[1, 2], default=2, help='какая текстура сверху: 1 (--tex) или 2 (--tex2), по умолчанию 2')
    ap.add_argument('--blend', choices=['normal', 'multiply', 'add', 'screen'], default='normal', help='режим наложения верхней текстуры (по умолчанию normal: по её альфе)')
    ap.add_argument('--opacity', type=float, default=1.0, help='непрозрачность верхней текстуры, 0..1')
    ap.add_argument('--no-skel', action='store_true', help='не добавлять скелет')
    ap.add_argument('--no-tex', action='store_true', help='не добавлять текстуры')
    ap.add_argument('--fur', choices=['strands', 'keep', 'remove'], default='keep', help='карточки шерсти без текстуры: keep как в файле, remove убрать, strands заменить прядями')
    ap.add_argument('--flip-v', action='store_true', help='перевернуть V-координату текстуры')
    ap.add_argument('--parents', help='JSON {"кость": "родитель"|null} с ручными поправками иерархии')
    ap.add_argument('--no-template', action='store_true', help='не использовать шаблон Biped, подбирать по геометрии')
    ap.add_argument('--beam', type=int, default=100, help='ширина поиска иерархии костей (больше = точнее и медленнее)')
    ap.add_argument('--ani', action='append', help='файл анимаций (.ani); можно несколько раз. По умолчанию берутся все .ani из папки модели, подходящие скелету')
    ap.add_argument('--no-anim', action='store_true', help='не добавлять анимации')
    ap.add_argument('--clips', help='JSON [{"name":..,"start":тики,"end":тики}, ...] с границами клипов')
    ap.add_argument('--lua', help='Lua-скрипт игры с onAdd(имя, кадр_начала, кадр_конца, цикл): клипы берутся оттуда')
    ap.add_argument('--model', help='имя модели в Lua (getModelName == ...), если в скрипте несколько веток')
    ap.add_argument('--timeline', action='store_true', help='добавить ещё и весь таймлайн одной анимацией')
    ap.add_argument('--no-auto-clips', action='store_true', help='не резать таймлайн эвристикой')
    ap.add_argument('--tick-rate', type=float, default=TICK_RATE, help='тиков в секунде (по умолчанию 60)')
    ap.add_argument('--obj', action='store_true', help='дополнительно сохранить .obj')
    ap.add_argument('--png', action='store_true', help='дополнительно сохранить итоговую текстуру в файл')
    ap.add_argument('--tree', action='store_true', help='показать дерево костей и точность посадки')
    ap.add_argument('--info', action='store_true', help='только вывести информацию, ничего не писать')
    ap.add_argument('-o', '--outdir', help='папка для результата (по умолчанию рядом с .msh)')
    a = ap.parse_args()
    overrides = json.load(open(a.parents)) if a.parents else None
    for path in a.files:
        base = os.path.splitext(os.path.basename(path))[0]
        try:
            m = parse_msh(path)
        except Exception as ex:
            print('%s: ошибка разбора: %s' % (path, ex))
            continue
        print('%s: версия %d, вершин %d (stride %d), треугольников %d, костей в палитре %d, скелет %s'
              % (os.path.basename(path), m.version, len(m.pos), m.stride, len(m.tris), len(m.bone_ids), m.skeleton))
        sk = None
        if m.bone_ids and m.wts and not a.no_skel:
            sp = a.ske or find_skeleton(path, m)
            if sp:
                try:
                    _, bones = parse_ske(sp)
                    print('  скелет: %s, костей %d' % (os.path.basename(sp), len(bones)))
                    if not a.info or a.tree or True:
                        sk = build_skeleton(m, bones, 'auto', a.beam, overrides, print, not a.no_template)
                        vals = list(sk.fit.values())
                        print('  иерархия: %s (кватернионы: %s), средняя посадка %.3f, худшая %.3f'
                              % (sk.method, 'инверсия' if sk.mode == 'conj' else 'как есть',
                                 sum(vals) / max(1, len(vals)), max(vals) if vals else 0))
                        diag = math.dist(m.bbox_min, m.bbox_max) or 1.0
                        if vals and sum(vals) / len(vals) > 0.05 * diag:
                            print('  ВНИМАНИЕ: посадка скелета плохая, иерархию стоит проверить (--tree, --parents)')
                        amb = ['%s (%s)' % (short_name(sk.bones[b].name),
                               ' / '.join(short_name(sk.bones[p].name) if p else 'корень' for p in ps))
                               for b, ps in sk.ambiguous.items()]
                        if amb:
                            print('  неоднозначные родители (выбран первый вариант по посадке): ' + '; '.join(amb))
                        if a.tree:
                            print(skeleton_tree(sk))
                except Exception as ex:
                    print('  скелет не добавлен: %s' % ex)
            else:
                print('  скелет не найден рядом (укажи --ske)')
        tex, tdesc = None, None
        if not a.no_tex:
            tp, others = (a.tex, None) if a.tex else find_texture(path, m)
            if tp:
                try:
                    w, h, px, tdesc, td = load_texture(tp)
                    print('  текстура: %s, %dx%d, %s' % (os.path.basename(tp), w, h, tdesc))
                    if not isinstance(px, RawImage) and any(c < 255 for c in px[3::4]):
                        bleed_colors(px, w, h)
                    tex = (w, h, px, td)
                except Exception as ex:
                    print('  текстура не добавлена: %s' % ex)
            elif others:
                print('  текстур рядом несколько (%s), укажи нужную через --tex' % ', '.join(others))
            elif not a.tex2:
                print('  текстура не найдена рядом (укажи --tex)')
            if a.tex2:
                try:
                    w2, h2, px2, d2, td2 = load_texture(a.tex2)
                    print('  вторая текстура: %s, %dx%d, %s' % (os.path.basename(a.tex2), w2, h2, d2))
                    second = tex_to_topdown_rgba(w2, h2, px2, td2)
                    if tex is None:
                        tex = (second[0], second[1], second[2], True)
                        print('  основной текстуры нет, используется только вторая')
                    else:
                        first = tex_to_topdown_rgba(tex[0], tex[1], tex[2], tex[3])
                        base_t, over_t = (first, second) if a.over == 2 else (second, first)
                        cw, ch, cpx = blend_textures(base_t, over_t, a.blend, a.opacity)
                        if any(c < 255 for c in cpx[3::4]):
                            bleed_colors(cpx, cw, ch)
                        tex = (cw, ch, cpx, True)
                        print('  наложение: текстура %d сверху, режим %s, непрозрачность %g'
                              % (a.over, a.blend, a.opacity))
                except Exception as ex:
                    print('  вторая текстура не добавлена: %s' % ex)
        anim, clips = None, None
        if sk is not None and not a.no_anim:
            cands = a.ani or find_anims(path, m)
            prefixes = model_prefixes(path, m)
            loaded = []
            for ap_ in cands:
                try:
                    tr_ = parse_ani(ap_)
                except Exception as ex:
                    print('  %s: не удалось прочитать: %s' % (os.path.basename(ap_), ex))
                    continue
                own = sum(1 for k in tr_ if k in sk.bones)
                if own == 0 or own < len(tr_) * 0.5 and not a.ani:
                    if a.ani:
                        print('  %s: треки не относятся к костям этого скелета, пропущен' % os.path.basename(ap_))
                    continue
                loaded.append((ap_, anim_label(ap_, prefixes), tr_))
            if not loaded:
                print('  .ani для этой модели не найдено' if not a.ani else '  ни один --ani не подошёл')
            clips = []
            used = set()

            def uniq(n_):
                k_, base_ = 2, n_
                while n_ in used:
                    n_ = '%s_%d' % (base_, k_)
                    k_ += 1
                used.add(n_)
                return n_
            main_idx = next((i for i, x in enumerate(loaded) if x[1] is None), None)
            if main_idx is None and (a.lua or a.clips) and loaded:
                main_idx = 0
            for i, (ap_, label, tr_) in enumerate(loaded):
                dur = anim_duration(tr_)
                unk = sum(1 for k in tr_ if k not in sk.bones)
                print('  анимации: %s, треков %d (не из скелета: %d), длина %g тиков = %.2f с при %g тик/с'
                      % (os.path.basename(ap_), len(tr_), unk, dur, dur / a.tick_rate, a.tick_rate))
                try:
                    if i == main_idx and a.lua:
                        lc = parse_lua_clips(a.lua)
                        if not lc:
                            raise ValueError('в Lua не нашёл вызовов onAdd')
                        pick = a.model
                        if pick is None:
                            stem = os.path.splitext(os.path.basename(path))[0]
                            pick = next((k for k in lc if k and k.lower() in stem.lower()), None)
                        if pick is None:
                            pick = next(iter(lc))
                            if len(lc) > 1:
                                print('  в Lua несколько моделей: %s; взята %s (укажи --model)' % (', '.join(map(str, lc)), pick))
                        if pick not in lc:
                            raise ValueError('модель %s не найдена в Lua; есть: %s' % (pick, ', '.join(map(str, lc))))
                        fps = lc[pick]['fps'] or 24.0
                        k = a.tick_rate / fps
                        new = [(uniq(n_), f0 * k, f1 * k, lp, tr_) for n_, f0, f1, lp in lc[pick]['clips']]
                        print('  клипы из Lua (модель %s, %g кадров/с, %g тиков на кадр)' % (pick, fps, k))
                        if a.timeline:
                            new.insert(0, (uniq('timeline'), 0, dur, None, tr_))
                    elif i == main_idx and a.clips:
                        new = [(uniq(c['name']), c['start'], c['end'], None, tr_) for c in json.load(open(a.clips))]
                    elif label is None:
                        new = [(uniq('timeline'), 0, dur, None, tr_)]
                        if not a.no_auto_clips:
                            for n_, (c0, c1) in enumerate(auto_clips(tr_), 1):
                                if c1 - c0 >= 10:
                                    new.append((uniq('clip%02d_%d-%d' % (n_, c0, c1)), c0, c1, None, tr_))
                    else:
                        new = [(uniq(label), 0, dur, None, tr_)]
                    clips += new
                except Exception as ex:
                    print('  %s: клипы не добавлены: %s' % (os.path.basename(ap_), ex))
            if clips:
                anim = loaded[0][2]
                print('  анимаций в GLB: %d' % len(clips))
                for cl in clips:
                    cn, c0, c1 = cl[:3]
                    print('    %-22s %7.1f - %7.1f тиков  (%.2f с)%s' % (cn, c0, c1, (c1 - c0) / a.tick_rate, '  цикл' if cl[3] else ''))
            else:
                clips = None
        if a.info:
            continue
        outdir = a.outdir or os.path.dirname(os.path.abspath(path))
        os.makedirs(outdir, exist_ok=True)
        glb, nb, nf = build_glb(m, a.fur, tex, sk, a.flip_v, base, anim, clips, a.tick_rate)
        out = os.path.join(outdir, base + '.glb')
        open(out, 'wb').write(glb)
        print('  -> %s (тело %d тр.%s)' % (out, nb, ', пряди %d тр.' % nf if nf else ''))
        if a.obj:
            write_obj(m, os.path.join(outdir, base + '.obj'))
        if a.png and tex:
            w, h, px = tex[:3]
            if isinstance(px, RawImage):
                open(os.path.join(outdir, base + '_texture.' + px.mime.split('/')[1].replace('jpeg', 'jpg')), 'wb').write(px.data)
            else:
                open(os.path.join(outdir, base + '_texture.png'), 'wb').write(
                    make_png(w, h, px, any(c < 255 for c in px[3::4])))


if __name__ == '__main__':
    main()
