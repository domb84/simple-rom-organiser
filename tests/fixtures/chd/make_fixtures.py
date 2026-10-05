"""Sources of the real-chdman fixtures in tests/fixtures/chd (all content is generated here)."""
import math, random, struct, sys, hashlib, json
sys.path.insert(0, '/home/deck/Dev/simple-rom-organiser')
sys.path.insert(0, '/home/deck/.cache/romorg-parity-test')
from tests import chdtestlib as T
import mkavi
rnd = random.Random(2026)
def pcm(n, seed):
    r = random.Random(seed); out = bytearray(); t = 0
    for _ in range(n // 4):
        l = int(9000 * math.sin(t * 0.031) + 4000 * math.sin(t * 0.0017)) + r.randrange(-40, 40)
        rr = int(7000 * math.sin(t * 0.027 + 1)) + r.randrange(-60, 60); t += 1
        out += struct.pack('<hh', l, rr)
    return bytes(out)
words = [''.join(rnd.choice('abcdefghijklmnopqrstuvwxyz') for _ in range(rnd.randrange(2, 9))) for _ in range(300)]
text = ' '.join(rnd.choice(words) for _ in range(6000)).encode()[:24576].ljust(24576, b'.')
skew = bytes(rnd.choices(range(256), weights=[1000 / (i + 1) ** 1.3 for i in range(256)], k=24576))
flat = bytes(rnd.choice(b'ABCDEFGH') for _ in range(4096))
src = pcm(24576, 1) + skew + text + (b'ABCDEFGH' * 512) * 2 + bytes(8192) + flat + bytes(rnd.randrange(256) for _ in range(4096))
src += bytes(-len(src) % 4096)
assert len(src) % 2048 == 0
open('src.img', 'wb').write(src)
b = bytearray(src)
b[4096:8192] = src[28672:32768]           # a hunk of the parent at another place
b[40000] ^= 0x55                           # one changed byte
b[61440:65536] = bytes(rnd.randrange(256) for _ in range(4096))
open('src2.img', 'wb').write(bytes(b))
# a tiny CD: MODE1 data with valid ECC (so chdman strips it), then audio
data = T.make_data_track(24, 5)
audio = pcm(20 * 2352, 7)
open('cd.bin', 'wb').write(data + audio)
open('cd.cue', 'w').write('FILE "cd.bin" BINARY\n  TRACK 01 MODE1/2352\n    INDEX 01 00:00:00\n  TRACK 02 AUDIO\n    INDEX 01 00:00:24\n')
mkavi.make('ld.avi', W=64, H=48, FRAMES=5, vtag=b'00dc', seed=4, CH=1)
mkavi.make('ld2.avi', W=80, H=48, FRAMES=4, vtag=b'00dc', seed=8, CH=2, flat=True)
json.dump({'src': hashlib.sha1(src).hexdigest(), 'src2': hashlib.sha1(bytes(b)).hexdigest(),
           'cd_data': hashlib.sha1(data).hexdigest(), 'cd_audio': hashlib.sha1(audio).hexdigest()}, open('sums.json', 'w'), indent=1)
print(len(src))
