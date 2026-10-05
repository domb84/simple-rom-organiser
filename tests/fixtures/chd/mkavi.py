import struct, math, random, sys
def make(path, W=64, H=48, FRAMES=6, base='rel4', vtag=b'00db', FPS_N=30000, FPS_D=1001, RATE=48000, CH=2, seed=3, flat=False):
    rnd = random.Random(seed)
    def chunk(tag, data):
        return tag + struct.pack('<I', len(data)) + data + (b'\0' if len(data) & 1 else b'')
    def lst(kind, data):
        return b'LIST' + struct.pack('<I', len(data) + 4) + kind + data
    frames = []; audio = []
    spf = RATE * FPS_D // FPS_N
    t = 0
    for f in range(FRAMES):
        b = bytearray()
        for y in range(H):
            for x in range(0, W, 2):
                Y0 = (x * 3 + y * 2 + f * 5) & 0xFF; Y1 = (Y0 + (rnd.randrange(3) if y > H * 2 // 3 else 0)) & 0xFF
                if flat and y < H // 3:
                    b += bytes((16, 128, 16, 128))        # a flat band: runs to the end of the row
                elif flat and y < H // 2 and x < 18:
                    b += bytes((Y0, 100, Y1, 90))         # nine equal chroma samples: a run of exactly eight
                else:
                    b += bytes((Y0, 128 + (y & 15), Y1, 128 - (x & 15)))
        frames.append(bytes(b))
        a = bytearray()
        for i in range(spf + (1 if f % 2 else 0)):
            l = int(12000 * math.sin(t * 0.05)); r = int(8000 * math.sin(t * 0.021)) + rnd.randrange(-200, 200); t += 1
            a += struct.pack('<hh', l, r)[:2 * CH]
        audio.append(bytes(a))
    avih = struct.pack('<14I', 1000000 * FPS_D // FPS_N, 0, 0, 0x10, FRAMES, 0, 2, W * H * 2, W, H, 0, 0, 0, 0)
    strh_v = b'vids' + b'YUY2' + struct.pack('<IHHIIIIIIII', 0, 0, 0, 0, FPS_D, FPS_N, 0, FRAMES, W * H * 2, 0xFFFFFFFF, 0) + struct.pack('<4H', 0, 0, W, H)
    strf_v = struct.pack('<IiiHH4sIiiII', 40, W, H, 1, 16, b'YUY2', W * H * 2, 0, 0, 0, 0)
    total_samples = sum(len(a) for a in audio) // (2 * CH)
    strh_a = b'auds' + b'\0\0\0\0' + struct.pack('<IHHIIIIIIII', 0, 0, 0, 0, 1, RATE, 0, total_samples, RATE * 2 * CH, 0xFFFFFFFF, 2 * CH) + struct.pack('<4H', 0, 0, 0, 0)
    strf_a = struct.pack('<HHIIHH', 1, CH, RATE, RATE * 2 * CH, 2 * CH, 16)
    hdrl = lst(b'hdrl', chunk(b'avih', avih) + lst(b'strl', chunk(b'strh', strh_v) + chunk(b'strf', strf_v)) + lst(b'strl', chunk(b'strh', strh_a) + chunk(b'strf', strf_a)))
    movi_start = 12 + len(hdrl) + 8          # file offset of the 'movi' fourcc
    off = {'rel4': 4, 'rel0': 0, 'abs': movi_start + 4}[base]
    movi_body = b''; idx = b''
    for f in range(FRAMES):
        for tag, data in ((vtag, frames[f]), (b'01wb', audio[f])):
            c = chunk(tag, data); idx += tag + struct.pack('<III', 0x10, off, len(data)); off += len(c); movi_body += c
    movi = lst(b'movi', movi_body)
    body = b'AVI ' + hdrl + movi + (chunk(b'idx1', idx) if base != 'none' else b'')
    open(path, 'wb').write(b'RIFF' + struct.pack('<I', len(body)) + body)
if __name__ == '__main__':
    for base in ('rel4', 'rel0', 'abs'):
        for vtag in (b'00db', b'00dc'):
            make(f'ld_{base}_{vtag.decode()}.avi', base=base, vtag=vtag)
