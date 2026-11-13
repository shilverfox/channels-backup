# ISAAC-64 keystream as used by the Channels player (Isaac64Decryptor in chunks-common.js):
# randrsl[0] = seed, randinit(true), values consumed from randrsl[255] downwards, emitted big-endian.
M = (1 << 64) - 1
MAX_ENCRYPT_LENGTH = 131072
class Isaac64:
    def __init__(self, seed):
        self.rsl = [0] * 256; self.mm = [0] * 256
        self.aa = self.bb = self.cc = 0
        self.rsl[0] = int(seed) & M
        self._init()
    @staticmethod
    def _mix(v):
        a, b, c, d, e, f, g, h = v
        a = (a - e) & M; f ^= h >> 9; h = (h + a) & M
        b = (b - f) & M; g ^= (a << 9) & M; a = (a + b) & M
        c = (c - g) & M; h ^= b >> 23; b = (b + c) & M
        d = (d - h) & M; a ^= (c << 15) & M; c = (c + d) & M
        e = (e - a) & M; b ^= d >> 14; d = (d + e) & M
        f = (f - b) & M; c ^= (e << 20) & M; e = (e + f) & M
        g = (g - c) & M; d ^= f >> 17; f = (f + g) & M
        h = (h - d) & M; e ^= (g << 14) & M; g = (g + h) & M
        return [a, b, c, d, e, f, g, h]
    def _init(self):
        v = [0x9e3779b97f4a7c13] * 8
        for _ in range(4): v = self._mix(v)
        for src in (self.rsl, self.mm):
            for i in range(0, 256, 8):
                v = self._mix([(v[k] + src[i + k]) & M for k in range(8)])
                self.mm[i:i + 8] = v
        self._round(); self.cnt = 256
    def _round(self):
        mm = self.mm
        self.cc = (self.cc + 1) & M; self.bb = (self.bb + self.cc) & M
        aa, bb = self.aa, self.bb
        for i in range(256):
            x = mm[i]; k = i & 3
            if k == 0: aa = ~(aa ^ (aa << 21)) & M
            elif k == 1: aa ^= aa >> 5
            elif k == 2: aa = (aa ^ (aa << 12)) & M
            else: aa ^= aa >> 33
            aa = (aa + mm[(i + 128) & 255]) & M
            y = (mm[(x & 2040) >> 3] + aa + bb) & M; mm[i] = y
            bb = (mm[((y >> 8) & 2040) >> 3] + x) & M; self.rsl[i] = bb
        self.aa, self.bb = aa, bb
    def keystream(self, n):
        out = bytearray()
        while len(out) < n:
            if self.cnt == 0: self._round(); self.cnt = 256
            self.cnt -= 1
            out += self.rsl[self.cnt].to_bytes(8, 'big')
        return bytes(out[:n])
def decrypt_head(buf, seed):
    n = min(len(buf), MAX_ENCRYPT_LENGTH)
    ks = Isaac64(seed).keystream(n)
    return bytes(a ^ b for a, b in zip(buf[:n], ks)) + buf[n:]
