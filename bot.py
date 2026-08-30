#!/usr/bin/env python3
"""
Uniswap V3 range-edge bot. Semua target dibaca dari environment.

Mode PASSIVE saja: tidak membuang umpan. Memantau pool; begitu ada USDC muncul
di dalam range (dari pembeli mana pun), langsung
jual token secukupnya untuk mengurasnya. Kalau kalah balapan, transaksi revert
dan hanya rugi gas (~$0,0026).

Mode ACTIVE terbukti rugi di semua ukuran umpan dan dikunci.
"""
import os, sys, time, json, math, signal, logging, urllib.request, http.client
from urllib.parse import urlparse
from datetime import datetime, timezone
from web3 import Web3

# ---------------------------------------------------------------- konfigurasi
RPC_URL   = os.getenv("ARC_RPC") or sys.exit("ARC_RPC belum diset")
DRY_RUN   = os.getenv("DRY_RUN", "true").lower() != "false"
MODE      = os.getenv("MODE", "PASSIVE").upper()

# Semua target dibaca dari environment supaya repo publik tidak membocorkan
# pool mana yang dikerjakan. Isi lewat GitHub Secrets.
def _addr(name, default=""):
    v = os.getenv(name, default)
    if not v: raise SystemExit(f"{name} belum diset (isi di GitHub Secrets / .env)")
    return Web3.to_checksum_address(v)
POOL   = _addr("POOL_ADDR")
BASE  = _addr("TOKEN_ADDR")
USDC   = _addr("QUOTE_ADDR")
ROUTER = _addr("ROUTER_ADDR")
FEE        = int(os.getenv("POOL_FEE", "10000"))
CHAIN_ID   = int(os.getenv("CHAIN_ID", "0")) or sys.exit("CHAIN_ID belum diset")
TICK_FLOOR = int(os.getenv("TICK_FLOOR", "0"))
Q96        = 2**96
EXPECTED_LG = int(os.getenv("EXPECTED_LG", "0"))   # jangkar struktur LP
LOW_INV     = float(os.getenv("LOW_INV", "3000"))   # peringatan stok BASE menipis

# Gate berbasis PROFIT, bukan angka dolar sembarangan. Margin jual ~64% di semua
# ukuran (harga jual efektif ~$0,00070-0,00075 vs modal beli), jadi yang benar-benar
# membatasi hanya gas. Titik impas: avail ~$0,0041.
TOKEN_COST = float(os.getenv("TOKEN_COST", "0.00026"))  # harga belimu di pool asli
MIN_PROFIT = float(os.getenv("MIN_PROFIT", "0.005"))    # profit bersih minimum per panen
# Arc pakai EIP-1559. Operator terpantau membayar priority 25-33 Gwei dan menang
# inklusi di +4 blok. Transaksi legacy dgn gasPrice 40 G hanya setara priority
# 20 G -> kalah tawar. Kita kirim tipe-2 dgn priority eksplisit di atas mereka.
# Gas bertingkat. Siklus kecil ($0,05) tidak diperebutkan operator — mereka
# membiarkannya menganggur berjam-jam — jadi priority tinggi hanya membakar
# sepertiga profitnya. Kejadian besar diperebutkan, dan di sana gas tidak
# berarti apa-apa dibanding hadiahnya. Pemilihan ini murni lokal: nol RPC,
# nol tambahan latensi.
PRIO_LOW   = float(os.getenv("PRIO_LOW",   "25"))       # peluang receh, tanpa lawan
PRIO_GWEI  = float(os.getenv("PRIO_GWEI",  "100"))      # peluang menengah, berebut
PRIO_MAX   = float(os.getenv("PRIO_MAX",   "1000"))     # peluang besar, jangan sampai kalah
PRIO_BATAS = float(os.getenv("PRIO_BATAS", "0.50"))     # batas receh -> berebut
BATAS_MAX  = float(os.getenv("BATAS_MAX",  "100"))      # batas berebut -> brutal
# --- sisi BELI: memborong token murah sesudah ada yang dump ---
# Saat harga jatuh di bawah lantai, token di zona tipis bisa ditebus jauh di
# bawah harga wajar. Dibeli untuk STOK, bukan untuk dijual lagi di pool ini
# (pool ini kosong). Nilainya diukur pakai TOKEN_COST = harga wajar di DEX lain.
BUY_ON      = os.getenv("BUY_ON", "true").lower() != "false"
MIN_BUY_NET = float(os.getenv("MIN_BUY_NET", "0.05"))   # untung bersih minimum
MAX_BUY_USD = float(os.getenv("MAX_BUY_USD", "2.00"))   # batas belanja per aksi
USDC_SISA   = float(os.getenv("USDC_SISA", "2.00"))     # sisakan segini untuk gas
DUST       = float(os.getenv("DUST", "0.004"))          # abaikan di bawah ini
SLIPPAGE   = float(os.getenv("SLIPPAGE", "0.97"))     # amountOutMinimum = perkiraan x ini
# Jendela balapan ~100 detik (jeda antara bot lawan beli dan menarik uangnya balik).
# Polling 2 detik masih memberi ~50 kesempatan, tapi beban RPC turun 5x.
POLL_SEC   = float(os.getenv("POLL_SEC", "0.7"))
MAX_PER_DAY= int(os.getenv("MAX_PER_DAY", "40"))      # reset otomatis tiap hari UTC
MAX_RUNTIME= int(os.getenv("MAX_RUNTIME", "0"))       # detik; 0 = tanpa batas
STATE_FILE = os.getenv("STATE_FILE", "state.json")
TG_TOKEN, TG_CHAT = os.getenv("TG_TOKEN"), os.getenv("TG_CHAT")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout),
              logging.FileHandler(os.getenv("LOG_FILE", "bot.log"))])
log = logging.getLogger("arb").info

def notify(msg):
    if not (TG_TOKEN and TG_CHAT): return
    try:
        urllib.request.urlopen(urllib.request.Request(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data=json.dumps({"chat_id": TG_CHAT, "text": msg}).encode(),
            headers={"content-type": "application/json"}), timeout=10)
    except Exception as e:
        log(f"  notify gagal: {e}")

PK = os.getenv("PRIVATE_KEY")
POOL_ABI  = json.loads('[{"inputs":[],"name":"slot0","outputs":[{"type":"uint160"},{"type":"int24"},{"type":"uint16"},{"type":"uint16"},{"type":"uint16"},{"type":"uint8"},{"type":"bool"}],"stateMutability":"view","type":"function"},{"inputs":[],"name":"liquidity","outputs":[{"type":"uint128"}],"stateMutability":"view","type":"function"},{"inputs":[{"type":"int24"}],"name":"ticks","outputs":[{"type":"uint128"},{"type":"int128"},{"type":"uint256"},{"type":"uint256"},{"type":"int56"},{"type":"uint160"},{"type":"uint32"},{"type":"bool"}],"stateMutability":"view","type":"function"}]')
ERC20_ABI = json.loads('[{"inputs":[{"type":"address"}],"name":"balanceOf","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"},{"inputs":[{"type":"address"},{"type":"uint256"}],"name":"approve","outputs":[{"type":"bool"}],"stateMutability":"nonpayable","type":"function"},{"inputs":[{"type":"address"},{"type":"address"}],"name":"allowance","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"}]')
ROUTER_ABI= json.loads('[{"inputs":[{"components":[{"name":"tokenIn","type":"address"},{"name":"tokenOut","type":"address"},{"name":"fee","type":"uint24"},{"name":"recipient","type":"address"},{"name":"amountIn","type":"uint256"},{"name":"amountOutMinimum","type":"uint256"},{"name":"sqrtPriceLimitX96","type":"uint160"}],"name":"params","type":"tuple"}],"name":"exactInputSingle","outputs":[{"type":"uint256"}],"stateMutability":"payable","type":"function"}]')
TICK_UPPER = int(os.getenv("TICK_UPPER", "0"))   # atap posisi tebal

def get_sqrt_ratio_at_tick(tick: int) -> int:
    """TickMath.getSqrtRatioAtTick — integer eksak, identik dengan on-chain.
    Versi float sebelumnya meleset ~4 miliar dan menolak pembacaan yang sah
    ketika harga duduk persis di batas tick."""
    a = abs(tick)
    r = 0xfffcb933bd6fad37aa2d162d1a594001 if a & 0x1 else 1 << 128
    for bit, mul in ((0x2,0xfff97272373d413259a46990580e213a),(0x4,0xfff2e50f5f656932ef12357cf3c7fdcc),
                     (0x8,0xffe5caca7e10e4e61c3624eaa0941cd0),(0x10,0xffcb9843d60f6159c9db58835c926644),
                     (0x20,0xff973b41fa98c081472e6896dfb254c0),(0x40,0xff2ea16466c96a3843ec78b326b52861),
                     (0x80,0xfe5dee046a99a2a811c461f1969c3053),(0x100,0xfcbe86c7900a88aedcffc83b479aa3a4),
                     (0x200,0xf987a7253ac413176f2b074cf7815e54),(0x400,0xf3392b0822b70005940c7a398e4b70f3),
                     (0x800,0xe7159475a2c29b7443b29c7fa6e889d9),(0x1000,0xd097f3bdfd2022b8845ad8f792aa5825),
                     (0x2000,0xa9f746462d870fdf8a65dc1f90e061e5),(0x4000,0x70d869a156d2a1b890bb3df62baf32f7),
                     (0x8000,0x31be135f97d08fd981231505542fcfa6),(0x10000,0x9aa508b5b7a84e1c677de54f3e99bc9),
                     (0x20000,0x5d6af8dedb81196699c329225ee604),(0x40000,0x2216e584f5fa1ea926041bedfe98),
                     (0x80000,0x48a170391f7dc42444e8fa2)):
        if a & bit: r = (r * mul) >> 128
    if tick > 0: r = ((1 << 256) - 1) // r
    return (r >> 32) + (0 if r % (1 << 32) == 0 else 1)

SQ_FLOOR = get_sqrt_ratio_at_tick(TICK_FLOOR)
SQ_UPPER = get_sqrt_ratio_at_tick(TICK_UPPER) if TICK_UPPER else None
L_IN  = int(os.getenv("L_IN",  "0"))   # likuiditas aktif DI DALAM rentang tebal
L_OUT = int(os.getenv("L_OUT", "0"))   # likuiditas aktif DI LUAR rentang (tipis)

def px(sq): return (sq / Q96) ** 2 * 1e12

KOSONG = {"day":"", "cycles":0, "usdc":0.0, "tokens":0.0, "fails":0,
          # total seumur hidup — TIDAK direset saat ganti hari / ganti run
          "tot_cycles":0, "tot_usdc":0.0, "tot_tokens":0.0, "tot_fails":0,
          "tot_gas":0.0, "sejak":""}
def load_state():
    try:
        d = json.load(open(STATE_FILE))
        for k, v in KOSONG.items(): d.setdefault(k, v)
        return d
    except Exception:
        d = dict(KOSONG); d["sejak"] = datetime.now(timezone.utc).strftime("%Y-%m-%d"); return d

def save_state(s):
    try: json.dump(s, open(STATE_FILE, "w"), indent=1)
    except Exception as e: log(f"  save state gagal: {e}")

RUN = True
def stop(*_):
    global RUN; RUN = False; log("sinyal berhenti diterima, keluar rapi...")
signal.signal(signal.SIGTERM, stop); signal.signal(signal.SIGINT, stop)

class Bot:
    def __init__(self):
        self.connect()
        self.st = load_state()
        self.last_report = 0
        self.warned_low = False
        self.lose_streak = 0
        self.last_skip = None
        self.last_sq = None   # dry-run: cegah panen ulang pada state yang sama
        self._nonce = None; self._gas = None; self._gas_t = 0
        self._bal = None; self._bal_t = 0
        self._usdc = None; self._usdc_t = 0

    # ---- lapisan RPC mentah: koneksi persisten, tanpa eth_chainId tersembunyi ----
    def _raw(self, method, params):
        body = json.dumps({"jsonrpc":"2.0","id":1,"method":method,"params":params})
        for attempt in (0, 1):
            try:
                if self._conn is None:
                    u = urlparse(RPC_URL)
                    self._conn = (http.client.HTTPSConnection(u.hostname, 443, timeout=10)
                                  if u.scheme == "https" else
                                  http.client.HTTPConnection(u.hostname, u.port or 80, timeout=10))
                    self._path = u.path or "/"
                self._conn.request("POST", self._path, body, {"content-type":"application/json"})
                d = json.loads(self._conn.getresponse().read())
                if "error" in d: raise RuntimeError(d["error"])
                return d["result"]
            except RuntimeError: raise
            except Exception:
                try: self._conn.close()
                except Exception: pass
                self._conn = None
                if attempt: raise

    def _call(self, to, data):
        return self._raw("eth_call", [{"to": to, "data": data}, "latest"])

    def connect(self):
        self._conn = None; self._path = "/"
        self.w3 = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": 15}))
        self.acct = self.w3.eth.account.from_key(PK) if PK else None
        self.me = self.acct.address if self.acct else "0x" + "0"*40
        self.pool   = self.w3.eth.contract(address=POOL,   abi=POOL_ABI)
        self.router = self.w3.eth.contract(address=ROUTER, abi=ROUTER_ABI)
        self.base  = self.w3.eth.contract(address=BASE,  abi=ERC20_ABI)

    # ---- perhitungan ----
    def _segments(self, sq):
        """Zona harga yang dilewati saat MENJUAL, dari sq turun ke lantai.
        Kalau harga di atas atap, ada DUA zona dengan likuiditas berbeda —
        versi lama hanya menghitung satu, sehingga berhenti di atap dan
        meninggalkan sebagian besar USDC untuk diambil orang lain."""
        segs = []
        if SQ_UPPER and sq > SQ_UPPER:
            segs.append((sq, SQ_UPPER, L_OUT))
            sq = SQ_UPPER
        if sq > SQ_FLOOR:
            segs.append((sq, SQ_FLOOR, L_IN))
        return segs

    # ---------- sisi BELI ----------
    def usdc_to_floor(self, sq):
        """USDC yang dibutuhkan untuk mengangkat harga dari sq ke lantai.
        Di atas lantai token dihargai ~$0,0007 — lebih mahal dari harga wajar
        $0,00026 — jadi membeli melewati lantai justru rugi. Berhenti di lantai."""
        if sq >= SQ_FLOOR: return 0.0
        return L_OUT * (SQ_FLOOR - sq) / Q96 / 1e6 / 0.99

    def tokens_from_buy(self, usd, sq):
        """Token yang didapat kalau membelanjakan `usd` mulai dari harga sq."""
        if sq >= SQ_FLOOR or usd <= 0: return 0.0
        d = usd * 0.99 * 1e6
        butuh = L_OUT * (SQ_FLOOR - sq) / Q96
        if d >= butuh:
            return L_OUT * (Q96 / sq - Q96 / SQ_FLOOR) / 1e18
        s2 = sq + d * Q96 / L_OUT
        return L_OUT * (Q96 / sq - Q96 / s2) / 1e18

    def usdc_balance(self):
        if self._usdc is None or time.time() - self._usdc_t > 120:
            self._usdc = int(self._raw("eth_getBalance", [self.me, "latest"]), 16) / 1e18
            self._usdc_t = time.time()
        return self._usdc

    def usdc_available(self, sq, L):
        return sum(Lseg * (hi - lo) / Q96 / 1e6 for hi, lo, Lseg in self._segments(sq))

    def tokens_to_drain(self, sq, L):
        raw = sum(Lseg * (Q96 / lo - Q96 / hi) for hi, lo, Lseg in self._segments(sq))
        return raw / 1e18 / 0.99

    def usdc_for_tokens(self, tokens, sq, L):
        """USDC hasil menjual `tokens`, berjalan lintas zona."""
        if tokens <= 0: return 0.0
        d = tokens * 0.99 * 1e18
        got = 0.0
        for hi, lo, Lseg in self._segments(sq):
            butuh = Lseg * (Q96 / lo - Q96 / hi)
            if d >= butuh:
                got += Lseg * (hi - lo) / Q96 / 1e6; d -= butuh
            else:
                s2 = Q96 / (Q96 / hi + d / Lseg)
                got += Lseg * (hi - s2) / Q96 / 1e6; d = 0
            if d <= 0: break
        return got

    def read_state(self):
        """slot0 + liquidity. Batch dimatikan di RPC ini, jadi 2 request —
        tetap lebih hemat dari 3 (dulu ada eth_blockNumber untuk pin blok).
        Pengganti pin: cek konsistensi. Di atas lantai likuiditas harus tebal,
        di bawah lantai harus tipis. Kalau tidak cocok, kedua nilai berasal dari
        blok berbeda -> tick ini dilewati, tidak dipakai untuk keputusan jual."""
        sq = int(self._call(POOL, "0x3850c7bd")[2:66], 16)
        # L diturunkan dari harga, tidak dipanggil ke RPC. Struktur pool sudah
        # diverifikasi tiap 15 menit lewat verify_structure(), jadi aman.
        # Efek: polling 2 panggilan -> 1. Hemat ~400 ms per siklus.
        L = L_IN if (SQ_UPPER and SQ_FLOOR <= sq < SQ_UPPER) else L_OUT
        # Posisi TEBAL hanya aktif di [SQ_FLOOR, SQ_UPPER). Di luar itu — termasuk
        # DI ATAS atap, yang terjadi saat pembelian besar — likuiditas kembali tipis.
        return sq, L

    def verify_structure(self):
        """Jangkar: posisi LP operator harus masih sama. Kalau mereka burn/mint ulang
        di rentang lain, TICK_FLOOR tidak lagi valid dan semua hitungan jadi salah."""
        lg = int(self._call(POOL, "0xf30dba93" + f"{(TICK_FLOOR % (1<<256)):064x}")[2:66], 16)
        if lg != EXPECTED_LG:
            raise SystemExit(f"STRUKTUR LP BERUBAH: liquidityGross@{TICK_FLOOR} "
                             f"{lg:,} != {EXPECTED_LG:,}. Bot dihentikan — "
                             f"matematika lantai harga tidak lagi valid.")

    # ---- aksi ----
    def ensure_approval(self):
        if DRY_RUN or not self.acct: return
        for tok, nama in ((BASE, "token"), (USDC, "USDC")):
            cur = int(self._call(tok, "0xdd62ed3e" + "0"*24 + self.me[2:].lower()
                                 + "0"*24 + ROUTER[2:].lower()), 16)
            if cur >= 2**200: continue
            log(f"approve router untuk {nama} (sekali saja)...")
            self._approve(tok)
        return

    def _approve(self, tok):
        mx, prio, _ = self.fees(0)
        c = self.w3.eth.contract(address=tok, abi=ERC20_ABI)
        tx = c.functions.approve(ROUTER, 2**256-1).build_transaction({
            "from": self.me, "nonce": self.nonce(), "gas": 100000, "chainId": CHAIN_ID,
            "type": 2, "maxFeePerGas": mx, "maxPriorityFeePerGas": prio})
        s = self.w3.eth.account.sign_transaction(tx, PK)
        self.w3.eth.wait_for_transaction_receipt(
            self.w3.eth.send_raw_transaction(s.raw_transaction), timeout=90)
        log("approve selesai")

    def gas_price(self):
        """baseFee di-cache 60 detik. Di jalur balapan, satu round-trip RPC
        (~220 ms) lebih mahal daripada base fee yang sedikit basi."""
        if self._gas is None or time.time() - self._gas_t > 60:
            b = self._raw("eth_getBlockByNumber", ["latest", False])
            self._gas = int(b["baseFeePerGas"], 16) if b.get("baseFeePerGas") else int(self._raw("eth_gasPrice", []), 16)
            self._gas_t = time.time()
        return self._gas

    def fees(self, expected=None):
        """(maxFee, priority, label) bertingkat tiga.
        Di peluang >$100, gas 1000 G hanya 0,071% dari hadiah — kalah balapan
        jauh lebih mahal daripada gasnya. Di peluang receh sebaliknya: gas
        tinggi memakan sepertiga profit, dan tidak ada yang memperebutkannya."""
        base = self.gas_price()
        if expected is None or expected >= BATAS_MAX: g, tag = PRIO_MAX, "BRUTAL"
        elif expected >= PRIO_BATAS:                  g, tag = PRIO_GWEI, "berebut"
        else:                                         g, tag = PRIO_LOW, "santai"
        prio = int(g * 1e9)
        return base * 2 + prio, prio, tag

    def token_balance(self):
        """Saldo token di-cache 5 menit; dikurangi lokal tiap kali menjual."""
        if self._bal is None or time.time() - self._bal_t > 300:
            self._bal = int(self._call(BASE, "0x70a08231" + "0"*24 + self.me[2:].lower()), 16) / 1e18
            self._bal_t = time.time()
        return self._bal

    def nonce(self):
        if self._nonce is None:
            self._nonce = int(self._raw("eth_getTransactionCount", [self.me, "pending"]), 16)
        return self._nonce

    def swap(self, amt_in, min_out, expected, jual=True):
        """jual=True: BASE->USDC (panen).  jual=False: USDC->BASE (borong murah)."""
        tin, tout = (BASE, USDC) if jual else (USDC, BASE)
        din, dout = (18, 6) if jual else (6, 18)
        if DRY_RUN:
            log(f"  [DRY] {'jual' if jual else 'beli'} {amt_in:,.4f} -> min {min_out:,.4f}")
            return True
        if not self.acct:
            log("  !! PRIVATE_KEY belum diset"); return False
        params = (tin, tout, FEE, self.me, int(amt_in * 10**din), int(min_out * 10**dout), 0)
        mx, prio, tag = self.fees(expected)
        log(f"  gas: priority {prio/1e9:,.0f} G [{tag}]")
        tx = self.router.functions.exactInputSingle(params).build_transaction({
            "from": self.me, "nonce": self.nonce(), "gas": 300000, "chainId": CHAIN_ID,
            "type": 2, "maxFeePerGas": mx, "maxPriorityFeePerGas": prio})
        signed = self.w3.eth.account.sign_transaction(tx, PK)
        h = self._raw("eth_sendRawTransaction", ["0x"+signed.raw_transaction.hex()])
        rc = None; t0 = time.time()
        while time.time() - t0 < 90:
            rc = self._raw("eth_getTransactionReceipt", [h])
            if rc: break
            time.sleep(0.5)
        ok = bool(rc) and rc.get("status") == "0x1"
        self._nonce += 1
        self._bal = None; self._usdc = None
        log(f"  -> {'BERHASIL' if ok else 'REVERT'} {h}")
        return ok

    def harvest(self, tokens, expected):
        if DRY_RUN:
            log(f"  [DRY] jual {tokens:,.1f} BASE -> perkiraan ${expected:.4f}")
            return True
        if not self.acct:
            log("  !! PRIVATE_KEY belum diset"); return False
        params = (BASE, USDC, FEE, self.me, int(tokens*1e18),
                  int(expected*SLIPPAGE*1e6), 0)
        mx, prio, tag = self.fees(expected)
        log(f"  gas: priority {prio/1e9:,.0f} G [{tag}]  biaya ~${(mx)*173080/1e18:.4f}")
        tx = self.router.functions.exactInputSingle(params).build_transaction({
            "from": self.me, "nonce": self.nonce(), "gas": 300000, "chainId": CHAIN_ID,
            "type": 2, "maxFeePerGas": mx, "maxPriorityFeePerGas": prio})
        s = self.w3.eth.account.sign_transaction(tx, PK)
        h = self.w3.eth.send_raw_transaction(s.raw_transaction)
        rc = self.w3.eth.wait_for_transaction_receipt(h, timeout=90)
        ok = rc.status == 1
        self._nonce += 1
        if ok and self._bal is not None: self._bal -= tokens
        else: self._bal = None          # revert: paksa baca ulang saldo
        log(f"  -> {'BERHASIL' if ok else 'REVERT (kalah balapan, rugi gas saja)'} {h}")
        return ok

    def roll_day(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.st.get("day") != today:
            if self.st.get("day"):
                notify(f"ringkasan {self.st['day']}\n"
                       f"panen {self.st['cycles']}x  ${self.st['usdc']:.4f}\n"
                       f"token terpakai {self.st['tokens']:,.0f}  gagal {self.st['fails']}x\n"
                       f"─────────\n"
                       f"TOTAL sejak {self.st.get('sejak') or '?'}: "
                       f"{self.st['tot_cycles']}x  ${self.st['tot_usdc']:.4f}")
            # reset harian saja; total seumur hidup dibawa terus
            for k in ("cycles","fails"): self.st[k] = 0
            for k in ("usdc","tokens"): self.st[k] = 0.0
            self.st["day"] = today
            save_state(self.st)

    def tick(self):
        self.roll_day()
        sq, L = self.read_state()
        avail = self.usdc_available(sq, L)

        now = time.time()
        if now - self.last_report > 900:          # heartbeat tiap 15 menit
            tn = self.st["tot_usdc"] - self.st["tot_tokens"]*TOKEN_COST - self.st["tot_gas"]
            log(f"hidup | harga ${px(sq):.10f} | tersedia ${avail:.4f} | "
                f"hari ini {self.st['cycles']}x ${self.st['usdc']:.4f} | "
                f"TOTAL {self.st['tot_cycles']}x kotor ${self.st['tot_usdc']:.4f} bersih ${tn:+.4f}")
            self.last_report = now
            self.verify_structure()

        # ---- SISI BELI: harga di bawah lantai = token murah, borong untuk stok ----
        if BUY_ON and sq < SQ_FLOOR and self.acct:
            perlu = self.usdc_to_floor(sq)
            kas = max(self.usdc_balance() - USDC_SISA, 0.0)
            belanja = min(perlu, kas, MAX_BUY_USD)
            if belanja > 0:
                dapat = self.tokens_from_buy(belanja, sq)
                gas_c = (self.gas_price() + int(PRIO_LOW*1e9)) * 200000 / 1e18
                net = dapat * TOKEN_COST - belanja - gas_c
                if net >= MIN_BUY_NET:
                    log(f"BORONG ${belanja:.4f} -> {dapat:,.0f} token "
                        f"(nilai ${dapat*TOKEN_COST:.4f}) | bersih ${net:+.4f}")
                    if self.swap(belanja, dapat*0.97, net, jual=False):
                        self.st["tot_beli"] = self.st.get("tot_beli",0)+1
                        self.st["tot_beli_usd"] = self.st.get("tot_beli_usd",0.0)+belanja
                        self.st["tot_beli_tok"] = self.st.get("tot_beli_tok",0.0)+dapat
                        save_state(self.st)
                        notify(f"BORONG {dapat:,.0f} token seharga ${belanja:.4f}\n"
                               f"nilai wajar ${dapat*TOKEN_COST:.4f}  bersih ${net:+.4f}\n"
                               f"stok bertambah — modal 9x lebih murah dari DEX")
                        time.sleep(3)
                    return
                elif self.last_skip != round(sq/1e18,2):
                    log(f"  lewati borong: ${belanja:.4f} -> {dapat:,.0f} tok, bersih ${net:+.4f} (< ${MIN_BUY_NET})")
                    self.last_skip = round(sq/1e18,2)

        if avail < DUST: return
        if DRY_RUN and sq == self.last_sq: return   # state belum berubah, sudah dihitung
        if self.st["cycles"] >= MAX_PER_DAY:
            log("  batas harian tercapai, menunggu reset UTC"); time.sleep(30); return

        need = self.tokens_to_drain(sq, L)
        bal  = self.token_balance() if self.acct else need
        sell = min(need, bal)
        if sell <= 0:
            log(f"  !! ${avail:.4f} tersedia tapi stok BASE habis — isi ulang dompet bot")
            notify("STOK BASE HABIS — bot tidak bisa panen. Isi ulang dompet.")
            time.sleep(60); return
        if bal < LOW_INV and not self.warned_low:
            log(f"  !! stok menipis: {bal:,.0f} BASE"); notify(f"stok BASE menipis: {bal:,.0f}")
            self.warned_low = True
        expected = self.usdc_for_tokens(sell, sq, L)
        # --- gate profit: jual kalau bersihnya positif, bukan kalau angkanya besar ---
        # perkiraan kasar dulu (pakai priority rendah) untuk gate profit;
        # nilai pastinya dihitung ulang setelah ukuran panen diketahui
        gas_cost = (self.gas_price() + int(PRIO_LOW * 1e9)) * 130000 / 1e18
        profit = expected - sell * TOKEN_COST - gas_cost
        if profit < MIN_PROFIT:
            if self.last_skip != round(avail, 4):
                log(f"  lewati: avail ${avail:.4f} -> bersih ${profit:+.4f} "
                    f"(< ${MIN_PROFIT}); modal {sell:,.0f} tok ${sell*TOKEN_COST:.4f} "
                    f"+ gas ${gas_cost:.4f}")
                self.last_skip = round(avail, 4)
            return
        if bal < need:
            log(f"  stok kurang ({bal:,.0f} < {need:,.0f}) — hanya bisa ambil "
                f"${expected:.4f} dari ${avail:.4f}")
        log(f"PANEN ${avail:.4f} -> jual {sell:,.1f} BASE = ${expected:.4f} "
            f"| bersih ${profit:+.4f}")

        self.last_sq = sq
        if self.harvest(sell, expected):
            self.warned_low = False; self.lose_streak = 0
            self.st["cycles"] += 1; self.st["usdc"] += expected; self.st["tokens"] += sell
            self.st["tot_cycles"] += 1; self.st["tot_usdc"] += expected
            self.st["tot_tokens"] += sell; self.st["tot_gas"] += gas_cost
            if not self.st.get("sejak"):
                self.st["sejak"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            bersih = expected - sell * TOKEN_COST - gas_cost
            tot_net = (self.st["tot_usdc"] - self.st["tot_tokens"] * TOKEN_COST
                       - self.st["tot_gas"])
            notify(f"PANEN ${expected:.4f}  (bersih ${bersih:+.4f})\n"
                   f"jual {sell:,.0f} token @ ${expected/sell:.6f}\n"
                   f"hari ini {self.st['cycles']}x  ${self.st['usdc']:.4f}\n"
                   f"─────────\n"
                   f"TOTAL {self.st['tot_cycles']}x panen  kotor ${self.st['tot_usdc']:.4f}\n"
                   f"BERSIH ${tot_net:+.4f}  sejak {self.st.get('sejak')}")
        else:
            self.st["fails"] += 1; self.st["tot_fails"] += 1; self.lose_streak += 1
            # Kalah balapan di peluang besar = info paling penting untuk dievaluasi.
            if expected >= 1.0:
                notify(f"KALAH BALAPAN — peluang ${expected:.2f} lolos\n"
                       f"priority {PRIO_GWEI}G, gagal {self.lose_streak}x berturut\n"
                       f"kalau ini berulang, operator menaikkan tawaran mereka")
            if self.lose_streak >= 3:
                log(f"  kalah {self.lose_streak}x berturut — jeda 60s "
                    f"(ada yang lebih cepat; berhenti membakar gas)")
                time.sleep(60); self.lose_streak = 0
        save_state(self.st); time.sleep(3)

    def run(self):
        log(f"mode={MODE} dry_run={DRY_RUN} akun={self.me}")
        log(f"lantai ${px(SQ_FLOOR):.8f} | modal/token ${TOKEN_COST} | profit min ${MIN_PROFIT} | "
            f"priority {PRIO_LOW:.0f}/{PRIO_GWEI:.0f}/{PRIO_MAX:.0f}G "
            f"(batas ${PRIO_BATAS} & ${BATAS_MAX}) | "
            f"maks {MAX_PER_DAY}/hari")
        notify(f"BASE bot start — dry_run={DRY_RUN} akun={self.me[:10]}…")
        self.verify_structure(); log("struktur LP terverifikasi")
        self.ensure_approval()
        backoff = 1; t0 = time.time()
        while RUN:
            if MAX_RUNTIME and time.time() - t0 > MAX_RUNTIME:
                log(f"batas runtime {MAX_RUNTIME}s tercapai — keluar rapi untuk dirotasi")
                break
            try:
                self.tick(); backoff = 1
            except Exception as e:
                self._nonce = None      # sinkron ulang setelah error
                log(f"error: {type(e).__name__}: {e} — coba lagi {backoff}s")
                time.sleep(backoff); backoff = min(backoff*2, 60)
                try: self.connect()
                except Exception: pass
                continue
            time.sleep(POLL_SEC)
        log("berhenti."); notify("BASE bot berhenti.")

if __name__ == "__main__":
    if MODE == "ACTIVE":
        log("MODE=ACTIVE dinonaktifkan — terbukti rugi di semua ukuran umpan.")
        log("  Umpan hanya memicu respons kecil, dan token umpan")
        log("  berpindah utuh ke bot (rasio ~1,0). Umpan 500 = -$0,104/siklus.")
        sys.exit(1)
    Bot().run()
